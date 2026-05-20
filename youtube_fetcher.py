"""
youtube_fetcher.py
------------------
Fetches all YouTube channel and video data via YouTube Data API v3.

Pipeline (identical to channel_tester.py, adapted for app.py structure):
  Step 0: Gemini analyses all companies together -> builds industry context
  Step 1: Search "[company] official" -> Gemini verifies with context
  Step 2-4: Gemini generates 3 context-aware alternate queries -> verify each
  Step 5: Gemini identifies exact handle -> direct forHandle lookup (NO re-verify)
  Step 6: Video-based fallback -> Gemini verifies with context
  Step 7: Give up -> unverified

API key rotation:
  - Reads YOUTUBE_API_KEY, YOUTUBE_API_KEY_2, YOUTUBE_API_KEY_3, YOUTUBE_API_KEY_4 from .env
  - Keys are tried in order; when one hits daily quota it is marked spent and the next is used
  - If all keys are exhausted, QuotaExhaustedError is raised — caught by app.py workers
    and surfaced to the user as a dedicated /quota-exhausted page

API usage:
  - Gemini 2.0 Flash  : industry context, channel verification, query generation, handle lookup
  - YouTube Data API  : channel search, channel stats, video data, forHandle lookup
  - Groq              : NOT used in discovery phase
"""

import os
import re
import requests
from datetime import datetime


# ── Load .env at import time (force-write, not setdefault) ──────────────────
def _load_env():
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '.env')
    if not _os.path.exists(path):
        return
    with open(path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                k, v = _line.split('=', 1)
                _os.environ[k.strip()] = v.strip().strip('"').strip("'")

_load_env()

GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
YT_BASE         = "https://www.googleapis.com/youtube/v3"


# ---------------------------------------------------------------------------
# YouTube API key pool — rotation on quota exhaustion
# ---------------------------------------------------------------------------

class QuotaExhaustedError(Exception):
    """
    Raised when every YouTube API key in the pool has hit its daily quota.
    Caught by run_channel_finding() and run_analysis() in app.py, which set
    jobs[job_id]["quota_exhausted"] = True and signal the loading page to
    redirect to /quota-exhausted.
    """
    pass


class YouTubeKeyPool:
    """
    Manages up to 4 YouTube Data API keys loaded from .env.

    Key env vars (in priority order):
        YOUTUBE_API_KEY       ← primary key (required)
        YOUTUBE_API_KEY_2     ← fallback key 2 (optional)
        YOUTUBE_API_KEY_3     ← fallback key 3 (optional)
        YOUTUBE_API_KEY_4     ← fallback key 4 (optional)

    Behaviour:
        - get_key()  → returns the current active key string
        - rotate()   → marks the current key as spent, advances to the next
                        valid key; if none remain raises QuotaExhaustedError
        - is_quota_error(data) → True when the API response signals quota
                                  exhaustion (HTTP 403 + quotaExceeded reason)
    """

    # YouTube API quota-exhaustion error reasons
    _QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}

    def __init__(self):
        # Read all 4 possible keys from environment; filter out blank ones
        candidates = [
            os.getenv("YOUTUBE_API_KEY",   ""),
            os.getenv("YOUTUBE_API_KEY_2", ""),
            os.getenv("YOUTUBE_API_KEY_3", ""),
            os.getenv("YOUTUBE_API_KEY_4", ""),
        ]
        self._keys  = [k.strip() for k in candidates if k.strip()]
        self._index = 0  # index of the currently active key
        self._spent = set()  # indices of keys already rotated away from

        if not self._keys:
            raise ValueError(
                "No YouTube API key found. "
                "Set YOUTUBE_API_KEY (and optionally YOUTUBE_API_KEY_2/3/4) in your .env file."
            )

    # ── Public interface ────────────────────────────────────────────────────

    def get_key(self) -> str:
        """Returns the current active API key."""
        return self._keys[self._index]

    def rotate(self) -> str:
        """
        Marks the current key as spent and advances to the next available key.
        Returns the new active key string.
        Raises QuotaExhaustedError if every key has been spent.
        """
        self._spent.add(self._index)

        for i, key in enumerate(self._keys):
            if i not in self._spent and key:
                self._index = i
                return key

        # All keys spent
        raise QuotaExhaustedError(
            f"All {len(self._keys)} YouTube API key(s) have reached their daily quota. "
            "Quota resets at midnight Pacific Time. Add more keys to .env or try again tomorrow."
        )

    @staticmethod
    def is_quota_error(data: dict) -> bool:
        """
        Returns True when a YouTube API JSON response signals quota exhaustion.
        Checks both the HTTP error code (403) and the specific error reason.
        """
        if "error" not in data:
            return False
        err = data["error"]
        if err.get("code") != 403:
            return False
        for detail in err.get("errors", []):
            if detail.get("reason") in YouTubeKeyPool._QUOTA_REASONS:
                return True
        # Some responses embed the reason directly in the message
        msg = err.get("message", "").lower()
        return any(r.lower() in msg for r in YouTubeKeyPool._QUOTA_REASONS)


