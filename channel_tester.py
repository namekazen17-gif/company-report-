"""
channel_tester.py
-----------------
Standalone channel discovery tester — pipeline IDENTICAL to the main project.
Includes Step 0 industry context pre-pass (same as fetch_all_companies in youtube_fetcher.py).

Pipeline:
  Step 0 : One Gemini call analyses ALL companies together → builds industry context
  Step 1 : Search "[company] official" → Gemini verifies WITH context
  Step 2-4: Gemini generates 3 context-aware alternate queries → Gemini verifies each
  Step 5 : Gemini identifies exact handle → direct forHandle lookup (NO re-verify)
  Step 6 : Video-based fallback → Gemini verifies with context
  Step 7 : Give up → unverified

APIs: YouTube Data API v3 + Gemini 2.0 Flash only. No Groq.

Run:  python channel_tester.py
Open: http://127.0.0.1:5001
"""

import os, re, json, threading, requests
from flask import Flask, render_template_string, request, Response, session

# ── Load .env ────────────────────────────────────────────────────────────────
def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
YT_BASE         = "https://www.googleapis.com/youtube/v3"

app = Flask(__name__)
app.secret_key = "channel-tester-key"
jobs = {}

# ─────────────────────────────────────────────────────────────────────────────
# Gemini helper
# ─────────────────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────────────────
# YouTube helper
# ─────────────────────────────────────────────────────────────────────────────

def _yt(endpoint, params):
    """YouTube Data API GET. Raises RuntimeError with readable message on failure."""
    params["key"] = YOUTUBE_API_KEY
    r    = requests.get(f"{YT_BASE}/{endpoint}", params=params, timeout=10)
    data = r.json()
    if "error" in data:
        code = data["error"].get("code", "?")
        msg  = data["error"].get("message", "Unknown error")
        raise RuntimeError(
            f"YouTube API bad request ({code}) — {msg}. "
            f"Please renew the API key. Verify YOUTUBE_API_KEY in .env and check quota at "
            f"console.cloud.google.com/apis/api/youtube.googleapis.com"
        )
    return data

# ─────────────────────────────────────────────────────────────────────────────
# Step 0 — Industry context pre-pass  (same as build_company_context in project)
# ─────────────────────────────────────────────────────────────────────────────

def build_company_context(all_company_names, log):
    """
    ONE Gemini call before any searching.
    Analyses ALL company names together to understand:
    - shared industry/sector
    - expected YouTube content type
    - known handles for any of them
    - ambiguous names that could match wrong channels

    This context is injected into every subsequent verification call.
    Prevents matching a company name to a regional comedy or news channel.
    """
    company_list = ", ".join(all_company_names)
    log(f"Step 0: Gemini analysing all companies together: {company_list}")

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
        "industry":        "technology or business software",
        "content_type":    "product demos, tutorials, and business content",
        "known_channels":  "",
        "ambiguous_names": "",
        "raw":             raw
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

    log(f"   Industry: {context['industry']}")
    log(f"   Content type: {context['content_type']}")
    if context["known_channels"] and context["known_channels"] != "NONE":
        log(f"   Known channels: {context['known_channels']}")
    if context["ambiguous_names"] and context["ambiguous_names"] != "NONE":
        log(f"   Ambiguous names flagged: {context['ambiguous_names']}")

    return context

# ─────────────────────────────────────────────────────────────────────────────
# Gemini-powered verification — context-aware  (same as project)
# ─────────────────────────────────────────────────────────────────────────────

def _verify(channel_name, description, company_name, context=None):
    """Gemini YES/NO with full industry context — same logic as project."""
    context_block = ""
    if context:
        context_block = (
            f"\nIndustry context: {context.get('industry', '')}"
            f"\nExpected content type: {context.get('content_type', '')}"
            f"\nAmbiguous names to watch for: {context.get('ambiguous_names', 'none')}\n"
        )

    prompt = f"""You are verifying YouTube channel ownership for a competitive intelligence report.
{context_block}
Company we are looking for: {company_name}
YouTube channel name found: {channel_name}
YouTube channel description: {description[:500] if description else "No description available"}

Does this YouTube channel belong to {company_name}?

Rules:
- Answer only YES or NO
- YES means this is the official or primary YouTube channel of {company_name}
- NO means this is a fan channel, comedy channel, news channel, or a different company entirely
- If the channel language, content, or description clearly does not match the expected industry above, answer NO
- If you are not sure, answer NO

Answer:"""

    ans = _gemini(prompt)
    return ans.strip().upper().startswith("YES") if ans else True

