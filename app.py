import os
import time
import requests
from collections import defaultdict, deque
from flask import Flask, request, jsonify

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# If this model name stops working, set GEMINI_MODEL on Render to a newer
# "flash" model listed at aistudio.google.com
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
SECRET = os.environ.get("SECRET", "").strip()      # optional X-Secret header check
PAUSED = os.environ.get("PAUSED", "") == "1"       # set PAUSED=1 on Render = AI off

# ---------- EDIT THESE TWO PROMPTS ----------

PERSONAL_PROMPT = """You are replying to WhatsApp messages on behalf of the owner's
personal number. Be casual, warm and friendly, like a good friend texting.
Keep replies short (1-3 sentences). If someone asks something you can't know
(plans, location, personal matters), say the owner will get back to them soon.
Never invent facts."""

BUSINESS_PROMPT = """You are the WhatsApp assistant for O² Affairs.
About us: We are O² Affairs, experts in Microsoft Office tools to get your tasks done.
We also offer: Graphics design, Cinematography, Architectural design,
Site creation and development, and many more services. We deliver across the nation.
Slogan: "Finger of God makes a difference. A trial will definitely convince you."

Rules:
- Be polite, professional and warm. Keep replies short (2-4 sentences).
- Greet new customers and ask what service they need.
- Never invent prices, timelines or availability. For quotes, say the team
  will reply shortly with details.
- Encourage customers to describe their task clearly so we can help faster.
- Business link: https://wa.me/message/Y62MOEWGUOAXF1
"""

# --------------------------------------------


def ask_gemini(system_prompt, user_text):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.7},
    }
    r = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=25,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:400]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def safe_reply(system_prompt, text):
    """AI reply, or None if anything fails (so no error text is ever sent to a chat)."""
    try:
        return ask_gemini(system_prompt, text) or None
    except Exception as e:
        print("GEMINI ERROR:", e, flush=True)
        return None


# ---------- Loop protection: stops two bots replying to each other forever ----------

REPLY_GAP_SECONDS = 10     # minimum seconds between replies to the same sender
MAX_PER_HOUR = 20          # max replies to one sender per hour
last_reply = {}
history = defaultdict(deque)


def should_skip(sender, text):
    t = (text or "").lower()
    if "autoresponder.ai" in t or "having trouble right now" in t:
        return True
    key = (sender or "").strip().lower()
    now = time.time()
    if now - last_reply.get(key, 0) < REPLY_GAP_SECONDS:
        return True
    h = history[key]
    while h and now - h[0] > 3600:
        h.popleft()
    if len(h) >= MAX_PER_HOUR:
        return True
    last_reply[key] = now
    h.append(now)
    if len(last_reply) > 5000:
        last_reply.clear()
        history.clear()
    return False


def is_business_app(name):
    name = (name or "").lower()
    # com.whatsapp.w4b = WhatsApp Business, com.whatsapp = normal WhatsApp
    return "w4b" in name or "business" in name


# ---------- Pages for checking things in your browser ----------

@app.get("/")
def home():
    return "AI responder is running."


@app.get("/check")
def check():
    # Shows whether settings were found. Never shows the secret values.
    return jsonify(
        gemini_key_set=bool(GEMINI_API_KEY),
        model=GEMINI_MODEL,
        paused=PAUSED,
        routes=sorted(r.rule for r in app.url_map.iter_rules()),
    )


@app.get("/testai")
def testai():
    # Open this page to see if Gemini works, or the exact reason it doesn't.
    try:
        sample = ask_gemini("Reply with one short friendly sentence.", "Say hello")
        return jsonify(ok=True, model=GEMINI_MODEL, sample=sample[:100])
    except Exception as e:
        return jsonify(ok=False, model=GEMINI_MODEL, error=str(e)[:500]), 500


# ---------- AutoResponder app ----------

@app.post("/autoresponder")
def autoresponder():
    if SECRET and request.headers.get("X-Secret") != SECRET:
        return jsonify(replies=[]), 401
    if PAUSED:
        return jsonify(replies=[])

    data = request.get_json(force=True, silent=True) or {}
    q = data.get("query") or {}
    print("AUTORESPONDER:", data, flush=True)

    text = (q.get("message") or "").strip()
    package = q.get("messengerPackageName") or q.get("appName") or ""
    is_group = bool(q.get("isGroup")) or bool(q.get("groupParticipant"))

    if is_group or not text or should_skip(q.get("sender"), text):
        return jsonify(replies=[])

    system = BUSINESS_PROMPT if is_business_app(package) else PERSONAL_PROMPT
    reply = safe_reply(system, text)
    return jsonify(replies=[{"message": reply}] if reply else [])


# ---------- Whatauto (needs Whatauto Pro) ----------

@app.post("/message")
def message():
    if SECRET and request.headers.get("X-Secret") != SECRET:
        return jsonify(reply=""), 401
    if PAUSED:
        return jsonify(reply="")

    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("message") or "").strip()
    group = data.get("group_name") or ""
    app_name = (data.get("app") or "").lower()
    print("INCOMING:", data, flush=True)

    if group or not text or should_skip(data.get("sender"), text):
        return jsonify(reply="")

    system = BUSINESS_PROMPT if is_business_app(app_name) else PERSONAL_PROMPT
    return jsonify(reply=safe_reply(system, text) or "")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