# Module-level singleton — initialised once at startup, shared across all threads
_key_pool = YouTubeKeyPool()


def _get_yt_key() -> str:
    """Returns the current active YouTube API key from the pool."""
    return _key_pool.get_key()


# ---------------------------------------------------------------------------
# Gemini helper
# ---------------------------------------------------------------------------

def _gemini(prompt):
    """Gemini 2.0 Flash call. Returns text or '' on failure."""
    try:
        url  = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={os.getenv('GEMINI_API_KEY','')}"
        )
        r    = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
        data = r.json()
        cands = data.get("candidates", [])
        if not cands:
            return ""
        parts = cands[0].get("content", {}).get("parts", [])
        return parts[0].get("text", "").strip() if parts else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# YouTube helper — with quota-aware key rotation
# ---------------------------------------------------------------------------

def _yt(endpoint, params, _retry=True):
    """
    YouTube Data API GET with automatic key rotation on quota exhaustion.

    Flow:
      1. Make request with current key from _key_pool.
      2. If response is a quota error → rotate to next key → retry ONCE.
      3. If all keys are exhausted during rotation → QuotaExhaustedError propagates up.
      4. Any other API error → raise RuntimeError with a readable message.

    The _retry flag prevents infinite recursion: the recursive call always
    passes _retry=False so a second quota hit (very unlikely) just raises.
    """
    params = dict(params)  # copy so we don't mutate caller's dict
    params["key"] = _get_yt_key()

    r    = requests.get(f"{YT_BASE}/{endpoint}", params=params, timeout=10)
    data = r.json()

    if "error" in data:
        # ── Quota exhausted: rotate key and retry once ──────────────────────
        if YouTubeKeyPool.is_quota_error(data) and _retry:
            _key_pool.rotate()          # raises QuotaExhaustedError if all spent
            return _yt(endpoint, params, _retry=False)

        # ── Any other API error ─────────────────────────────────────────────
        code = data["error"].get("code", "?")
        msg  = data["error"].get("message", "Unknown error")
        raise RuntimeError(
            f"YouTube API error ({code}) — {msg}. "
            f"Verify YOUTUBE_API_KEY in .env and check quota at "
            f"console.cloud.google.com/apis/api/youtube.googleapis.com"
        )

    return data


# ---------------------------------------------------------------------------
# Step 0: Company context pre-pass — called ONCE before any searches
# ---------------------------------------------------------------------------

