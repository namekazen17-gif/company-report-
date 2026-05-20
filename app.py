"""
app.py
------
Flask application — all routes.

Routes:
  GET  /                  → input form (index.html)
  POST /generate          → Phase 1: find YouTube channels only
  GET  /progress-channels → SSE stream for channel-finding phase
  GET  /verify-channels   → show found channels, let user verify / edit
  POST /confirm-channels  → user confirms channels → starts Phase 2
  GET  /progress          → SSE stream for full analysis
  GET  /report            → renders completed report (report.html)
  GET  /download          → streams PPTX file download
  GET  /quota-exhausted   → shown when all YouTube API keys hit daily quota

Pipeline:
  Phase 1: youtube_fetcher (channels only) → /verify-channels
  Phase 2: transcriber → analyser → insights → quality_gate → report_builder
"""

import os
import re
import json
import time
import threading
from flask import Flask, render_template, request, send_file, session, Response, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

from youtube_fetcher import fetch_company_data, build_company_context, QuotaExhaustedError, refetch_channel_from_url
from transcriber import transcribe_company_videos
from analyser import analyse_all
from insights import get_insights, sanitise_text, WRITING_MODEL
from report_builder import build_pptx
from groq import Groq

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-fallback-change-this")

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# In-memory job store
jobs = {}


def fmt_num_filter(n):
    try:
        n = int(n)
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(n)
    except Exception:
        return str(n)


app.jinja_env.filters["fmt_num"] = fmt_num_filter


# ---------------------------------------------------------------------------
# Pre-PPTX quality gate
# ---------------------------------------------------------------------------

BAD_SIGNALS = [
    "check your groq", "api key", "groq_api", "unavailable — ",
    "please verify", "manually review", "error code", "as an ai",
    "here is a summary", "here are the", "main topic:", "target audience:",
    "**", "##", "```", "based on the data provided"
]


def section_needs_fix(text):
    """Returns True if the section contains fallback/error/markdown content."""
    lower = text.lower()
    return any(signal in lower for signal in BAD_SIGNALS) or len(text.split()) < 15


def regenerate_section(section_key, all_data, analysis, your_company, leader):
    ranked = analysis.get("ranked", [])
    gaps   = analysis.get("gaps", [])

    data_brief = []
    for d in all_data:
        if not d["channel_found"]:
            continue
        data_brief.append(
            f"{d['company_name']}: {d['subscribers']:,} subscribers, "
            f"{d['avg_views_per_video']:,} avg views, "
            f"{d.get('engagement_rate', 0)}% engagement, "
            f"posts every {d.get('upload_frequency_days', 'unknown')} days"
        )
    brief_str = "\n".join(data_brief)

    section_instructions = {
        "executive_summary": (
            f"Write 2-3 sentences identifying who leads in YouTube video marketing among these companies "
            f"and the single most important data-backed reason why. Include one specific number."
        ),
        "leader_analysis": (
            f"Write 3-4 sentences explaining why {leader} is the market leader in video marketing. "
            f"Reference their specific metrics and what their content strategy reveals."
        ),
        "posting_insight": (
            f"Write 2 sentences about what the upload frequency data reveals about each company's "
            f"video marketing commitment. Name the most consistent company."
        ),
        "recommendations": (
            f"Write exactly 4 numbered recommendations for {your_company} to improve their YouTube "
            f"video marketing. Format: 1. [action] 2. [action] 3. [action] 4. [action]. "
            f"Each must be specific and reference a real number or competitor."
        ),
        "gap_analysis": (
            f"Write 2-3 sentences about content topics competitors cover that {your_company} is missing. "
            f"Name the specific gaps: {', '.join(gaps[:5]) if gaps else 'competitor topic areas'}."
        ),
        "score_justification": (
            "Write one sentence per company explaining their composite score."
        )
    }

    if section_key == "score_justification":
        score_parts = ", ".join([f"{r['company']} {r['total_score']}/10" for r in ranked])
        instruction = f"Write one sentence per company explaining their composite score. Scores: {score_parts}."
    else:
        instruction = section_instructions.get(
            section_key,
            f"Write a professional 2-3 sentence analysis for the {section_key} section of a competitive report."
        )

    prompt = f"""You are a senior video marketing strategist writing a client report for {your_company}.

Data:
{brief_str}

Task: {instruction}

Rules — no markdown (no **, ##, *, _), no AI phrases, write as a human expert.
Return ONLY the section text, nothing else."""

    try:
        response = groq_client.chat.completions.create(
            model=WRITING_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.5
        )
        return sanitise_text(response.choices[0].message.content)
    except Exception:
        top = ranked[0] if ranked else {}
        return (
            f"{leader} leads with a score of {top.get('total_score', 'N/A')}/10, "
            f"driven by {top.get('avg_views', 0):,} average views per video. "
            f"{your_company} should benchmark against their upload frequency and content depth."
        )


