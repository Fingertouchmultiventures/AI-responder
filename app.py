import os
import re
import time
import difflib
import requests
from collections import defaultdict, deque
from flask import Flask, request, jsonify, Response

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- Settings (all optional, set on Render > Environment) ----------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
USE_AI = os.environ.get("USE_AI", "1") != "0"          # USE_AI=0 -> FAQ answers only
DEFAULT_REPLY = os.environ.get("DEFAULT_REPLY", "").strip()  # sent when nothing matches and AI is off/fails
SECRET = os.environ.get("SECRET", "").strip()          # optional X-Secret header from the phone app
ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()    # optional: protects /ask /faq /testai with ?key=
PAUSED = os.environ.get("PAUSED", "") == "1"           # PAUSED=1 -> bot stops replying
# SaleSmartly: put a secret word in the webhook URL path, e.g. /salesmartly/mysecret123
SALESMARTLY_KEY = os.environ.get("SALESMARTLY_KEY", "").strip()
# SaleSmartly: words that identify your PERSONAL WhatsApp channel (its channel name or number),
# separated by commas. Everything else is treated as the business number.
PERSONAL_MATCH = [x.strip().lower() for x in os.environ.get("PERSONAL_CHANNEL_MATCH", "").split(",") if x.strip()]

# ---------------- How the AI should sound when no FAQ entry matches ----------------
PERSONAL_PERSONA = """You are replying to WhatsApp messages on behalf of the owner's
personal number. Be casual, warm and friendly, like a good friend texting.
Keep replies short (1-3 sentences)."""

BUSINESS_PERSONA = """You are the WhatsApp assistant for O² Affairs.
O² Affairs: experts in Microsoft Office tools to get your tasks done. Also offers
graphics design, cinematography, architectural design, site creation and development,
and more. Delivers across the nation.
Slogan: "Finger of God makes a difference. A trial will definitely convince you."
Be polite, professional and warm. Keep replies short (2-4 sentences).
Business link: https://wa.me/message/Y62MOEWGUOAXF1"""

AI_RULES = """
Rules:
- Use ONLY the facts in the KNOWLEDGE BASE and the description above.
- Never invent prices, timelines, availability, addresses or promises.
- If the answer is not covered, say the team will reply shortly. Do not guess.
"""

# ---------------- FAQ files ----------------

def load_faq(filename):
    """Reads faq_*.txt. Entries are separated by a line containing only ---
    Each entry has:  Q: question   KEYWORDS: a, b, c (optional)   A: answer"""
    path = os.path.join(BASE_DIR, filename)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return []
    entries = []
    for block in re.split(r"^\s*-{3,}\s*$", raw, flags=re.M):
        parts = {"q": [], "k": [], "a": []}
        cur = None
        for line in block.splitlines():
            m = re.match(r"^\s*(Q|KEYWORDS|A)\s*:\s*(.*)$", line, flags=re.I)
            if m:
                cur = {"Q": "q", "KEYWORDS": "k", "A": "a"}[m.group(1).upper()]
                parts[cur].append(m.group(2))
            elif cur and not (cur != "a" and line.strip().startswith("#")):
                parts[cur].append(line)
        question = " ".join(parts["q"]).strip()
        answer = "\n".join(parts["a"]).strip()
        keywords = [k.strip() for k in ",".join(parts["k"]).split(",") if k.strip()]
        if answer and (question or keywords):
            entries.append({"question": question, "keywords": keywords, "answer": answer})
    return entries


FAQ = {
    "business": load_faq("faq_business.txt"),
    "personal": load_faq("faq_personal.txt"),
}


def normalize(text):
    text = (text or "").lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9']+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def match_faq(entries, text):
    """Returns the best matching answer or None.
    Keyword hit (whole word/phrase): longer keyword wins. Near-identical question wins over keywords."""
    t = normalize(text)
    if not t:
        return None
    best_answer, best_score = None, 0
    for e in entries:
        score = 0
        for kw in e["keywords"]:
            kn = normalize(kw)
            if kn and re.search(r"(?<![a-z0-9'])" + re.escape(kn) + r"(?![a-z0-9'])", t):
                score = max(score, 50 + len(kn))
        if e["question"]:
            ratio = difflib.SequenceMatcher(None, t, normalize(e["question"])).ratio()
            if ratio >= 0.8:
                score = max(score, 100 * ratio + 100)
        if score > best_score:
            best_answer, best_score = e["answer"], score
    return best_answer