def build_company_context(all_company_names):
    """
    Analyses ALL company names together before any searching begins.

    Gemini looks at the full list and determines:
    - What industry/sector these companies share
    - What type of YouTube content their channels would produce
    - Any known channel handles or aliases
    - Company names that are ambiguous (could match wrong channels)

    Returns a context dict injected into every subsequent verification call.
    This is what prevents matching a company name to a regional comedy or
    news channel that happens to share a similar word.

    Called once by fetch_all_companies() before the per-company loop.
    """
    company_list = ", ".join(all_company_names)

    prompt = f"""You are helping find the correct official YouTube channels for a group of companies.

These companies will be analysed together as a competitive group:
{company_list}

Your task:
1. Identify what industry or business sector these companies share
2. Describe what type of YouTube content their official channels would publish
3. Note any known YouTube channel names, handles, or aliases for any of them
4. Flag any company names that are generic words or could easily match unrelated channels

Return your response in this exact format with no extra text:

INDUSTRY: [one sentence describing the shared industry/sector]
CONTENT_TYPE: [one sentence describing the type of YouTube videos these companies post]
KNOWN_CHANNELS: [list any known YouTube handles or channel names, or write NONE]
AMBIGUOUS_NAMES: [list any company names that could match wrong channels, or write NONE]"""

    raw = _gemini(prompt)

    context = {
        "industry": "technology or business software",
        "content_type": "product demos, tutorials, and business content",
        "known_channels": "",
        "ambiguous_names": "",
        "raw": raw
    }

    if raw:
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("INDUSTRY:"):
                context["industry"] = line.replace("INDUSTRY:", "").strip()
            elif line.startswith("CONTENT_TYPE:"):
                context["content_type"] = line.replace("CONTENT_TYPE:", "").strip()
            elif line.startswith("KNOWN_CHANNELS:"):
                context["known_channels"] = line.replace("KNOWN_CHANNELS:", "").strip()
            elif line.startswith("AMBIGUOUS_NAMES:"):
                context["ambiguous_names"] = line.replace("AMBIGUOUS_NAMES:", "").strip()

    return context


# ---------------------------------------------------------------------------
# Gemini-powered verification — context-aware
# ---------------------------------------------------------------------------

def verify_channel_belongs_to_company(channel_name, channel_description,
                                       company_name, company_context=None):
    """
    Asks Gemini 2.0 Flash if a YouTube channel belongs to a given company.
    Passes full industry context so Gemini knows what to look for.
    Returns True (verified) or False (not a match).
    """
    context_block = ""
    if company_context:
        context_block = (
            f"\nIndustry context: {company_context.get('industry', '')}"
            f"\nExpected content type: {company_context.get('content_type', '')}"
            f"\nAmbiguous names to watch for: {company_context.get('ambiguous_names', 'none')}\n"
        )

    prompt = f"""You are verifying YouTube channel ownership for a competitive intelligence report.
{context_block}
Company we are looking for: {company_name}
YouTube channel name found: {channel_name}
YouTube channel description: {channel_description[:500] if channel_description else "No description available"}

Does this YouTube channel belong to {company_name}?

Rules:
- Answer only YES or NO
- YES means this is the official or primary YouTube channel of {company_name}
- NO means this is a fan channel, comedy channel, news channel, or a different company entirely
- If the channel language, content, or description clearly does not match the expected industry above, answer NO
- If you are not sure, answer NO

Answer:"""

    answer = _gemini(prompt)
    if not answer:
        return True  # Fail open on API error only
    return answer.strip().upper().startswith("YES")


def get_ai_alternate_queries(company_name, company_context=None):
    """
    Asks Gemini for 3 smart alternate YouTube search queries.
    Uses industry context so queries are sector-specific.
    Returns list of up to 3 query strings.
    """
    context_block = ""
    if company_context:
        context_block = (
            f"Industry: {company_context.get('industry', '')}\n"
            f"Expected content: {company_context.get('content_type', '')}\n"
        )

    prompt = f"""A YouTube search for "{company_name} official" did not find the correct channel.

{context_block}
Generate exactly 3 alternative YouTube search queries to find the official
YouTube channel for: {company_name}

Consider:
- Their industry: {company_context.get('industry', '') if company_context else ''}
- Common channel naming patterns in this sector
- Product names, sub-brands, or abbreviations they might use

Return ONLY the 3 search queries, one per line, no numbering, no explanation."""

    raw = _gemini(prompt)
    if not raw:
        industry = company_context.get("industry", "software") if company_context else "software"
        return [
            f"{company_name} {industry}",
            f"{company_name} tutorials",
            f"{company_name} official channel"
        ]
    queries = [line.strip() for line in raw.split("\n") if line.strip()]
    return queries[:3]


# ---------------------------------------------------------------------------
# Gemini handle finder — Step 5
# ---------------------------------------------------------------------------

