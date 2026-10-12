import os
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment

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

SECRET = os.environ.get("SECRET", "")


@app.get("/")
def home():
    return "AI responder is running."


@app.post("/message")
def message():
    if SECRET and request.headers.get("X-Secret") != SECRET:
        return jsonify(reply=""), 401

    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("message") or "").strip()
    group = data.get("group_name") or ""
    app_name = (data.get("app") or "").lower()

    # Shows in Render logs so you can confirm what Whatauto sends
    print("INCOMING:", data, flush=True)

    # Skip group chats and empty messages
    if group or not text:
        return jsonify(reply="")

    # WhatsApp Business app -> business tone, normal WhatsApp -> personal tone
    system = BUSINESS_PROMPT if "business" in app_name else PERSONAL_PROMPT

    try:
        r = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": text}],
        )
        reply = r.content[0].text
    except Exception as e:
        print("ERROR:", e, flush=True)
        reply = "Sorry, I'm having trouble right now. We'll reply shortly."

    return jsonify(reply=reply)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
