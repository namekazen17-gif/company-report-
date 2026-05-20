"""
analyser.py
-----------
Pure Python data analysis + AI-powered theme cleaning.

Takes raw YouTube data + transcript summaries → produces structured
analysis dict consumed by insights.py and report_builder.py.

Key outputs:
  - ranked: list of companies sorted by composite score
  - gaps: content topics competitors cover that your company doesn't
  - leader: name of the top-ranked company
  - engagement metrics per company
  - topic clusters extracted from titles + transcript summaries
  - content_themes: clean 2-sentence theme summaries per company (AI-cleaned)
"""

import os
import re
from collections import Counter
from groq import Groq

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CLEAN_MODEL = "llama-3.1-8b-instant"   # Fast theme cleanup

# Words filtered out of topic extraction
STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "how", "what", "why", "when", "your", "you", "we", "our", "my", "i",
    "it", "its", "this", "that", "will", "can", "do", "does", "did",
    "have", "has", "had", "not", "no", "vs", "ft", "ep", "part", "new",
    "get", "make", "using", "use", "top", "best", "tips", "guide", "full",
    "official", "video", "watch", "channel", "2020", "2021", "2022",
    "2023", "2024", "2025", "2026", "|", "-", "–", "about", "more",
    "also", "just", "like", "know", "see", "need", "want", "here",
    "there", "their", "they", "them", "then", "than", "very", "so",
    "all", "any", "from", "into", "over", "such", "only", "even", "most"
}


# ---------------------------------------------------------------------------
# Topic extraction
# ---------------------------------------------------------------------------

def extract_topics(videos, summaries=None, top_n=10):
    """
    Extracts key content topics from video titles AND transcript summaries.
    Summaries are double-weighted — they carry richer topic signals.
    """
    all_words = []

    for v in videos:
        title = v.get("title", "").lower()
        title = re.sub(r"[^a-z0-9\s]", " ", title)
        for word in title.split():
            if len(word) > 3 and word not in STOP_WORDS:
                all_words.append(word)

    if summaries:
        for s in summaries:
            text = s.get("summary", "").lower()
            text = re.sub(r"[^a-z0-9\s]", " ", text)
            for word in text.split():
                if len(word) > 4 and word not in STOP_WORDS:
                    all_words.append(word)
                    all_words.append(word)  # double weight

    counter = Counter(all_words)
    return [word for word, count in counter.most_common(top_n)]


# ---------------------------------------------------------------------------
# Engagement scoring
# ---------------------------------------------------------------------------

def calculate_engagement_rate(avg_views, avg_likes, avg_comments):
    """Engagement rate = (likes + comments) / views × 100."""
    if avg_views == 0:
        return 0
    return round((avg_likes + avg_comments) / avg_views * 100, 2)


# ---------------------------------------------------------------------------
# Company ranking
# ---------------------------------------------------------------------------