def find_channel_via_gemini(company_name, company_context=None):
    """
    Asks Gemini to identify the exact YouTube channel handle.
    Uses industry context to avoid wrong matches.
    Returns (channel_id, channel_title) or (None, None).
    """
    context_hint = ""
    if company_context:
        context_hint = f" (operates in: {company_context.get('industry', '')})"

    prompt = (
        f"What is the official YouTube channel handle for the company "
        f"'{company_name}'{context_hint}? "
        f"Return ONLY the YouTube handle in the format @HandleName with no spaces, "
        f"no explanation, no punctuation, nothing else. "
        f"If you are not certain, return UNKNOWN."
    )

    raw_handle = _gemini(prompt)

    if not raw_handle or "UNKNOWN" in raw_handle.upper():
        return None, None

    match = re.search(r'@[\w\-]+', raw_handle)
    if match:
        handle = match.group(0).lstrip("@")
    else:
        handle = raw_handle.strip().split()[0].lstrip("@")

    if not handle:
        return None, None

    try:
        data = _yt("channels", {"part": "snippet", "forHandle": handle})
        if "items" in data and data["items"]:
            item = data["items"][0]
            return item["id"], item["snippet"]["title"]

        results = search_channel(f"@{handle}", max_results=1)
        if results:
            cid, ctitle, desc = results[0]
            full_desc = fetch_channel_description(cid) or desc
            if verify_channel_belongs_to_company(ctitle, full_desc, company_name, company_context):
                return cid, ctitle

    except Exception:
        pass

    return None, None


# ---------------------------------------------------------------------------
# YouTube search helpers
# ---------------------------------------------------------------------------

def search_channel(query, max_results=2):
    """Searches YouTube for channels. Returns list of (id, title, description)."""
    try:
        data = _yt("search", {"part": "snippet", "q": query, "type": "channel",
                              "maxResults": max_results})
        return [(item["snippet"]["channelId"],
                 item["snippet"]["title"],
                 item["snippet"].get("description", ""))
                for item in data.get("items", [])]
    except Exception:
        return []


def fetch_channel_description(channel_id):
    """Fetches full About/description text for a channel."""
    try:
        data = _yt("channels", {"part": "snippet", "id": channel_id})
        if "items" not in data or not data["items"]:
            return ""
        return data["items"][0]["snippet"].get("description", "")
    except Exception:
        return ""


def _fetch_subs(channel_id):
    """Fetches subscriber count for a channel."""
    try:
        data = _yt("channels", {"part": "statistics", "id": channel_id})
        items = data.get("items", [])
        return int(items[0].get("statistics", {}).get("subscriberCount", 0)) if items else 0
    except Exception:
        return 0


def find_channel_via_videos(company_name, company_context=None):
    """
    Last-resort fallback: searches videos, extracts unique channel owners,
    Gemini-verifies each one with full industry context.
    """
    try:
        data = _yt("search", {"part": "snippet", "q": f"{company_name} official video",
                              "type": "video", "maxResults": 5})
        if "items" not in data:
            return None, None

        seen = set()
        for item in data["items"]:
            cid    = item["snippet"]["channelId"]
            ctitle = item["snippet"]["channelTitle"]
            if cid in seen:
                continue
            seen.add(cid)
            desc = fetch_channel_description(cid)
            if verify_channel_belongs_to_company(ctitle, desc, company_name, company_context):
                return cid, ctitle

        return None, None
    except Exception:
        return None, None


def get_channel_id_verified(company_name, company_context=None):
    """
    Full context-aware verification pipeline (Steps 1-6).
    All AI calls receive company_context built in Step 0.
    Returns (channel_id, channel_title, verified: bool).

    QuotaExhaustedError from _yt() is NOT caught here — it propagates up to
    run_channel_finding() / run_analysis() in app.py which handle it.
    """
    # Step 1: Primary search
    for cid, ctitle, desc in search_channel(f"{company_name} official", max_results=2):
        full_desc = fetch_channel_description(cid) or desc
        if verify_channel_belongs_to_company(ctitle, full_desc, company_name, company_context):
            return cid, ctitle, True

    # Steps 2-4: Context-aware alternate queries
    for query in get_ai_alternate_queries(company_name, company_context):
        for cid, ctitle, desc in search_channel(query, max_results=2):
            full_desc = fetch_channel_description(cid) or desc
            if verify_channel_belongs_to_company(ctitle, full_desc, company_name, company_context):
                return cid, ctitle, True

    # Step 5: Gemini handle lookup (no re-verify needed)
    cid, ctitle = find_channel_via_gemini(company_name, company_context)
    if cid:
        return cid, ctitle, True

    # Step 6: Video-based fallback
    cid, ctitle = find_channel_via_videos(company_name, company_context)
    if cid:
        return cid, ctitle, True

    return None, None, False