def _alternate_queries(company_name, context=None):
    """Gemini generates 3 context-aware alternate search queries."""
    context_block = ""
    if context:
        context_block = (
            f"Industry: {context.get('industry', '')}\n"
            f"Expected content: {context.get('content_type', '')}\n"
        )

    prompt = f"""A YouTube search for "{company_name} official" did not find the correct channel.

{context_block}
Generate exactly 3 alternative YouTube search queries to find the official
YouTube channel for: {company_name}

Consider:
- Their industry: {context.get('industry', '') if context else ''}
- Common channel naming patterns in this sector
- Product names, sub-brands, or abbreviations they might use

Return ONLY the 3 search queries, one per line, no numbering, no explanation."""

    raw = _gemini(prompt)
    if not raw:
        industry = context.get("industry", "software") if context else "software"
        return [f"{company_name} {industry}", f"{company_name} tutorials",
                f"{company_name} official channel"]
    return [l.strip() for l in raw.split("\n") if l.strip()][:3]

def _handle_lookup(company_name, context=None):
    """
    Gemini identifies exact handle → direct forHandle YouTube lookup.
    NO re-verification — Gemini confirmed + YouTube confirmed = double-confirmed.
    """
    context_hint = f" (operates in: {context.get('industry', '')})" if context else ""
    prompt = (
        f"What is the official YouTube channel handle for the company "
        f"'{company_name}'{context_hint}? "
        f"Return ONLY the handle in format @HandleName, nothing else. "
        f"If unsure, return UNKNOWN."
    )
    raw = _gemini(prompt)
    if not raw or "UNKNOWN" in raw.upper():
        return None, None

    match  = re.search(r'@[\w\-]+', raw)
    handle = match.group(0).lstrip("@") if match else raw.strip().split()[0].lstrip("@")
    if not handle:
        return None, None

    try:
        data  = _yt("channels", {"part": "snippet", "forHandle": handle})
        items = data.get("items", [])
        if items:
            # YouTube confirmed handle — trust it, NO re-verify
            return items[0]["id"], items[0]["snippet"]["title"]

        # handle not found directly — try as search
        results = _search_channel(f"@{handle}", 1)
        if results:
            cid, ctitle, desc = results[0]
            full = _fetch_desc(cid) or desc
            if _verify(ctitle, full, company_name, context):
                return cid, ctitle
    except Exception:
        pass

    return None, None

def _video_fallback(company_name, context=None):
    """Search videos → extract channels → Gemini-verify with context."""
    try:
        data = _yt("search", {"part": "snippet", "q": f"{company_name} official video",
                               "type": "video", "maxResults": 5})
        seen = set()
        for item in data.get("items", []):
            cid    = item["snippet"]["channelId"]
            ctitle = item["snippet"]["channelTitle"]
            if cid in seen:
                continue
            seen.add(cid)
            desc = _fetch_desc(cid)
            if _verify(ctitle, desc, company_name, context):
                return cid, ctitle
    except Exception:
        pass
    return None, None

def _search_channel(query, max_results=2):
    try:
        data = _yt("search", {"part": "snippet", "q": query,
                               "type": "channel", "maxResults": max_results})
        return [(i["snippet"]["channelId"], i["snippet"]["title"],
                 i["snippet"].get("description", ""))
                for i in data.get("items", [])]
    except Exception:
        return []

def _fetch_desc(channel_id):
    try:
        data  = _yt("channels", {"part": "snippet", "id": channel_id})
        items = data.get("items", [])
        return items[0]["snippet"].get("description", "") if items else ""
    except Exception:
        return ""

def _fetch_subs(channel_id):
    try:
        data  = _yt("channels", {"part": "statistics", "id": channel_id})
        items = data.get("items", [])
        return int(items[0].get("statistics", {}).get("subscriberCount", 0)) if items else 0
    except Exception:
        return 0