def run_quality_gate(insights, all_data, analysis, your_company):
    leader   = analysis.get("leader", "N/A")
    sections = [
        "executive_summary", "leader_analysis", "posting_insight",
        "recommendations", "gap_analysis", "score_justification"
    ]
    for section in sections:
        text = insights.get(section, "")
        if section_needs_fix(text):
            insights[section] = regenerate_section(
                section, all_data, analysis, your_company, leader
            )
    return insights


# ---------------------------------------------------------------------------
# Phase 1: Channel-finding worker
# ---------------------------------------------------------------------------

def run_channel_finding(job_id, your_company, competitors):
    """
    Phase 1: Only find and verify YouTube channels.
    Stores channel results in jobs[job_id]["channel_data"] when done.

    QuotaExhaustedError: sets quota_exhausted flag so the SSE route can
    signal loading.html to redirect to /quota-exhausted.
    """

    def push(msg):
        jobs[job_id]["channel_status"].append(msg)

    try:
        all_names = [your_company] + competitors

        push("🔍 Building industry context for all companies...")
        company_context = build_company_context(all_names)
        push(f"   Industry identified: {company_context.get('industry', 'N/A')}")

        all_data = []
        for name in all_names:
            push(f"   Searching YouTube channel for: {name}")
            data = fetch_company_data(name, company_context)
            all_data.append(data)
            if data["channel_found"]:
                verified_tag = "✓ verified" if data.get("channel_verified") else "⚠ unverified"
                push(f"   ✅ {name} → {data['channel_title']} ({verified_tag})")
            else:
                push(f"   ⚠️  {name} → No verified channel found")
            time.sleep(0.2)

        found     = [d for d in all_data if d["channel_found"]]
        not_found = [d for d in all_data if not d["channel_found"]]
        push(f"✅ Found {len(found)} channel(s). {len(not_found)} could not be verified.")
        push("__CHANNELS_READY__")

        jobs[job_id]["channel_data"]    = all_data
        jobs[job_id]["company_context"] = company_context
        jobs[job_id]["channels_done"]   = True

    except QuotaExhaustedError as e:
        # All YouTube API keys exhausted — signal the dedicated error page
        jobs[job_id]["quota_exhausted"] = True
        jobs[job_id]["channels_done"]   = True
        push(f"❌ YouTube API quota exhausted: {str(e)}")

    except Exception as e:
        jobs[job_id]["channel_error"]  = str(e)
        jobs[job_id]["channels_done"]  = True
        push(f"❌ Error: {str(e)}")


# ---------------------------------------------------------------------------
# Phase 2: Full analysis worker (after channel confirmation)
# ---------------------------------------------------------------------------