# ---------------------------------------------------------------------------
# Channel stats + video data
# ---------------------------------------------------------------------------

def get_channel_stats(channel_id):
    """Returns subscriber count, video count, view count, uploads playlist ID."""
    try:
        data = _yt("channels", {"part": "statistics,snippet,contentDetails", "id": channel_id})
        if "items" not in data or not data["items"]:
            return {}
        item  = data["items"][0]
        stats = item.get("statistics", {})
        return {
            "subscribers":      int(stats.get("subscriberCount", 0)),
            "total_videos":     int(stats.get("videoCount", 0)),
            "total_views":      int(stats.get("viewCount", 0)),
            "uploads_playlist": item["contentDetails"]["relatedPlaylists"].get("uploads", "")
        }
    except Exception:
        return {}


def get_recent_videos(uploads_playlist_id, max_results=20):
    """Fetches most recent videos from a channel's uploads playlist."""
    try:
        data = _yt("playlistItems", {"part": "contentDetails,snippet",
                                     "playlistId": uploads_playlist_id, "maxResults": max_results})
        if "items" not in data:
            return []
        return [
            {
                "video_id":     item["contentDetails"]["videoId"],
                "title":        item["snippet"]["title"],
                "description":  item["snippet"].get("description", ""),
                "published_at": item["contentDetails"].get("videoPublishedAt", "")
            }
            for item in data["items"]
        ]
    except Exception:
        return []


def get_video_stats(video_ids):
    """Batch-fetches statistics and snippet for up to 50 video IDs."""
    if not video_ids:
        return {}
    try:
        data = _yt("videos", {"part": "statistics,snippet", "id": ",".join(video_ids)})
        result = {}
        for item in data.get("items", []):
            vid_id = item["id"]
            stats  = item.get("statistics", {})
            result[vid_id] = {
                "views":        int(stats.get("viewCount", 0)),
                "likes":        int(stats.get("likeCount", 0)),
                "comments":     int(stats.get("commentCount", 0)),
                "title":        item["snippet"]["title"],
                "description":  item["snippet"].get("description", ""),
                "published_at": item["snippet"]["publishedAt"],
                "tags":         item["snippet"].get("tags", []),
            }
        return result
    except Exception:
        return {}


def calculate_upload_frequency(videos):
    """Calculates average number of days between uploads."""
    dates = []
    for v in videos:
        pub = v.get("published_at", "")
        if pub:
            try:
                dates.append(datetime.fromisoformat(pub.replace("Z", "+00:00")))
            except Exception:
                pass
    if len(dates) < 2:
        return None
    dates.sort(reverse=True)
    gaps = [(dates[i] - dates[i + 1]).days for i in range(len(dates) - 1)]
    return round(sum(gaps) / len(gaps), 1)


# ---------------------------------------------------------------------------
# Per-company fetcher
# ---------------------------------------------------------------------------

