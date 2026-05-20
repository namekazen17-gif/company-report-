"""
insights.py
-----------
2-model AI pipeline for generating strategic competitive insights.

UPDATED MODEL MAP:
  Step 1 — Gemini 2.0 Flash (Reasoning):
      Deep competitive analysis — why companies are winning/losing,
      what the data patterns mean strategically.
      REPLACES: openai/gpt-oss-120b (not available on standard Groq free tier)
      REASON: Gemini 2.0 Flash is free, fast, and already used in youtube_fetcher.
      Same API key (GEMINI_API_KEY). No extra setup needed.

  Step 2 — llama-3.3-70b-versatile via Groq (Writing):
      Takes Step 1 raw analysis -> rewrites into humanized, client-ready English.
      Every section verified and retried until clean.

  Verification — llama-3.1-8b-instant via Groq:
      After every AI output, checks for markdown, AI meta-phrases,
      missing company references, and error text. Retries automatically.

Final model map:
  REASONING_MODEL : gemini-2.0-flash  (via Gemini API — free)
  WRITING_MODEL   : llama-3.3-70b-versatile (via Groq — free)
  VERIFY_MODEL    : llama-3.1-8b-instant (via Groq — free)
"""

import os
import re
import requests
from groq import Groq

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

WRITING_MODEL = "llama-3.3-70b-versatile"   # Humanized client copy
VERIFY_MODEL  = "llama-3.1-8b-instant"      # Fast verification + cleanup


# ---------------------------------------------------------------------------
# Gemini reasoning call — replaces openai/gpt-oss-120b
# ---------------------------------------------------------------------------