# ---------------- Gemini (fallback when no FAQ entry matches) ----------------

def ask_gemini(system_prompt, user_text):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {"maxOutputTokens": 800, "temperature": 0.5},
    }
    r = None
    for attempt in range(3):  # retry when Google is busy (503) or rate limiting (429)
        r = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=body,
            timeout=20,
        )
        if r.ok or r.status_code not in (429, 500, 503):
            break
        time.sleep(1.5 * (attempt + 1))
    if not r.ok:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:400]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def knowledge_text(kind):
    items = [f"Q: {e['question'] or ', '.join(e['keywords'])}\nA: {e['answer']}" for e in FAQ[kind]]
    return "\n\n".join(items)


def ai_reply(kind, text):
    persona = BUSINESS_PERSONA if kind == "business" else PERSONAL_PERSONA
    system = f"{persona}\n{AI_RULES}\nKNOWLEDGE BASE:\n{knowledge_text(kind) or '(empty)'}"
    try:
        return ask_gemini(system, text) or None
    except Exception as e:
        print("GEMINI ERROR:", e, flush=True)
        return None


def get_reply(kind, text, use_ai=True):
    """FAQ first, then AI, then DEFAULT_REPLY. Returns (reply or None, source)."""
    hit = match_faq(FAQ[kind], text)
    if hit:
        return hit, "faq"
    if use_ai and USE_AI and GEMINI_API_KEY:
        reply = ai_reply(kind, text)
        if reply:
            return reply, "ai"
    if DEFAULT_REPLY:
        return DEFAULT_REPLY, "default"
    return None, "none"


# ---------------- Loop protection (stops two bots replying to each other forever) ----------------

REPLY_GAP_SECONDS = 10
MAX_PER_HOUR = 20
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


def kind_for_app(name):
    name = (name or "").lower()
    # com.whatsapp.w4b = WhatsApp Business, com.whatsapp = normal WhatsApp
    return "business" if ("w4b" in name or "business" in name) else "personal"


def admin_ok():
    return (not ADMIN_KEY) or request.args.get("key", "") == ADMIN_KEY


# ---------------- Pages you can open in a browser ----------------

@app.get("/")
def home():
    return "AI responder is running."


@app.get("/check")
def check():
    return jsonify(
        faq_business_entries=len(FAQ["business"]),
        faq_personal_entries=len(FAQ["personal"]),
        gemini_key_set=bool(GEMINI_API_KEY),
        ai_fallback_on=bool(USE_AI and GEMINI_API_KEY),
        model=GEMINI_MODEL,
        paused=PAUSED,
    )


@app.get("/faq")
def faq_list():
    if not admin_ok():
        return jsonify(error="add ?key=YOUR_ADMIN_KEY"), 401
    return jsonify({
        kind: [{"question": e["question"], "keywords": e["keywords"]} for e in entries]
        for kind, entries in FAQ.items()
    })


@app.get("/ask")
def ask():
    # Try it in your browser:  /ask?app=business&q=how much for graphics design
    # Add &ai=0 to test only the FAQ files.
    if not admin_ok():
        return jsonify(error="add &key=YOUR_ADMIN_KEY"), 401
    q = request.args.get("q", "")
    kind = "personal" if request.args.get("app", "business").lower().startswith("p") else "business"
    reply, source = get_reply(kind, q, use_ai=request.args.get("ai", "1") != "0")
    return jsonify(app=kind, question=q, source=source, reply=reply)


@app.get("/testai")
def testai():
    if not admin_ok():
        return jsonify(error="add ?key=YOUR_ADMIN_KEY"), 401
    try:
        sample = ask_gemini("Reply with one short friendly sentence.", "Say hello")
        return jsonify(ok=True, model=GEMINI_MODEL, sample=sample[:100])
    except Exception as e:
        return jsonify(ok=False, model=GEMINI_MODEL, error=str(e)[:500]), 500