# ─────────────────────────────────────────────────────────────────────────────
# Full pipeline per company
# ─────────────────────────────────────────────────────────────────────────────

def discover_channel(company_name, log, context=None):
    result = {"company": company_name, "found": False, "channel_title": "",
              "channel_id": "", "subscribers": 0, "step_used": "", "error": ""}
    try:
        # Step 1 — primary search
        log(f"[{company_name}] Step 1: Searching '{company_name} official'...")
        for cid, ctitle, desc in _search_channel(f"{company_name} official", 2):
            full = _fetch_desc(cid) or desc
            log(f"[{company_name}]   Candidate: {ctitle} — verifying with Gemini (context-aware)...")
            if _verify(ctitle, full, company_name, context):
                log(f"[{company_name}]   ✅ Verified via Step 1")
                result.update({"found": True, "channel_title": ctitle, "channel_id": cid,
                                "step_used": "Step 1 — primary search",
                                "subscribers": _fetch_subs(cid)})
                return result
            log(f"[{company_name}]   ❌ Gemini rejected: {ctitle}")

        # Steps 2-4 — Gemini alternate queries (context-aware)
        log(f"[{company_name}] Steps 2-4: Gemini generating 3 context-aware alternate queries...")
        for i, query in enumerate(_alternate_queries(company_name, context), 1):
            log(f"[{company_name}]   Alternate {i}: '{query}'")
            for cid, ctitle, desc in _search_channel(query, 2):
                full = _fetch_desc(cid) or desc
                log(f"[{company_name}]     Candidate: {ctitle} — verifying...")
                if _verify(ctitle, full, company_name, context):
                    log(f"[{company_name}]   ✅ Verified via alternate query {i}")
                    result.update({"found": True, "channel_title": ctitle, "channel_id": cid,
                                   "step_used": f"Step 2-4 — alternate: '{query}'",
                                   "subscribers": _fetch_subs(cid)})
                    return result
                log(f"[{company_name}]     ❌ Rejected: {ctitle}")

        # Step 5 — Gemini handle lookup (NO re-verify)
        log(f"[{company_name}] Step 5: Gemini identifying exact channel handle...")
        cid, ctitle = _handle_lookup(company_name, context)
        if cid:
            subs = _fetch_subs(cid)
            log(f"[{company_name}]   ✅ Gemini handle → YouTube confirmed → {ctitle}")
            result.update({"found": True, "channel_title": ctitle, "channel_id": cid,
                            "step_used": "Step 5 — Gemini handle lookup (no re-verify)",
                            "subscribers": subs})
            return result
        log(f"[{company_name}]   ❌ Handle lookup failed")

        # Step 6 — video fallback
        log(f"[{company_name}] Step 6: Video-based fallback...")
        cid, ctitle = _video_fallback(company_name, context)
        if cid:
            log(f"[{company_name}]   ✅ Found via video fallback: {ctitle}")
            result.update({"found": True, "channel_title": ctitle, "channel_id": cid,
                            "step_used": "Step 6 — video fallback",
                            "subscribers": _fetch_subs(cid)})
            return result
        log(f"[{company_name}]   ❌ Video fallback failed")

        log(f"[{company_name}] ❌ All steps exhausted — channel not found")
        result["error"] = "All 6 discovery steps exhausted."

    except RuntimeError as e:
        log(f"[{company_name}] ❌ API ERROR: {e}")
        result["error"] = str(e)
    except Exception as e:
        log(f"[{company_name}] ❌ Unexpected error: {e}")
        result["error"] = str(e)

    return result

# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────

def _run_job(job_id, companies):
    job = jobs[job_id]

    def log(msg):
        job["log"].append(msg)

    log("🚀 Starting channel discovery pipeline...")
    log(f"Companies: {', '.join(companies)}")
    log("─" * 55)

    # Step 0 — industry context pre-pass (ONE call for ALL companies)
    try:
        context = build_company_context(companies, log)
    except RuntimeError as e:
        log(f"❌ API ERROR during context build: {e}")
        job["done"] = True
        return
    log("─" * 55)

    for company in companies:
        r = discover_channel(company, log, context)
        job["results"].append(r)
        log("─" * 55)

    log("✅ All companies processed.")
    job["done"] = True

# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Channel Discovery Tester</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',sans-serif;background:#f0f2f5;padding:30px 20px}
  .wrap{max-width:900px;margin:0 auto}
  h1{color:#1b2a4a;font-size:24px;margin-bottom:4px}
  .sub{color:#666;font-size:13px;margin-bottom:24px}
  .card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 12px rgba(0,0,0,.08);margin-bottom:20px}
  label{display:block;font-weight:600;color:#1b2a4a;margin-bottom:8px;font-size:14px}
  input[type=text]{width:100%;padding:9px 13px;border:1.5px solid #dde3ee;border-radius:7px;font-size:14px;outline:none}
  input:focus{border-color:#2d7dd2}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
  button{background:#2d7dd2;color:#fff;border:none;padding:11px 30px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{background:#1b5fa8} button:disabled{background:#aaa;cursor:not-allowed}
  .log-box{background:#0f1923;border-radius:8px;padding:16px;height:340px;overflow-y:auto;
    font-family:'Courier New',monospace;font-size:11.5px;color:#c8d8e8;margin-bottom:16px}
  .ll{padding:2px 0;line-height:1.6}
  .ll.ok{color:#4eca8b} .ll.err{color:#e76f51} .ll.sep{color:#334455} .ll.ctx{color:#f4a261}
  .rg{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
  .rc{border-radius:10px;padding:16px;border:2px solid}
  .rc.found{border-color:#2d7dd2;background:#f0f7ff}
  .rc.notfound{border-color:#e76f51;background:#fff5f2}
  .rc-co{font-weight:700;font-size:14px;color:#1b2a4a;margin-bottom:6px}
  .rc-st{font-size:13px;font-weight:600;margin-bottom:5px}
  .found .rc-st{color:#2d7dd2} .notfound .rc-st{color:#e76f51}
  .rc-d{font-size:12px;color:#555;line-height:1.7}
  .rc-s{font-size:11px;color:#888;margin-top:5px;font-style:italic}
  .sp{display:inline-block;width:13px;height:13px;border:2px solid #fff;
    border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite;margin-right:7px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .hidden{display:none}
  .warn{background:#fff8e1;border:1px solid #f0c040;border-radius:8px;
    padding:11px 15px;font-size:13px;color:#7a5c00;margin-bottom:16px}
  .ctx-box{background:#1a2a1a;border-radius:8px;padding:12px 16px;margin-bottom:12px;
    font-size:12px;color:#a8d8a8;border-left:3px solid #4eca8b}
</style>
</head>
<body>
<div class="wrap">
  <h1>🔍 YouTube Channel Discovery Tester</h1>
  <p class="sub">Full pipeline with Step 0 industry context pre-pass — identical to main project.</p>

  {% if not yt_key or not gem_key %}
  <div class="warn">⚠️ <strong>Missing API keys:</strong>
    {% if not yt_key %} YOUTUBE_API_KEY {% endif %}
    {% if not gem_key %} GEMINI_API_KEY {% endif %}
    — place this file in same folder as your .env
  </div>
  {% endif %}

  <div class="card">
    <label>Enter up to 5 company names:</label>
    <div class="grid">
      <input type="text" id="c1" placeholder="e.g. Canva"/>
      <input type="text" id="c2" placeholder="e.g. Figma"/>
      <input type="text" id="c3" placeholder="e.g. Adobe"/>
      <input type="text" id="c4" placeholder="e.g. PicsArt"/>
      <input type="text" id="c5" placeholder="e.g. Visme"/>
    </div>
    <button id="btn" onclick="startTest()">▶ Run Discovery Test</button>
  </div>

  <div class="card hidden" id="logCard">
    <label>Live Pipeline Log</label>
    <div class="log-box" id="logBox"></div>
  </div>

  <div class="card hidden" id="resCard">
    <label>Results</label>
    <div class="rg" id="resGrid"></div>
  </div>
</div>

<script>
function fmt(n){
  if(n>=1e6)return(n/1e6).toFixed(1)+'M';
  if(n>=1e3)return(n/1e3).toFixed(1)+'K';
  return String(n);
}
function addLog(msg){
  const box=document.getElementById('logBox');
  const d=document.createElement('div');
  d.className='ll'+(msg.includes('✅')?' ok':msg.includes('❌')||msg.includes('ERROR')?' err':
    msg.startsWith('─')?' sep':msg.includes('Industry:')||msg.includes('Step 0')?' ctx':'');
  d.textContent=msg;
  box.appendChild(d);
  box.scrollTop=box.scrollHeight;
}
function renderResults(results){
  const g=document.getElementById('resGrid');
  g.innerHTML='';
  results.forEach(r=>{
    const c=document.createElement('div');
    c.className='rc '+(r.found?'found':'notfound');
    c.innerHTML=`<div class="rc-co">${r.company}</div>
      <div class="rc-st">${r.found?'✅ Channel Found':'❌ Not Found'}</div>
      <div class="rc-d">${r.found?
        `<strong>Channel:</strong> ${r.channel_title}<br>
         <strong>Subscribers:</strong> ${fmt(r.subscribers)}<br>
         <strong>ID:</strong> <a href="https://youtube.com/channel/${r.channel_id}"
           target="_blank" style="color:#2d7dd2">${r.channel_id}</a>`:
        `<strong>Error:</strong> ${r.error||'All steps exhausted'}`}</div>
      ${r.step_used?`<div class="rc-s">${r.step_used}</div>`:''}`;
    g.appendChild(c);
  });
  document.getElementById('resCard').classList.remove('hidden');
}
function startTest(){
  const companies=['c1','c2','c3','c4','c5']
    .map(id=>document.getElementById(id).value.trim()).filter(v=>v);
  if(!companies.length){alert('Enter at least one company.');return;}
  const btn=document.getElementById('btn');
  btn.disabled=true;
  btn.innerHTML='<span class="sp"></span>Running...';
  document.getElementById('logCard').classList.remove('hidden');
  document.getElementById('logBox').innerHTML='';
  document.getElementById('resCard').classList.add('hidden');

  fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({companies})})
  .then(r=>r.json())
  .then(data=>{
    if(!data.job_id){alert('Failed to start.');return;}
    const es=new EventSource(`/stream/${data.job_id}`);
    es.onmessage=function(e){
      const msg=JSON.parse(e.data);
      if(msg.line)addLog(msg.line);
      if(msg.done){
        es.close();
        btn.disabled=false;
        btn.innerHTML='▶ Run Discovery Test';
        if(msg.results)renderResults(msg.results);
      }
    };
    es.onerror=function(){
      es.close();
      btn.disabled=false;
      btn.innerHTML='▶ Run Discovery Test';
      addLog('❌ Connection error');
    };
  });
}
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML,
        yt_key=bool(YOUTUBE_API_KEY), gem_key=bool(GEMINI_API_KEY))

@app.route("/start", methods=["POST"])
def start():
    import time
    data      = request.get_json()
    companies = [c.strip() for c in data.get("companies", []) if c.strip()][:5]
    if not companies:
        return {"error": "No companies"}, 400
    job_id       = f"job_{int(time.time()*1000)}"
    jobs[job_id] = {"log": [], "done": False, "results": []}
    threading.Thread(target=_run_job, args=(job_id, companies), daemon=True).start()
    return {"job_id": job_id}

@app.route("/stream/<job_id>")
def stream(job_id):
    if job_id not in jobs:
        def _e():
            yield 'data: {"done":true,"results":[]}\n\n'
        return Response(_e(), mimetype="text/event-stream")

    def _generate():
        import time
        sent = 0
        while True:
            job = jobs[job_id]
            while sent < len(job["log"]):
                yield f"data: {json.dumps({'line': job['log'][sent], 'done': False})}\n\n"
                sent += 1
            if job["done"]:
                yield f"data: {json.dumps({'done': True, 'results': job['results'], 'line': ''})}\n\n"
                break
            time.sleep(0.3)

    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  YouTube Channel Discovery Tester")
    print("=" * 55)
    print(f"  YOUTUBE_API_KEY : {'✅ loaded' if YOUTUBE_API_KEY else '❌ MISSING'}")
    print(f"  GEMINI_API_KEY  : {'✅ loaded' if GEMINI_API_KEY else '❌ MISSING'}")
    print("=" * 55)
    print("  Open → http://127.0.0.1:5001")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5001, debug=False)