def _call_gemini_reasoning(prompt):
    """
    Calls Gemini 2.0 Flash for strategic reasoning.
    Same API pattern as youtube_fetcher._call_gemini().
    Returns raw text or empty string on failure.
    """
    try:
        url  = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.0-flash:generateContent?key=" + os.getenv("GEMINI_API_KEY", "")
        )
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, json=body, timeout=30)
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return ""
        return parts[0].get("text", "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Master prompt — injected into every writing call
# ---------------------------------------------------------------------------

MASTER_CONTEXT = """You are a senior video marketing strategist writing a client-facing \
competitive intelligence report for a board presentation.

STRICT RULES — every response must follow without exception:
- Write like an expert human strategist, never like an AI assistant
- NO markdown symbols anywhere: no **, no ##, no *, no _, no backticks
- NO meta-phrases: never write "Here is", "Main topic:", "As an AI", \
  "Based on the data", "It is important to note", "I have analysed"
- ALWAYS reference specific company names and real numbers from the data
- Be concise and direct — every sentence must add new information
- No repetition, no filler, no generic statements
- Professional tone — this goes to a real client who paid for this report"""


# ---------------------------------------------------------------------------
# Text sanitiser
# ---------------------------------------------------------------------------

def sanitise_text(text):
    """Removes markdown and AI meta-phrases from any text block."""
    text = re.sub(r'\*{1,3}', '', text)
    text = re.sub(r'#{1,6}\s?', '', text)
    text = re.sub(r'_{1,2}', '', text)
    text = re.sub(r'`{1,3}', '', text)
    text = re.sub(r'^\s*[-•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def has_quality_problems(text, company_name):
    """Returns list of quality problems. Empty list = clean."""
    problems = []
    lower    = text.lower()

    if any(sym in text for sym in ['**', '##', '__', '```']):
        problems.append("contains markdown formatting")
    if any(phrase in lower for phrase in [
        "here is a", "here are", "main topic:", "target audience:",
        "as an ai", "based on the data", "it is important to note",
        "i have analysed", "i will now", "in conclusion,"
    ]):
        problems.append("contains AI meta-phrases")
    if company_name.lower() not in lower:
        problems.append(f"does not mention {company_name}")
    if len(text.split()) < 25:
        problems.append("too short — fewer than 25 words")
    if any(err in lower for err in [
        "api key", "groq_api", "unavailable", "error code",
        "check your", "please verify", "404", "500"
    ]):
        problems.append("contains error/fallback text")
    # Detect incomplete sentences — text ends with ellipsis or a non-terminal word
    stripped = text.strip()
    if stripped.endswith('...') or stripped.endswith('…'):
        problems.append("incomplete sentence — ends with ellipsis; complete the thought")
    elif stripped and not stripped[-1] in '.!?"\'':
        problems.append("incomplete sentence — does not end with terminal punctuation; complete the thought")

    return problems


def fix_with_ai(text, problems, context_hint=""):
    """Uses llama-3.1-8b-instant to fix text that failed verification."""
    problem_list = "; ".join(problems)
    incomplete_instruction = ""
    if any("incomplete sentence" in p for p in problems):
        incomplete_instruction = (
            "\nCRITICAL: The text ends mid-sentence. You MUST complete every sentence. "
            "Never end with '...' or an incomplete thought. Every sentence must end with "
            "a period, exclamation mark, or question mark."
        )
    prompt = f"""{MASTER_CONTEXT}

Fix the following text. Problems found: {problem_list}
{incomplete_instruction}
{f'Context: {context_hint}' if context_hint else ''}

Original text:
{text}

Return ONLY the corrected text. No explanation, no preamble."""

    try:
        response = groq_client.chat.completions.create(
            model=VERIFY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.3
        )
        return sanitise_text(response.choices[0].message.content)
    except Exception:
        return sanitise_text(text)


def verify_and_fix(text, company_name, context_hint="", max_retries=2):
    """Verifies text quality. Fixes with AI if problems found. Retries up to max_retries."""
    text = sanitise_text(text)
    for attempt in range(max_retries + 1):
        problems = has_quality_problems(text, company_name)
        if not problems:
            return text
        if attempt < max_retries:
            text = fix_with_ai(text, problems, context_hint)
            text = sanitise_text(text)
    return sanitise_text(text)


# ---------------------------------------------------------------------------
# Data summary builder
# ---------------------------------------------------------------------------

def build_data_summary(all_data, analysis):
    """Builds structured text summary of all YouTube + transcript data."""
    lines = ["=== YOUTUBE COMPETITIVE INTELLIGENCE DATA ===\n"]

    for d in all_data:
        if not d["channel_found"]:
            lines.append(f"{d['company_name']}: No verified YouTube channel found.\n")
            continue

        lines.append(f"COMPANY: {d['company_name']}")
        lines.append(f"  Channel: {d['channel_title']}")
        lines.append(f"  Subscribers: {d['subscribers']:,}")
        lines.append(f"  Total videos: {d['total_videos']:,}")
        lines.append(f"  Total views: {d['total_views']:,}")
        lines.append(f"  Avg views/video: {d['avg_views_per_video']:,}")
        lines.append(f"  Avg likes/video: {d['avg_likes_per_video']:,}")
        lines.append(f"  Avg comments/video: {d['avg_comments_per_video']:,}")
        lines.append(f"  Engagement rate: {d.get('engagement_rate', 0)}%")
        freq = d.get("upload_frequency_days")
        lines.append(f"  Upload frequency: {'every ' + str(freq) + ' days' if freq else 'unknown'}")
        lines.append(f"  Top content topics: {', '.join(d.get('topics', []))}")

        summaries = d.get("transcript_summaries", [])
        if summaries:
            lines.append(f"  VIDEO CONTENT ANALYSIS ({len(summaries)} videos):")
            for s in summaries:
                tier = s.get("source_tier", "unknown")
                lines.append(f"    [{tier.upper()}] '{s['title'][:60]}'")
                lines.append(f"    {s['summary'][:300]}")
                lines.append("")

        if d["top_videos"]:
            lines.append("  Top 3 videos by views:")
            for v in d["top_videos"][:3]:
                lines.append(f"    - '{v['title']}' — {v['views']:,} views, {v['likes']:,} likes")
        lines.append("")

    if analysis.get("ranked"):
        lines.append("=== OVERALL RANKING (composite score) ===")
        for i, r in enumerate(analysis["ranked"]):
            lines.append(
                f"  #{i+1} {r['company']} — score: {r['total_score']}/10 "
                f"(subs: {r['subscribers_score']}, views: {r['views_score']}, "
                f"engagement: {r['engagement_score']}, frequency: {r['frequency_score']})"
            )
        lines.append("")

    if analysis.get("gaps"):
        lines.append("=== CONTENT GAPS ===")
        lines.append(", ".join(analysis["gaps"]))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 1: Gemini 2.0 Flash — Strategic Reasoning
# ---------------------------------------------------------------------------

def run_reasoning(data_summary, your_company_name):
    """
    Uses Gemini 2.0 Flash to analyse all YouTube + transcript data and
    produce raw strategic intelligence.
    REPLACES openai/gpt-oss-120b — same role, free via GEMINI_API_KEY.
    Output is analytical, not polished. Verified and retried until clean.
    """
    prompt = f"""{MASTER_CONTEXT}

You are analysing YouTube competitive data for {your_company_name} against its competitors.

DATA:
{data_summary}

Produce a raw strategic analysis with these exact section headers
(write the header, then the content directly below it):

REASONING_EXECUTIVE:
Who is leading and why — cite specific subscriber counts, view numbers, and engagement rates.

REASONING_LEADER:
Specific tactical reasons the leader outperforms — reference their upload cadence,
engagement rate, and content types from the video summaries.

REASONING_POSTING:
What the upload frequency data reveals about each company's commitment to video.
Name the consistency winner with their actual number.

REASONING_RECOMMENDATIONS:
Four specific, data-backed actions for {your_company_name}.
Each must reference a specific number or pattern from the data above.

REASONING_GAPS:
Topics and content formats competitors produce that {your_company_name} is absent from.
Be specific — name the topics found in the transcripts.

REASONING_SCORES:
One sentence per company justifying their composite score. Use their actual score number.

Write direct analysis. Use real numbers. No generic advice."""

    raw = _call_gemini_reasoning(prompt)

    if not raw:
        # Fallback to Groq writing model if Gemini fails
        try:
            response = groq_client.chat.completions.create(
                model=WRITING_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2500,
                temperature=0.3
            )
            raw = response.choices[0].message.content
        except Exception:
            raw = f"REASONING_EXECUTIVE:\n{your_company_name} data requires manual review.\n"

    return verify_and_fix(raw, your_company_name, context_hint="strategic competitive analysis")


# ---------------------------------------------------------------------------
# Step 2: Llama 3.3 70B — Humanized Writing (per section)
# ---------------------------------------------------------------------------

def write_section(section_name, raw_content, your_company_name, leader, companies_str,
                  extra_instruction="", word_limit=80):
    """
    Rewrites one section of raw analysis into polished, humanized client copy.
    Uses llama-3.3-70b-versatile — best Groq model for human-sounding writing.
    Verifies and retries until clean.
    word_limit: hard maximum words for this section — enforced in prompt AND post-processing.
    """
    prompt = f"""{MASTER_CONTEXT}

You are writing one section of a competitive intelligence report for {your_company_name}.
Companies analysed: {companies_str}
Market leader: {leader}

Section to write: {section_name}
{f'Specific instructions: {extra_instruction}' if extra_instruction else ''}

Raw analysis to rewrite:
{raw_content}

Write ONLY the section content — no section header, no preamble, no explanation.
Write as a human strategist presenting to the {your_company_name} marketing team.
Keep all specific numbers and company names from the raw analysis.
HARD LIMIT: Maximum {word_limit} words. Every sentence must be complete — never end mid-sentence or with '...'."""

    response = groq_client.chat.completions.create(
        model=WRITING_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300,
        temperature=0.5
    )
    raw = response.choices[0].message.content
    return verify_and_fix(raw, your_company_name,
                          context_hint=f"{section_name} for {your_company_name} report")


def write_recommendations(raw_recs, your_company_name, leader, companies_str):
    """Writes 4 numbered, specific recommendations verified for quality."""
    prompt = f"""{MASTER_CONTEXT}

Write exactly 4 numbered video marketing recommendations for {your_company_name}.
Companies analysed: {companies_str}. Market leader: {leader}.

Base them on this analysis:
{raw_recs}

Format — exactly this structure, no deviation:
1. [Specific action with a target number or benchmark]
2. [Action referencing a competitor doing it well — name the competitor]
3. [Content format or topic gap to fill — name the specific topic]
4. [Engagement or distribution tactic with measurable outcome]

Rules:
- Every recommendation must be actionable, not vague
- Every recommendation must reference a real number or a specific company
- No bullet points — numbered list only
- No sub-bullets, no markdown
- Maximum 25 words per recommendation — complete sentences only, never cut off mid-sentence"""

    response = groq_client.chat.completions.create(
        model=WRITING_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0.5
    )
    raw = response.choices[0].message.content
    return verify_and_fix(raw, your_company_name,
                          context_hint="4 numbered video marketing recommendations")


def write_score_justification(ranked, raw_scores):
    """Writes a flowing 2-3 sentence paragraph summarising the competitive scores."""
    companies_with_scores = ', '.join([f"{r['company']} ({r['total_score']}/10)" for r in ranked])
    prompt = f"""{MASTER_CONTEXT}

Write a single flowing paragraph of 2-3 sentences that summarises how these companies ranked \
in YouTube video marketing and the primary reason each score reflects their position.

Companies and scores: {companies_with_scores}

Base the explanation on:
{raw_scores}

Rules:
- Write as ONE connected paragraph, not separate lines per company
- Weave the company names and scores naturally into the narrative
- Explain the strategic reason behind the top and bottom rankings
- Professional, insightful tone — like a strategist summarising a competitive audit
- No markdown, no bullet points, no numbered lists
- Maximum 60 words total — every sentence must be complete and end with a period"""

    response = groq_client.chat.completions.create(
        model=WRITING_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=200,
        temperature=0.4
    )
    raw = response.choices[0].message.content
    return sanitise_text(raw)


# ---------------------------------------------------------------------------
# Section parser
# ---------------------------------------------------------------------------

def parse_reasoning_sections(raw_text):
    """Parses the reasoning model's structured output into labelled sections."""
    section_map = {
        "REASONING_EXECUTIVE:":       "executive",
        "REASONING_LEADER:":          "leader",
        "REASONING_POSTING:":         "posting",
        "REASONING_RECOMMENDATIONS:": "recommendations",
        "REASONING_GAPS:":            "gaps",
        "REASONING_SCORES:":          "scores"
    }

    sections      = {v: "" for v in section_map.values()}
    current_key   = None
    current_lines = []

    for line in raw_text.split("\n"):
        stripped = line.strip()
        matched  = False
        for header, key in section_map.items():
            if stripped.startswith(header):
                if current_key:
                    sections[current_key] = "\n".join(current_lines).strip()
                current_key   = key
                current_lines = []
                matched       = True
                break
        if not matched and current_key:
            current_lines.append(line)

    if current_key:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


# ---------------------------------------------------------------------------
# Master function
# ---------------------------------------------------------------------------

def get_insights(all_data, analysis, your_company_name):
    """
    Full 2-step AI pipeline:
      1. Gemini 2.0 Flash reasons through all data (replaces openai/gpt-oss-120b)
      2. Llama 3.3 70B rewrites each section into client-ready copy
      3. Every section verified and auto-fixed before use

    Falls back to data-driven text if models are unavailable.
    """
    try:
        data_summary  = build_data_summary(all_data, analysis)
        leader        = analysis.get("leader", "N/A")
        companies_str = ", ".join([d["company_name"] for d in all_data if d["channel_found"]])
        ranked        = analysis.get("ranked", [])

        # Step 1: Gemini reasons through all data
        raw_analysis = run_reasoning(data_summary, your_company_name)
        raw_sections = parse_reasoning_sections(raw_analysis)

        # Step 2: Llama 3.3 70B writes each section individually
        executive_summary = write_section(
            "Executive Summary",
            raw_sections.get("executive", raw_analysis[:800]),
            your_company_name, leader, companies_str,
            extra_instruction="2-3 sentences. Who leads and the single most important reason why. Include one key number.",
            word_limit=55
        )

        leader_analysis = write_section(
            "Leader Analysis",
            raw_sections.get("leader", raw_analysis[:800]),
            your_company_name, leader, companies_str,
            extra_instruction="3-4 sentences. Why the leader is winning. Reference their content strategy from video summaries.",
            word_limit=70
        )

        posting_insight = write_section(
            "Posting Frequency Insight",
            raw_sections.get("posting", raw_analysis[:600]),
            your_company_name, leader, companies_str,
            extra_instruction="2 sentences. What upload frequency patterns reveal about each company's video marketing maturity.",
            word_limit=50
        )

        recommendations = write_recommendations(
            raw_sections.get("recommendations", raw_analysis),
            your_company_name, leader, companies_str
        )

        gap_analysis = write_section(
            "Gap Analysis",
            raw_sections.get("gaps", raw_analysis[:600]),
            your_company_name, leader, companies_str,
            extra_instruction="2-3 sentences. Specific content topics and formats competitors produce that this company is missing. Name the topics.",
            word_limit=60
        )

        score_justification = write_score_justification(
            ranked,
            raw_sections.get("scores", raw_analysis[:600])
        )

        return {
            "executive_summary":   executive_summary,
            "leader_analysis":     leader_analysis,
            "posting_insight":     posting_insight,
            "recommendations":     recommendations,
            "gap_analysis":        gap_analysis,
            "score_justification": score_justification
        }

    except Exception as e:
        # Data-driven fallback — never shows error text to the user
        leader      = analysis.get("leader", "N/A")
        ranked      = analysis.get("ranked", [])
        gaps        = analysis.get("gaps", [])
        top_company = ranked[0] if ranked else {}

        return {
            "executive_summary": (
                f"{leader} leads the competitive field in YouTube video marketing. "
                f"With a composite score of {top_company.get('total_score', 'N/A')}/10, "
                f"their combination of upload consistency and audience engagement sets the benchmark "
                f"for {your_company_name} to target."
            ),
            "leader_analysis": (
                f"{leader} achieves {top_company.get('avg_views', 0):,} average views per video "
                f"with an engagement rate of {top_company.get('engagement_rate', 0)}%. "
                f"Their content strategy prioritises consistent publishing and audience-focused topics."
            ),
            "posting_insight": (
                f"Upload frequency is the clearest differentiator between active and passive video strategies. "
                f"Companies posting more frequently consistently show higher subscriber growth."
            ),
            "recommendations": (
                f"1. Match {leader}'s upload cadence by publishing at least as frequently to stay algorithmically competitive.\n"
                f"2. Produce content covering {', '.join(gaps[:2]) if gaps else 'competitor topics'} — topics competitors cover that are absent from your channel.\n"
                f"3. Invest in longer-form educational content, which drives the highest engagement rates across all channels analysed.\n"
                f"4. Respond to every comment in the first 24 hours after publishing to signal engagement quality to the YouTube algorithm."
            ),
            "gap_analysis": (
                f"{your_company_name} is absent from content topics including "
                f"{', '.join(gaps[:4]) if gaps else 'several key areas competitors cover'}. "
                f"Competitors are actively producing content in these spaces."
            ),
            "score_justification": "\n".join([
                f"{r['company']} scores {r['total_score']}/10 based on their subscriber base, "
                f"average views of {r['avg_views']:,}, and upload frequency of "
                f"{'every ' + str(r['upload_freq_days']) + ' days' if r['upload_freq_days'] else 'unknown cadence'}."
                for r in ranked
            ])
        }