# ---------------- Phone app routes ----------------

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

    reply, source = get_reply(kind_for_app(package), text)
    print("REPLY SOURCE:", source, flush=True)
    return jsonify(replies=[{"message": reply}] if reply else [])


@app.post("/message")  # Whatauto Pro / MacroDroid / Tasker style: {"reply": "..."}
def message():
    if SECRET and request.headers.get("X-Secret") != SECRET:
        return jsonify(reply=""), 401
    if PAUSED:
        return jsonify(reply="")

    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("message") or "").strip()
    group = data.get("group_name") or ("group" if str(data.get("is_group", "0")) == "1" else "")
    app_name = data.get("app") or ""
    print("INCOMING:", data, flush=True)
    if group or not text or should_skip(data.get("sender"), text):
        return jsonify(reply="")

    reply, source = get_reply(kind_for_app(app_name), text)
    print("REPLY SOURCE:", source, flush=True)
    return jsonify(reply=reply or "")


@app.get("/tasker")  # for Tasker / MacroDroid: returns plain text, or ### when nothing should be sent
def tasker():
    if SECRET and not (request.headers.get("X-Secret") == SECRET or request.args.get("secret") == SECRET):
        return Response("###", mimetype="text/plain")
    if PAUSED:
        return Response("###", mimetype="text/plain")

    text = request.args.get("message", "").strip()
    sender = request.args.get("sender", "")
    app_name = request.args.get("app", "")
    is_group = request.args.get("is_group", "0") == "1"
    print("TASKER:", {"app": app_name, "sender": sender, "message": text, "is_group": is_group}, flush=True)

    # Tasker leaves a variable unreplaced (e.g. "%message") when it has no value
    if is_group or not text or text.startswith("%") or should_skip(sender, text):
        return Response("###", mimetype="text/plain")

    reply, source = get_reply(kind_for_app(app_name), text)
    print("REPLY SOURCE:", source, flush=True)
    return Response(reply or "###", mimetype="text/plain; charset=utf-8")


# ---------------- SaleSmartly (Team > Member Management > AI Member > Custom Webhook) ----------------

def salesmartly_kind(d):
    label = f"{d.get('channel_name', '')} {d.get('channel_uid', '')}".lower()
    return "personal" if any(p in label for p in PERSONAL_MATCH) else "business"


@app.post("/salesmartly")
@app.post("/salesmartly/<key>")
def salesmartly(key=""):
    ok = {"code": 0, "msg": "Success"}
    if SALESMARTLY_KEY and key != SALESMARTLY_KEY:
        return jsonify(code=-1, msg="unauthorized"), 401
    if PAUSED:
        return jsonify(ok)

    body = request.get_json(force=True, silent=True) or {}
    print("SALESMARTLY:", body, flush=True)  # check Render > Logs to see exactly what SaleSmartly sends

    d = body.get("data") or {}
    if body.get("event") != "message" or not isinstance(d, dict):
        return jsonify(ok)

    text = d.get("msg")
    # Only answer plain text sent by the customer (sender_type 1), not by your team,
    # and not when a human agent already owns the chat (sys_user_id > 0)
    if str(d.get("msg_type")) != "text" or not isinstance(text, str) or not text.strip():
        return jsonify(ok)
    if str(d.get("sender_type")) != "1":
        return jsonify(ok)
    try:
        if int(d.get("sys_user_id") or 0) > 0:
            return jsonify(ok)
    except (TypeError, ValueError):
        pass
    if should_skip(d.get("chat_user_id"), text):
        return jsonify(ok)

    reply, source = get_reply(salesmartly_kind(d), text.strip())
    print("REPLY SOURCE:", source, flush=True)
    if not reply:
        return jsonify(ok)

    return jsonify({
        "code": 0,
        "msg": "Success",
        "data": {
            "msg_type": 1,
            "msg": reply,
            "chat_user_id": d.get("chat_user_id"),
            "chat_session_id": d.get("chat_session_id"),
            "send_time": str(int(time.time() * 1000)),
            "channel": d.get("channel"),
            "channel_uid": d.get("channel_uid"),
            "channel_name": d.get("channel_name"),
        },
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
