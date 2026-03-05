from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import os, psycopg2, psycopg2.extras

app = Flask(__name__)

# ── Bay config ───────────────────────────────────────
# "universal" = any EV  |  "tesla" = Tesla only
BAYS = {
    "1": "universal", "2": "universal",
    "3": "universal", "4": "universal",
    "5": "tesla",     "6": "tesla",
    "7": "tesla",
}
# ─────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")

def init_db():
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bays (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                user_phone  TEXT,
                claimed_at  FLOAT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                phone  TEXT PRIMARY KEY,
                name   TEXT NOT NULL
            )
        """)
        for bid, btype in BAYS.items():
            cur.execute("""
                INSERT INTO bays (id, type, user_phone, claimed_at)
                VALUES (%s, %s, NULL, NULL)
                ON CONFLICT (id) DO NOTHING
            """, (bid, btype))
        conn.commit()

def get_state():
    with get_db() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM bays ORDER BY id")
        return {r["id"]: dict(r) for r in cur.fetchall()}

def get_user_name(phone):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM users WHERE phone=%s", (phone,))
        row = cur.fetchone()
        return row[0] if row else None

def save_user_name(phone, name):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO users (phone, name) VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE SET name=%s
        """, (phone, name, name))
        conn.commit()

def claim(bid, phone):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE bays SET user_phone=%s, claimed_at=%s WHERE id=%s",
                    (phone, datetime.now().timestamp(), bid))
        conn.commit()

def release(bid):
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE bays SET user_phone=NULL, claimed_at=NULL WHERE id=%s", (bid,))
        conn.commit()

def elapsed(ts):
    if not ts: return ""
    diff = int((datetime.now().timestamp() - float(ts)) / 60)
    return f"{diff}m" if diff < 60 else f"{diff//60}h {diff%60}m"

init_db()

# ── Twilio client for outbound alerts ────────────────
TWILIO_SID    = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_NUMBER = os.environ["TWILIO_WHATSAPP_NUMBER"]  # e.g. whatsapp:+14155238886
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)

OVERTIME_HOURS = 5  # alert after this many hours

def check_overtime():
    """Runs every 30 min — sends WhatsApp alert if anyone has been on 5+ hours."""
    try:
        state = get_state()
        now   = datetime.now().timestamp()
        for bid, bay in state.items():
            if not bay["user_phone"] or not bay["claimed_at"]:
                continue
            hours = (now - float(bay["claimed_at"])) / 3600
            if hours >= OVERTIME_HOURS:
                name  = get_user_name(bay["user_phone"]) or f"...{bay['user_phone'][-4:]}"
                btype = "Tesla-only ⚡" if BAYS[bid] == "tesla" else "Universal 🔌"
                twilio_client.messages.create(
                    from_=TWILIO_NUMBER,
                    to=f"whatsapp:{bay['user_phone']}",
                    body=(
                        f"⏰ *Belk Charging Station Alert*\n\n"
                        f"Hi *{name}*, you've had Bay {bid} ({btype}) "
                        f"for *{int(hours)} hours*.\n\n"
                        f"Please release it if you're done so others can use it.\n"
                        f"Reply *release {bid}* when done. 🙏"
                    )
                )
    except Exception as e:
        print(f"Overtime check error: {e}")

# Run check every 30 minutes
scheduler = BackgroundScheduler()
scheduler.add_job(check_overtime, "interval", minutes=30)
scheduler.start()



@app.route("/")
def health(): return "EV Bot is running ⚡"