def rank_companies(all_data):
    """
    Scores each company on 4 dimensions (each normalised 1–10):
      - Subscriber count        (weight: 25%)
      - Average views per video (weight: 30%)
      - Engagement rate         (weight: 25%)
      - Upload frequency        (weight: 20%, inverted — more frequent = higher)

    Returns list of dicts sorted by total_score descending.
    """
    scored = []
    for d in all_data:
        if not d["channel_found"]:
            continue
        scored.append({
            "company":         d["company_name"],
            "subscribers":     d["subscribers"],
            "avg_views":       d["avg_views_per_video"],
            "engagement_rate": calculate_engagement_rate(
                d["avg_views_per_video"],
                d["avg_likes_per_video"],
                d["avg_comments_per_video"]
            ),
            "upload_freq":     d["upload_frequency_days"] or 999
        })

    if not scored:
        return []

    def norm(values, invert=False):
        mn, mx = min(values), max(values)
        if mx == mn:
            return [5.0] * len(values)
        if invert:
            return [round(10 - (v - mn) / (mx - mn) * 9, 2) for v in values]
        return [round(1 + (v - mn) / (mx - mn) * 9, 2) for v in values]

    sub_scores  = norm([s["subscribers"]    for s in scored])
    view_scores = norm([s["avg_views"]       for s in scored])
    eng_scores  = norm([s["engagement_rate"] for s in scored])
    freq_scores = norm([s["upload_freq"]     for s in scored], invert=True)

    ranked = []
    for i, s in enumerate(scored):
        total = round(
            sub_scores[i]  * 0.25 +
            view_scores[i] * 0.30 +
            eng_scores[i]  * 0.25 +
            freq_scores[i] * 0.20,
            2
        )
        ranked.append({
            "company":           s["company"],
            "subscribers_score": sub_scores[i],
            "views_score":       view_scores[i],
            "engagement_score":  eng_scores[i],
            "frequency_score":   freq_scores[i],
            "total_score":       total,
            "subscribers":       s["subscribers"],
            "avg_views":         s["avg_views"],
            "engagement_rate":   s["engagement_rate"],
            "upload_freq_days":  s["upload_freq"] if s["upload_freq"] != 999 else None
        })

    ranked.sort(key=lambda x: x["total_score"], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Content gap detection
# ---------------------------------------------------------------------------

def find_content_gaps(all_data, your_company_name):
    """
    Finds topics that competitors cover but your company does not.
    Uses transcript summaries + titles for richer gap detection.
    """
    your_data = next(
        (d for d in all_data if d["company_name"].lower() == your_company_name.lower()),
        None
    )
    if not your_data or not your_data["channel_found"]:
        return []

    your_topics = set(extract_topics(
        your_data.get("recent_videos", []),
        your_data.get("transcript_summaries", []),
        top_n=20
    ))

    competitor_topics = set()
    for d in all_data:
        if d["company_name"].lower() == your_company_name.lower():
            continue
        if not d["channel_found"]:
            continue
        topics = extract_topics(
            d.get("recent_videos", []),
            d.get("transcript_summaries", []),
            top_n=20
        )
        competitor_topics.update(topics)

    gaps = competitor_topics - your_topics
    return list(gaps)[:12]


# ---------------------------------------------------------------------------
# Theme cleaning — AI-powered
# ---------------------------------------------------------------------------

def clean_theme_text(raw_summary, company_name):
    """
    Cleans a video transcript summary into a crisp 2-sentence human insight.
    Removes markdown, AI meta-phrases, headers, and generic filler.
    Uses llama-3.1-8b-instant for speed.
    Falls back to manual extraction if AI fails.
    """
    prompt = f"""You are cleaning a video summary for a client report about {company_name}.

Rules:
- Remove ALL markdown symbols (**, ##, *, _, backticks)
- Remove AI phrases like "Here is a summary", "Main topic:", "Target audience:", "Key messages:"
- Keep only the most important strategic insight about this video
- Write as 2 clean sentences maximum, as a human expert would write
- Maximum 55 words total
- Do not start with the company name
- No preamble, no explanation — return only the 2 cleaned sentences

Raw text to clean:
{raw_summary[:600]}"""

    try:
        response = groq_client.chat.completions.create(
            model=CLEAN_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.2
        )
        cleaned = response.choices[0].message.content.strip()

        # Verify it has no markdown
        if any(sym in cleaned for sym in ['**', '##', '__', '```', '* ', '- ']):
            cleaned = re.sub(r'[*#_`]', '', cleaned)
            cleaned = re.sub(r'^\s*[-•]\s+', '', cleaned, flags=re.MULTILINE)

        # Verify it's not too short (fallback if cleaning went wrong)
        if len(cleaned.split()) < 8:
            raise ValueError("Cleaned text too short")

        return cleaned.strip()

    except Exception:
        # Manual fallback: extract first 2 non-empty sentences from raw text
        cleaned_raw = re.sub(r'[*#_`]', '', raw_summary)
        cleaned_raw = re.sub(r'^\s*[-•]\s+', '', cleaned_raw, flags=re.MULTILINE)
        sentences   = [s.strip() for s in cleaned_raw.split('.') if len(s.strip()) > 15]
        fallback    = '. '.join(sentences[:2])
        if fallback and not fallback.endswith('.'):
            fallback += '.'
        return fallback[:280] if fallback else f"{company_name} produces video content in this topic area."


# ---------------------------------------------------------------------------
# Content themes builder
# ---------------------------------------------------------------------------

def build_content_themes(all_data):
    """
    For each company, generates a real AI insight (40-45 words) from the
    complete content analysis data using Groq LLaMA. No truncation — the
    full transcript summaries, topics, and video data are sent to the model.
    Used in PPTX Slide 5 and web report Content Topics section.
    """
    themes = {}
    for d in all_data:
        if not d["channel_found"]:
            themes[d["company_name"]] = "No channel data available."
            continue

        summaries = d.get("transcript_summaries", [])
        topics    = d.get("topics", [])
        top_vids  = d.get("top_videos", [])

        # Build a rich content brief from all available data
        content_brief_parts = []

        if topics:
            content_brief_parts.append(f"Key topics: {', '.join(topics[:10])}")

        if summaries:
            content_brief_parts.append("Video content analysis:")
            for s in summaries[:5]:
                title   = s.get("title", "")[:80]
                summary = s.get("summary", "")
                if summary:
                    content_brief_parts.append(f"  '{title}': {summary}")

        if top_vids:
            content_brief_parts.append("Top performing videos:")
            for v in top_vids[:3]:
                content_brief_parts.append(
                    f"  '{v.get('title', '')[:60]}' — {v.get('views', 0):,} views"
                )

        if not content_brief_parts:
            themes[d["company_name"]] = (
                f"Content focuses on: {', '.join(topics[:5])}." if topics
                else "Insufficient data to determine content themes."
            )
            continue

        content_brief = "\n".join(content_brief_parts)

        prompt = f"""You are a video marketing strategist. Based on the complete content analysis below for {d['company_name']}'s YouTube channel, write a sharp insight paragraph of exactly 80-100 words.

Content analysis:
{content_brief}

Rules:
- Exactly 80-100 words (count carefully)
- No markdown (no **, ##, *, _)
- No AI phrases ("here is", "based on", "it is important")
- Cover: what content themes dominate, who their target audience is, what makes their content strategy distinctive, and one specific strength or weakness visible in the data
- Write as a human strategist presenting to a board — confident, specific, no filler
- Return ONLY the insight paragraph, nothing else"""

        try:
            response = groq_client.chat.completions.create(
                model=CLEAN_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=350,
                temperature=0.5
            )
            raw = response.choices[0].message.content.strip()
            # Strip any markdown that slipped through
            raw = re.sub(r'\*{1,3}', '', raw)
            raw = re.sub(r'#{1,6}\s?', '', raw)
            raw = re.sub(r'_{1,2}', '', raw)
            raw = re.sub(r'`{1,3}', '', raw)
            themes[d["company_name"]] = raw.strip()
        except Exception:
            # Fallback: compose from available data without truncation
            if summaries:
                first = summaries[0].get("summary", "")
                themes[d["company_name"]] = first if first else (
                    f"Content centres on {', '.join(topics[:4])}." if topics else "Insufficient data."
                )
            else:
                themes[d["company_name"]] = (
                    f"Content centres on {', '.join(topics[:5])}." if topics else "Insufficient data."
                )

    return themes


# ---------------------------------------------------------------------------
# Master analysis function
# ---------------------------------------------------------------------------

def analyse_all(all_data, your_company_name):
    """
    Master analysis function. Called after transcript_summaries are attached.
    Returns complete analysis dict used by insights.py and report_builder.py.
    """
    for d in all_data:
        if d["channel_found"]:
            d["topics"] = extract_topics(
                d.get("recent_videos", []),
                d.get("transcript_summaries", []),
                top_n=8
            )
            d["engagement_rate"] = calculate_engagement_rate(
                d["avg_views_per_video"],
                d["avg_likes_per_video"],
                d["avg_comments_per_video"]
            )
        else:
            d["topics"]          = []
            d["engagement_rate"] = 0

    analysis                   = {}
    analysis["ranked"]         = rank_companies(all_data)
    analysis["gaps"]           = find_content_gaps(all_data, your_company_name)
    analysis["leader"]         = analysis["ranked"][0]["company"] if analysis["ranked"] else "N/A"
    analysis["content_themes"] = build_content_themes(all_data)
    return analysis
