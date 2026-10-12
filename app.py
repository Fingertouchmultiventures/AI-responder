import os
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# If this model name stops working, set GEMINI_MODEL on Render to a newer
# "flash" model listed at aistudio.google.com
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
SECRET = os.environ.get("SECRET", "")  # only used by the Whatauto /message route

# Meta WhatsApp Cloud API settings (set these on Render)
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")          # any password you invent
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")      # from Meta
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")    # from Meta
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v21.0")

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

FALLBACK = "Sorry, I'm having trouble right now. We'll reply shortly."
seen_ids = set()  # avoids answering the same message twice if Meta retries


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
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def safe_reply(system_prompt, text):
    try:
        reply = ask_gemini(system_prompt, text)
        return reply or "Thanks for your message. We'll get back to you shortly."
    except Exception as e:
        print("GEMINI ERROR:", e, flush=True)
        return FALLBACK


def send_whatsapp(to, text):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        },
        timeout=20,
    )
    print("SEND STATUS:", r.status_code, r.text[:300], flush=True)


def handle_incoming(sender, text):
    reply = safe_reply(BUSINESS_PROMPT, text)
    send_whatsapp(sender, reply)


@app.get("/")
def home():
    return "AI responder is running."


# ---------- Meta WhatsApp Cloud API ----------

@app.get("/webhook")
def webhook_verify():
    # Meta calls this once when you save the webhook in the developer dashboard
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and VERIFY_TOKEN and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


@app.post("/webhook")
def webhook_receive():
    data = request.get_json(force=True, silent=True) or {}
    print("WEBHOOK:", data, flush=True)

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    msg_id = msg.get("id")
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    if len(seen_ids) > 2000:
                        seen_ids.clear()

                    sender = msg.get("from")
                    if msg.get("type") == "text":
                        text = (msg.get("text", {}).get("body") or "").strip()
                        if sender and text:
                            # Reply in the background so Meta gets a fast 200 OK
                            threading.Thread(
                                target=handle_incoming, args=(sender, text), daemon=True
                            ).start()
                    elif sender:
                        threading.Thread(
                            target=send_whatsapp,
                            args=(sender, "Thanks! Please send your request as a text message so we can help you quickly."),
                            daemon=True,
                        ).start()
    except Exception as e:
        print("WEBHOOK ERROR:", e, flush=True)

    return "OK", 200


# ---------- AutoResponder app (webhook) ----------

def is_business_app(name):
    name = (name or "").lower()
    # com.whatsapp.w4b = WhatsApp Business, com.whatsapp = normal WhatsApp
    return "w4b" in name or "business" in name


@app.post("/autoresponder")
def autoresponder():
    if SECRET and request.headers.get("X-Secret") != SECRET:
        return jsonify(replies=[]), 401

    data = request.get_json(force=True, silent=True) or {}
    q = data.get("query") or {}
    print("AUTORESPONDER:", data, flush=True)

    text = (q.get("message") or "").strip()
    package = q.get("messengerPackageName") or q.get("appName") or ""
    is_group = bool(q.get("isGroup")) or bool(q.get("groupParticipant"))

    if is_group or not text:
        return jsonify(replies=[])

    system = BUSINESS_PROMPT if is_business_app(package) else PERSONAL_PROMPT
    return jsonify(replies=[{"message": safe_reply(system, text)}])


# ---------- Whatauto (only works if you have Whatauto Pro) ----------

@app.post("/message")
def message():
    if SECRET and request.headers.get("X-Secret") != SECRET:
        return jsonify(reply=""), 401

    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("message") or "").strip()
    group = data.get("group_name") or ""
    app_name = (data.get("app") or "").lower()
    print("INCOMING:", data, flush=True)

    if group or not text:
        return jsonify(reply="")

    system = BUSINESS_PROMPT if is_business_app(app_name) else PERSONAL_PROMPT
    return jsonify(reply=safe_reply(system, text))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