@app.route("/whatsapp", methods=["POST"])
def bot():
    body  = request.form.get("Body", "").strip()
    phone = request.form.get("From", "").replace("whatsapp:", "")
    parts = body.split()
    cmd   = parts[0].lower() if parts else ""
    resp  = MessagingResponse()

    # ── Name registration ────────────────────────────
    name = get_user_name(phone)

    if not name:
        # Check if they're replying with their name
        if body and not any(body.lower().startswith(c) for c in ["status","claim","release","who","help","myname"]):
            save_user_name(phone, body.strip())
            resp.message(
                f"👋 Welcome, *{body.strip()}*!\n\n"
                "You're all set. Here's what you can do:\n\n"
                "⚡ *Belk Charging Station*\n"
                "🔌 Universal: Bays 1–4\n"
                "⚡ Tesla only: Bays 5–7\n\n"
                "• *status* — see all bays\n"
                "• *claim 1* — claim a bay\n"
                "• *release 1* — free your bay\n"
                "• *who* — see who's charging\n"
                "• *help* — show this menu"
            )
            return str(resp)
        else:
            # First time user — ask for name
            resp.message(
                "👋 Welcome to *Belk Charging Station*!\n\n"
                "Before we start, what's your name?\n"
                "_(Just reply with your name, e.g. Sarah)_"
            )
            return str(resp)
    # ─────────────────────────────────────────────────

    # Allow users to update their name
    if cmd == "myname" and len(parts) >= 2:
        new_name = " ".join(parts[1:])
        save_user_name(phone, new_name)
        resp.message(f"✅ Name updated to *{new_name}*!")
        return str(resp)

    state = get_state()

    if cmd == "status":
        lines = ["⚡ *Belk Charging Station*\n",
                 "🔌 *Universal (Bays 1–4)*"]
        for b in ["1","2","3","4"]:
            s = state[b]
            if s["user_phone"]:
                n = get_user_name(s["user_phone"]) or f"...{s['user_phone'][-4:]}"
                lines.append(f"  🔴 Bay {b} — {n} ({elapsed(s['claimed_at'])})")
            else:
                lines.append(f"  ✅ Bay {b} — Free")
        lines.append("\n⚡ *Tesla Only (Bays 5–7)*")
        for b in ["5","6","7"]:
            s = state[b]
            if s["user_phone"]:
                n = get_user_name(s["user_phone"]) or f"...{s['user_phone'][-4:]}"
                lines.append(f"  🔴 Bay {b} — {n} ({elapsed(s['claimed_at'])})")
            else:
                lines.append(f"  ✅ Bay {b} — Free")
        fu = sum(1 for b in ["1","2","3","4"] if not state[b]["user_phone"])
        ft = sum(1 for b in ["5","6","7"] if not state[b]["user_phone"])
        lines.append(f"\n🔌 {fu}/4 universal  ⚡ {ft}/3 Tesla free")
        resp.message("\n".join(lines))

    elif cmd == "claim" and len(parts) == 2:
        bid = parts[1]
        if bid not in BAYS:
            resp.message("❌ Invalid bay. Universal: 1–4 🔌  Tesla: 5–7 ⚡")
        elif state[bid]["user_phone"]:
            n = get_user_name(state[bid]["user_phone"]) or f"...{state[bid]['user_phone'][-4:]}"
            resp.message(f"⚠️ Bay {bid} is taken by *{n}* ({elapsed(state[bid]['claimed_at'])}).\nSend *status* to find a free bay.")
        else:
            label = "Tesla-only ⚡" if BAYS[bid] == "tesla" else "Universal 🔌"
            warn  = "\n⚠️ This is a *Tesla-only* bay." if BAYS[bid] == "tesla" else ""
            claim(bid, phone)
            resp.message(f"✅ Bay {bid} ({label}) claimed, *{name}*!{warn}\nSend *release {bid}* when done.")

    elif cmd == "release" and len(parts) == 2:
        bid = parts[1]
        if bid not in BAYS:
            resp.message("❌ Invalid bay. Universal: 1–4  Tesla: 5–7")
        elif not state[bid]["user_phone"]:
            resp.message(f"Bay {bid} is already free!")
        elif state[bid]["user_phone"] != phone:
            n = get_user_name(state[bid]["user_phone"]) or "someone else"
            resp.message(f"⚠️ Bay {bid} was claimed by *{n}*. Only they can release it.")
        else:
            t = elapsed(state[bid]["claimed_at"])
            release(bid)
            resp.message(f"🔌 Bay {bid} released after {t}. Thanks, *{name}*!")

    elif cmd == "who":
        lines = ["👤 *Currently charging:*\n"]
        found = False
        for bid, btype in BAYS.items():
            s = state[bid]
            if s["user_phone"]:
                n = get_user_name(s["user_phone"]) or f"...{s['user_phone'][-4:]}"
                icon = "⚡" if btype == "tesla" else "🔌"
                lines.append(f"{icon} Bay {bid}: *{n}* · {elapsed(s['claimed_at'])}")
                found = True
        resp.message("\n".join(lines) if found else "All 7 bays are free! 🎉")

    else:
        resp.message(
            "⚡ *Belk Charging Station*\n\n"
            "🔌 Universal: Bays 1–4\n"
            "⚡ Tesla only: Bays 5–7\n\n"
            "• *status* — see all bays\n"
            "• *claim 1* — claim a bay\n"
            "• *release 1* — free your bay\n"
            "• *who* — see who's charging\n"
            "• *myname John* — update your name\n"
            "• *help* — show this menu"
        )

    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