def fetch_company_data(company_name, company_context=None):
    """
    Fetches, verifies, and enriches all data for one company.
    Accepts company_context from build_company_context() for accurate matching.

    QuotaExhaustedError is NOT caught here — it propagates up to the caller
    (run_channel_finding / run_analysis in app.py).
    """
    result = {
        "company_name":           company_name,
        "channel_found":          False,
        "channel_verified":       False,
        "channel_title":          None,
        "channel_id":             None,
        "subscribers":            0,
        "total_videos":           0,
        "total_views":            0,
        "avg_views_per_video":    0,
        "avg_likes_per_video":    0,
        "avg_comments_per_video": 0,
        "upload_frequency_days":  None,
        "top_videos":             [],
        "recent_videos":          [],
        "topics":                 [],
        "engagement_rate":        0,
        "transcript_summaries":   [],
        "error":                  None
    }

    try:
        channel_id, channel_title, verified = get_channel_id_verified(
            company_name, company_context
        )

        if not channel_id:
            result["error"] = (
                f"Could not verify an authentic YouTube channel for '{company_name}'. "
                f"Searched via direct lookup, 3 context-aware alternate queries, "
                f"Gemini handle lookup, and video search."
            )
            return result

        result["channel_found"]    = True
        result["channel_verified"] = verified
        result["channel_id"]       = channel_id
        result["channel_title"]    = channel_title

        stats                      = get_channel_stats(channel_id)
        result["subscribers"]      = stats.get("subscribers", 0)
        result["total_videos"]     = stats.get("total_videos", 0)
        result["total_views"]      = stats.get("total_views", 0)
        uploads_playlist           = stats.get("uploads_playlist", "")

        if not uploads_playlist:
            result["error"] = "Could not access uploads playlist"
            return result

        recent = get_recent_videos(uploads_playlist, max_results=20)
        if not recent:
            result["error"] = "No videos found in uploads playlist"
            return result

        video_ids   = [v["video_id"] for v in recent]
        video_stats = get_video_stats(video_ids[:20])

        enriched = []
        for v in recent:
            vid_id = v["video_id"]
            if vid_id in video_stats:
                enriched.append({
                    "video_id":     vid_id,
                    "title":        video_stats[vid_id]["title"],
                    "description":  video_stats[vid_id]["description"],
                    "views":        video_stats[vid_id]["views"],
                    "likes":        video_stats[vid_id]["likes"],
                    "comments":     video_stats[vid_id]["comments"],
                    "published_at": video_stats[vid_id]["published_at"],
                    "tags":         video_stats[vid_id]["tags"],
                    "url":          f"https://youtube.com/watch?v={vid_id}"
                })

        result["recent_videos"] = enriched
        result["top_videos"]    = sorted(enriched, key=lambda x: x["views"], reverse=True)[:5]

        if enriched:
            result["avg_views_per_video"]    = round(sum(v["views"]    for v in enriched) / len(enriched))
            result["avg_likes_per_video"]    = round(sum(v["likes"]    for v in enriched) / len(enriched))
            result["avg_comments_per_video"] = round(sum(v["comments"] for v in enriched) / len(enriched))

        result["upload_frequency_days"] = calculate_upload_frequency(enriched)

    except QuotaExhaustedError:
        # Let it propagate — app.py handles it
        raise

    except Exception as e:
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# Master entry point — called by app.py
# ---------------------------------------------------------------------------

