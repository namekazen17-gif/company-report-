"""
test_setup.py — run before starting the app to verify all 3 API keys work.
Usage: python test_setup.py
"""

import os
from dotenv import load_dotenv
load_dotenv()

print("=" * 55)
print("  Video Competitor Intelligence — Setup Test")
print("=" * 55)

errors = []

# ── 1. YouTube ───────────────────────────────────────────────
print("\n[1/3] Testing YouTube Data API v3...")
try:
    import requests
    key = os.getenv("YOUTUBE_API_KEY")
    if not key: raise ValueError("YOUTUBE_API_KEY not set in .env")
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={"part": "snippet", "q": "HubSpot", "type": "channel", "maxResults": 1, "key": key},
        timeout=10
    )
    data = r.json()
    if "error" in data: raise ValueError(data["error"]["message"])
    print(f"   OK  YouTube API working. Found: {data['items'][0]['snippet']['title']}")
except Exception as e:
    print(f"   FAIL  YouTube: {e}")
    errors.append("YouTube")

# ── 2. Groq ──────────────────────────────────────────────────
print("\n[2/3] Testing Groq API (Llama 3.3 70B)...")
try:
    from groq import Groq
    key = os.getenv("GROQ_API_KEY")
    if not key: raise ValueError("GROQ_API_KEY not set in .env")
    client = Groq(api_key=key)
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": "Reply with exactly: GROQ OK"}],
        max_tokens=10
    )
    print(f"   OK  Groq API working. Response: {resp.choices[0].message.content.strip()}")
except Exception as e:
    print(f"   FAIL  Groq: {e}")
    errors.append("Groq")

# ── 3. Gemini (official google-genai SDK) ────────────────────
print("\n[3/3] Testing Gemini API (google-genai SDK, gemini-2.5-flash)...")
try:
    from google import genai as google_genai
    key = os.getenv("GEMINI_API_KEY")
    if not key: raise ValueError("GEMINI_API_KEY not set in .env")
    client = google_genai.Client(api_key=key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="Reply with exactly: GEMINI OK"
    )
    print(f"   OK  Gemini API working. Response: {response.text.strip()[:30]}")
except Exception as e:
    print(f"   FAIL  Gemini: {e}")
    errors.append("Gemini")

# ── Summary ──────────────────────────────────────────────────
print("\n" + "=" * 55)
if not errors:
    print("  ALL CLEAR. Run: python app.py")
    print("  Then open:  http://localhost:5000")
else:
    print(f"  Fix these before running: {', '.join(errors)}")
print("=" * 55)
