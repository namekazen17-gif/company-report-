"""
transcriber.py
--------------
3-tier fallback pipeline for extracting and summarising video content:

  Tier 1: youtube-transcript-api  ->  gets official captions (any language)
  Tier 2: Title + Description     ->  used if transcript disabled/unavailable
  Tier 3: Gemini 2.5 Flash        ->  watches the actual video if Tiers 1 & 2 fail

Uses:
  - youtube-transcript-api for captions
  - Groq Llama 4 Scout for summarisation
  - google-genai (official new SDK) for Gemini video fallback
"""

import os
import time
from groq import Groq
from google import genai as google_genai

try:
    from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
    TRANSCRIPT_API_AVAILABLE = True
except ImportError:
    TRANSCRIPT_API_AVAILABLE = False

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

# Initialise Gemini client using official new SDK
# Reads GEMINI_API_KEY from environment automatically
gemini_client = google_genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Models
SCOUT_MODEL  = "meta-llama/llama-4-scout-17b-16e-instruct"  # Groq — multilingual summarisation
GEMINI_MODEL = "gemini-2.5-flash"                            # Google — video understanding fallback


# ---------------------------------------------------------------------------
# TIER 1: Official YouTube transcript (any language)
# ---------------------------------------------------------------------------

def fetch_transcript_text(video_id):
    """Pull official captions. Tries manual first, then auto-generated."""
    if not TRANSCRIPT_API_AVAILABLE:
        return None
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            transcript = transcript_list.find_manually_created_transcript(
                ['en', 'ar', 'hi', 'fr', 'es', 'de', 'pt', 'zh', 'ja', 'ko']
            )
        except Exception:
            transcript = transcript_list.find_generated_transcript(
                ['en', 'ar', 'hi', 'fr', 'es', 'de', 'pt', 'zh', 'ja', 'ko']
            )
        entries = transcript.fetch()
        full_text = " ".join([e["text"] for e in entries])
        return full_text if len(full_text) > 100 else None
    except (TranscriptsDisabled, NoTranscriptFound):
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# TIER 3: Gemini 2.5 Flash watches the actual video
# ---------------------------------------------------------------------------

def summarise_with_gemini(video_url, title, company_name):
    """
    Sends YouTube URL to Gemini 2.5 Flash via the official google-genai SDK.
    Gemini natively understands YouTube video content from the URL.
    Returns plain-text summary or None if it fails.
    """
    if not gemini_client:
        return None

    prompt = (
        f"Watch this YouTube video for {company_name}: {video_url}\n\n"
        f'Title: "{title}"\n\n'
        "Summarise in structured English:\n"
        "1. Main topic and purpose of this video?\n"
        "2. Who is the target audience?\n"
        f"3. Key messages or value propositions {company_name} communicates?\n"
        "4. Products, services, or solutions featured?\n"
        "5. Tone — educational, promotional, testimonial, or entertainment?\n"
        "6. One unique insight about their video marketing strategy?\n\n"
        "Be specific and concise. 150-200 words total."
    )

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        return response.text if response.text else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Llama 4 Scout: Standardise any content into a clean English brief
# ---------------------------------------------------------------------------

def summarise_with_scout(raw_content, title, company_name, source_type):
    """Turns raw content (transcript/description/gemini) into a clean English content brief."""
    source_note = {
        "transcript": "This is the full video transcript (may be in any language — translate and summarise into English).",
        "description": "No transcript available. This is the video title and description only.",
        "gemini": "This is an AI-generated summary of the video content from Gemini.",
        "title_only": "Only the video title was available."
    }.get(source_type, "")

    prompt = f"""You are a video content analyst. {source_note}

Company: {company_name}
Video Title: "{title}"

Content:
{raw_content[:4000]}

Write a structured English summary (120-150 words) covering:
- Main topic and video purpose
- Target audience
- Key messages and value propositions
- Products/services/solutions featured
- Video tone and style
- One strategic insight about {company_name}'s content strategy

Be specific. Reference actual content, not generic statements."""

    try:
        response = groq_client.chat.completions.create(
            model=SCOUT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.4
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Summary unavailable: {str(e)}"


# ---------------------------------------------------------------------------
# Master per-video function: 3-tier fallback
# ---------------------------------------------------------------------------

def process_video(video, company_name):
    """Full 3-tier pipeline for one video. Returns dict with summary and source_tier."""
    video_id   = video["video_id"]
    title      = video["title"]
    description = video.get("description", "")
    url        = video.get("url", f"https://youtube.com/watch?v={video_id}")

    # TIER 1: Official transcript
    raw_transcript = fetch_transcript_text(video_id)
    if raw_transcript:
        return {
            "video_id": video_id, "title": title, "url": url,
            "summary": summarise_with_scout(raw_transcript, title, company_name, "transcript"),
            "source_tier": "transcript"
        }

    # TIER 2: Description fallback
    if description and len(description) > 200:
        return {
            "video_id": video_id, "title": title, "url": url,
            "summary": summarise_with_scout(
                f"Title: {title}\n\nDescription: {description}", title, company_name, "description"
            ),
            "source_tier": "description"
        }

    # TIER 3: Gemini watches video
    gemini_summary = summarise_with_gemini(url, title, company_name)
    if gemini_summary:
        return {
            "video_id": video_id, "title": title, "url": url,
            "summary": summarise_with_scout(gemini_summary, title, company_name, "gemini"),
            "source_tier": "gemini"
        }

    # Final fallback: title only
    return {
        "video_id": video_id, "title": title, "url": url,
        "summary": summarise_with_scout(f"Title only: {title}", title, company_name, "title_only"),
        "source_tier": "title_only"
    }


def transcribe_company_videos(company_data, max_videos=5, progress_callback=None):
    """Processes top N videos through 3-tier pipeline. Attaches results to company_data."""
    company_name = company_data["company_name"]
    top_videos   = company_data.get("top_videos", [])[:max_videos]
    summaries    = []
    for i, video in enumerate(top_videos):
        if progress_callback:
            progress_callback(f"  Analysing video {i+1}/{len(top_videos)}: {video['title'][:50]}...")
        summaries.append(process_video(video, company_name))
        time.sleep(0.5)
    company_data["transcript_summaries"] = summaries
    return company_data