def refetch_channel_from_url(url, company_name):
    """
    Called when the user manually corrects a channel URL on the verify page.

    Flow:
      1. Extract channel ID from the URL (supports all 3 YouTube URL formats)
      2. Call Gemini ONCE — only to confirm this is a real accessible YouTube channel
         (no industry/relevance check — user override is trusted)
      3. Fetch full stats + videos for that channel ID
      4. Return a complete company data dict ready for Phase 2

    Supports:
      https://www.youtube.com/channel/UCxxxxxx   <- direct channel ID
      https://www.youtube.com/@HubSpot           <- handle
      https://www.youtube.com/c/HubSpot          <- custom name

    Returns a fully populated data dict on success, or None on failure.
    """

    # ── Step 1: Extract channel ID from URL ──────────────────────────────────
    channel_id    = None
    channel_title = None

    # Format A: /channel/UC...
    m = re.search(r'/channel/(UC[\w\-]{20,})', url)
    if m:
        channel_id = m.group(1)

    # Format B: /@handle
    if not channel_id:
        m = re.search(r'/@([\w\-]+)', url)
        if m:
            handle = m.group(1)
            try:
                data = _yt("channels", {"part": "snippet", "forHandle": handle})
                if "items" in data and data["items"]:
                    channel_id    = data["items"][0]["id"]
                    channel_title = data["items"][0]["snippet"]["title"]
            except Exception:
                pass

    # Format C: /c/customname or /user/username — search YouTube for it
    if not channel_id:
        m = re.search(r'/(?:c|user)/([\w\-]+)', url)
        if m:
            custom_name = m.group(1)
            try:
                results = search_channel(custom_name, max_results=1)
                if results:
                    channel_id = results[0][0]
                    channel_title = results[0][1]
            except Exception:
                pass

    if not channel_id:
        return None

    # ── Step 2: Gemini accessibility check ───────────────────────────────────
    # Only confirms "is this a real YouTube channel?" — no industry verification.
    # User override is trusted; we just make sure the URL isn't broken/private.
    gemini_prompt = (
        f"I have a YouTube channel ID: {channel_id}\n"
        f"The user provided this YouTube URL for company '{company_name}': {url}\n\n"
        f"Is this a real, publicly accessible YouTube channel that contains videos "
        f"a program could analyse? Answer only YES or NO."
    )
    gemini_answer = _gemini(gemini_prompt)
    if gemini_answer and gemini_answer.strip().upper().startswith("NO"):
        return None
    # If Gemini fails (returns ""), we proceed anyway — trust the user

    # ── Step 3: Fetch full channel stats + videos ─────────────────────────────
    result = {
        "company_name":           company_name,
        "channel_found":          False,
        "channel_verified":       True,   # user manually confirmed
        "channel_title":          channel_title,
        "channel_id":             channel_id,
        "subscribers":            0,
        "total_videos":           0,
        "total_views":            0,
        "avg_views_per_video":    0,
        "avg_likes_per_video":    0,
        "avg_comments_per_video": 0,
        "upload_frequency_days":  None,
        "top_videos":             [],
        "recent_videos":          [],
        "topics":                 [],
        "engagement_rate":        0,
        "transcript_summaries":   [],
        "error":                  None
    }

    try:
        stats = get_channel_stats(channel_id)
        if not stats:
            result["error"] = f"Could not fetch stats for channel ID {channel_id}"
            return result

        # Use channel title from stats if we didn't get it from the handle lookup
        if not channel_title:
            # get_channel_stats doesn't return title — fetch it separately
            try:
                title_data = _yt("channels", {"part": "snippet", "id": channel_id})
                if "items" in title_data and title_data["items"]:
                    channel_title = title_data["items"][0]["snippet"]["title"]
            except Exception:
                channel_title = company_name

        result["channel_found"]  = True
        result["channel_title"]  = channel_title
        result["subscribers"]    = stats.get("subscribers", 0)
        result["total_videos"]   = stats.get("total_videos", 0)
        result["total_views"]    = stats.get("total_views", 0)
        uploads_playlist         = stats.get("uploads_playlist", "")

        if not uploads_playlist:
            result["error"] = "Could not access uploads playlist for corrected channel"
            return result

        recent = get_recent_videos(uploads_playlist, max_results=20)
        if not recent:
            result["error"] = "No videos found in corrected channel"
            return result

        video_ids   = [v["video_id"] for v in recent]
        video_stats = get_video_stats(video_ids[:20])

        enriched = []
        for v in recent:
            vid_id = v["video_id"]
            if vid_id in video_stats:
                enriched.append({
                    "video_id":     vid_id,
                    "title":        video_stats[vid_id]["title"],
                    "description":  video_stats[vid_id]["description"],
                    "views":        video_stats[vid_id]["views"],
                    "likes":        video_stats[vid_id]["likes"],
                    "comments":     video_stats[vid_id]["comments"],
                    "published_at": video_stats[vid_id]["published_at"],
                    "tags":         video_stats[vid_id]["tags"],
                    "url":          f"https://youtube.com/watch?v={vid_id}"
                })

        result["recent_videos"] = enriched
        result["top_videos"]    = sorted(enriched, key=lambda x: x["views"], reverse=True)[:5]

        if enriched:
            result["avg_views_per_video"]    = round(sum(v["views"]    for v in enriched) / len(enriched))
            result["avg_likes_per_video"]    = round(sum(v["likes"]    for v in enriched) / len(enriched))
            result["avg_comments_per_video"] = round(sum(v["comments"] for v in enriched) / len(enriched))

        result["upload_frequency_days"] = calculate_upload_frequency(enriched)

    except QuotaExhaustedError:
        raise  # propagate to app.py
    except Exception as e:
        result["error"] = str(e)

    return result



def fetch_all_companies(your_company, competitors):
    """
    Master function replacing the per-company loop in app.py.

    Step 0: One Gemini call to understand the full competitive group.
    Step 1+: Each company fetched with that context injected.

    app.py calls this instead of looping fetch_company_data() individually.
    Returns (all_data list, company_context dict).
    """
    all_names = [your_company] + [c for c in competitors if c]

    # Step 0: Build industry context from ALL company names together
    company_context = build_company_context(all_names)

    # Fetch each company with context
    all_data = []
    for name in all_names:
        data = fetch_company_data(name, company_context)
        all_data.append(data)

    return all_data, company_context