def run_analysis(job_id, your_company, competitors, all_data):
    """
    Phase 2: Full pipeline — transcribe, analyse, insights, quality gate, PPTX.
    Receives confirmed all_data (with possibly user-corrected channel links).

    QuotaExhaustedError: sets quota_exhausted flag so the SSE route can
    signal loading.html to redirect to /quota-exhausted.
    """

    def push(msg):
        jobs[job_id]["status"].append(msg)

    try:
        # Step 2: Transcribe / analyse videos
        push("🎬 Downloading and analysing video content...")
        push("   (3-tier: transcript → description → Gemini AI watch)")
        for d in all_data:
            if not d["channel_found"]:
                continue
            push(f"   Processing videos for: {d['company_name']}")

            def make_cb(job_id):
                def cb(msg):
                    jobs[job_id]["status"].append(msg)
                return cb

            transcribe_company_videos(d, max_videos=5, progress_callback=make_cb(job_id))
            push(f"   ✅ {d['company_name']} — {len(d.get('transcript_summaries', []))} videos analysed")

        push("✅ Video content analysis complete.")

        # Step 3: Statistical analysis
        push("📊 Running competitive scoring and gap analysis...")
        analysis = analyse_all(all_data, your_company)
        push(f"✅ Ranked {len(analysis.get('ranked', []))} companies. Leader: {analysis.get('leader', 'N/A')}")

        # Step 4: AI insights
        push("🧠 Running strategic reasoning (GPT OSS 120B)...")
        push("✍️  Writing client-ready copy (Llama 3.3 70B)...")
        insights = get_insights(all_data, analysis, your_company)
        push("✅ AI insights complete.")

        # Step 5: Quality gate
        push("🔎 Running quality verification on all report sections...")
        insights = run_quality_gate(insights, all_data, analysis, your_company)
        push("✅ Quality check passed.")

        # Step 6: Build PPTX
        push("📑 Building PowerPoint report (11 slides)...")
        pptx_buf = build_pptx(
            your_company=your_company,
            competitors=competitors,
            all_data=all_data,
            analysis=analysis,
            insights=insights
        )
        push("✅ PowerPoint ready.")
        push("🎉 Report complete! Redirecting...")

        jobs[job_id]["result"] = {
            "your_company": your_company,
            "competitors":  competitors,
            "all_data":     all_data,
            "analysis":     analysis,
            "insights":     insights,
            "pptx_bytes":   pptx_buf.read()
        }
        jobs[job_id]["done"] = True

    except QuotaExhaustedError as e:
        # All YouTube API keys exhausted — signal the dedicated error page
        jobs[job_id]["quota_exhausted"] = True
        jobs[job_id]["done"]            = True
        push(f"❌ YouTube API quota exhausted: {str(e)}")

    except Exception as e:
        jobs[job_id]["error"]  = str(e)
        jobs[job_id]["done"]   = True
        push(f"❌ Error: {str(e)}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    """Phase 1 entry: collect companies, start channel-finding only."""
    your_company = request.form.get("your_company", "").strip()
    competitors  = [
        request.form.get("competitor1", "").strip(),
        request.form.get("competitor2", "").strip(),
        request.form.get("competitor3", "").strip(),
        request.form.get("competitor4", "").strip(),
    ]
    competitors = [c for c in competitors if c]

    if not your_company:
        return render_template("index.html", error="Please enter your company name.")
    if not competitors:
        return render_template("index.html", error="Please enter at least one competitor.")

    job_id = f"{your_company}_{int(time.time())}"
    jobs[job_id] = {
        # Phase 1
        "channel_status":  [],
        "channel_data":    None,
        "channels_done":   False,
        "channel_error":   None,
        "company_context": None,
        # Phase 2
        "status": [],
        "done":   False,
        "result": None,
        "error":  None,
        # Quota flag (set by either phase worker)
        "quota_exhausted": False,
    }
    session["job_id"]       = job_id
    session["your_company"] = your_company
    session["competitors"]  = competitors

    t = threading.Thread(
        target=run_channel_finding,
        args=(job_id, your_company, competitors),
        daemon=True
    )
    t.start()

    return render_template(
        "loading.html",
        your_company=your_company,
        competitors=competitors,
        phase="channels",
        job_id=job_id
    )


@app.route("/progress-channels")
def progress_channels():
    """SSE stream for Phase 1 (channel finding)."""
    job_id = session.get("job_id")
    if not job_id or job_id not in jobs:
        def empty():
            yield 'data: {"msg": "No job found.", "done": true, "error": true}\n\n'
        return Response(empty(), mimetype="text/event-stream")

    def generate_events():
        sent = 0
        while True:
            job      = jobs.get(job_id, {})
            messages = job.get("channel_status", [])
            while sent < len(messages):
                payload = json.dumps({"msg": messages[sent], "done": False})
                yield f"data: {payload}\n\n"
                sent += 1
            if job.get("channels_done"):
                # Check for quota exhaustion first
                if job.get("quota_exhausted"):
                    payload = json.dumps({
                        "msg":             "❌ All YouTube API keys have exhausted their daily quota.",
                        "done":            True,
                        "error":           False,
                        "quota_exhausted": True
                    })
                else:
                    error = job.get("channel_error")
                    payload = json.dumps({
                        "msg":   f"❌ {error}" if error else "__CHANNELS_READY__",
                        "done":  True,
                        "error": bool(error)
                    })
                yield f"data: {payload}\n\n"
                break
            time.sleep(0.5)

    return Response(
        generate_events(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/verify-channels")
def verify_channels():
    """Show found channels to user for verification / editing."""
    job_id = session.get("job_id")
    if not job_id or job_id not in jobs:
        return redirect(url_for("index"))

    job = jobs[job_id]
    if not job.get("channels_done") or job.get("channel_error"):
        return redirect(url_for("index"))

    all_data     = job["channel_data"]
    your_company = session.get("your_company", "")
    competitors  = session.get("competitors", [])

    return render_template(
        "verify_channels.html",
        all_data=all_data,
        your_company=your_company,
        competitors=competitors
    )


@app.route("/confirm-channels", methods=["POST"])
def confirm_channels():
    """
    User confirms (or corrects) channel links.
    Updates all_data with any user-edited channel URLs, then starts Phase 2.
    """
    job_id = session.get("job_id")
    if not job_id or job_id not in jobs:
        return redirect(url_for("index"))

    job          = jobs[job_id]
    all_data     = job["channel_data"]
    your_company = session.get("your_company", "")
    competitors  = session.get("competitors", [])

    # Apply any user-edited channel URLs
    for i, d in enumerate(all_data):
        company_key = d["company_name"].replace(" ", "_").lower()
        edited_url  = request.form.get(f"channel_url_{company_key}", "").strip()

        if edited_url and edited_url != d.get("channel_url_display", ""):
            # User corrected this channel — refetch ALL data from the new URL.
            # Gemini verifies accessibility only (no industry check — user is trusted).
            # This replaces top_videos, recent_videos, stats — everything Phase 2 uses.
            fresh = refetch_channel_from_url(edited_url, d["company_name"])
            if fresh:
                all_data[i] = fresh
            else:
                # Refetch failed — keep old data but mark as user-confirmed
                d["channel_url_override"] = edited_url
                d["user_confirmed"]       = True
        else:
            d["user_confirmed"] = True

    # Reset Phase 2 tracking
    jobs[job_id]["status"]         = []
    jobs[job_id]["done"]           = False
    jobs[job_id]["result"]         = None
    jobs[job_id]["error"]          = None
    jobs[job_id]["quota_exhausted"] = False

    # Start Phase 2 in background
    t = threading.Thread(
        target=run_analysis,
        args=(job_id, your_company, competitors, all_data),
        daemon=True
    )
    t.start()

    return render_template(
        "loading.html",
        your_company=your_company,
        competitors=competitors,
        phase="analysis",
        job_id=job_id
    )


@app.route("/progress")
def progress():
    """SSE stream for Phase 2 (full analysis)."""
    job_id = session.get("job_id")
    if not job_id or job_id not in jobs:
        def empty():
            yield 'data: {"msg": "No job found.", "done": true, "error": true}\n\n'
        return Response(empty(), mimetype="text/event-stream")

    def generate_events():
        sent = 0
        while True:
            job      = jobs.get(job_id, {})
            messages = job.get("status", [])
            while sent < len(messages):
                payload = json.dumps({"msg": messages[sent], "done": False})
                yield f"data: {payload}\n\n"
                sent += 1
            if job.get("done"):
                # Check for quota exhaustion first
                if job.get("quota_exhausted"):
                    payload = json.dumps({
                        "msg":             "❌ All YouTube API keys have exhausted their daily quota.",
                        "done":            True,
                        "error":           False,
                        "quota_exhausted": True
                    })
                else:
                    error = job.get("error")
                    payload = json.dumps({
                        "msg":   f"❌ {error}" if error else "done",
                        "done":  True,
                        "error": bool(error)
                    })
                yield f"data: {payload}\n\n"
                break
            time.sleep(0.5)

    return Response(
        generate_events(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.route("/quota-exhausted")
def quota_exhausted():
    """Shown when all YouTube API keys have hit their daily quota."""
    return render_template("quota_exhausted.html")


@app.route("/report")
def report():
    job_id = request.args.get("job_id") or session.get("job_id")
    if not job_id or job_id not in jobs:
        return redirect(url_for("index"))

    job = jobs[job_id]
    if not job.get("done") or job.get("error"):
        return redirect(url_for("index"))

    data                    = job["result"]
    session["report_ready"] = True

    return render_template(
        "report.html",
        your_company=data["your_company"],
        competitors=data["competitors"],
        all_data=data["all_data"],
        analysis=data["analysis"],
        insights=data["insights"],
        fmt_num=fmt_num_filter
    )


@app.route("/download")
def download():
    job_id = request.args.get("job_id") or session.get("job_id")
    if not job_id or job_id not in jobs:
        return "No report found. Please generate a report first.", 400

    job = jobs[job_id]
    if not job.get("done") or not job.get("result"):
        return "Report not ready yet.", 400

    data = job["result"]
    import io
    buf = io.BytesIO(data["pptx_bytes"])
    buf.seek(0)

    filename = f"video_intelligence_{data['your_company'].replace(' ', '_')}.pptx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
