from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
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
    # Creates table on first run; safe to call on every startup
    with get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bays (
                id          TEXT PRIMARY KEY,
                type        TEXT NOT NULL,
                user_phone  TEXT,
                claimed_at  FLOAT
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

def tag(phone):
    return f"...{phone[-4:]}" if phone else ""

init_db()  # runs on every startup — safe, idempotent

@app.route("/")
def health(): return "EV Bot is running ⚡"

@app.route("/whatsapp", methods=["POST"])
def bot():
    body  = request.form.get("Body", "").strip().lower()
    phone = request.form.get("From", "").replace("whatsapp:", "")
    parts = body.split()
    cmd   = parts[0] if parts else ""
    resp  = MessagingResponse()
    state = get_state()  # fresh from DB every request

    if cmd == "status":
        lines = ["⚡ *EV Charger Status*\n", "🔌 *Universal (Bays 1–4)*"]
        for b in ["1","2","3","4"]:
            s = state[b]
            status = f"{tag(s['user_phone'])} ({elapsed(s['claimed_at'])})" if s["user_phone"] else "Free"
            lines.append(f"  {'🔴' if s['user_phone'] else '✅'} Bay {b} — {status}")
        lines.append("\n⚡ *Tesla Only (Bays 5–7)*")
        for b in ["5","6","7"]:
            s = state[b]
            status = f"{tag(s['user_phone'])} ({elapsed(s['claimed_at'])})" if s["user_phone"] else "Free"
            lines.append(f"  {'🔴' if s['user_phone'] else '✅'} Bay {b} — {status}")
        fu = sum(1 for b in ["1","2","3","4"] if not state[b]["user_phone"])
        ft = sum(1 for b in ["5","6","7"] if not state[b]["user_phone"])
        lines.append(f"\n🔌 {fu}/4 universal  ⚡ {ft}/3 Tesla free")
        resp.message("\n".join(lines))

    elif cmd == "claim" and len(parts) == 2:
        bid = parts[1]
        if bid not in BAYS:
            resp.message("❌ Invalid bay. Universal: 1–4 🔌  Tesla: 5–7 ⚡")
        elif state[bid]["user_phone"]:
            resp.message(f"⚠️ Bay {bid} is taken by {tag(state[bid]['user_phone'])} ({elapsed(state[bid]['claimed_at'])}).\nSend *status* to find a free bay.")
        else:
            label = "Tesla-only ⚡" if BAYS[bid] == "tesla" else "Universal 🔌"
            warn  = "\n⚠️ This is a *Tesla-only* bay." if BAYS[bid] == "tesla" else ""
            claim(bid, phone)
            resp.message(f"✅ Bay {bid} ({label}) claimed!{warn}\nSend *release {bid}* when done.")

    elif cmd == "release" and len(parts) == 2:
        bid = parts[1]
        if bid not in BAYS:
            resp.message("❌ Invalid bay. Universal: 1–4  Tesla: 5–7")
        elif not state[bid]["user_phone"]:
            resp.message(f"Bay {bid} is already free!")
        elif state[bid]["user_phone"] != phone:
            resp.message("⚠️ You didn't claim this bay. Only the person who claimed it can release it.")
        else:
            t = elapsed(state[bid]["claimed_at"])
            release(bid)
            resp.message(f"🔌 Bay {bid} released after {t}. Thanks!")

    elif cmd == "who":
        lines = ["👤 *Currently charging:*\n"]
        found = False
        for bid, btype in BAYS.items():
            s = state[bid]
            if s["user_phone"]:
                icon = "⚡" if btype == "tesla" else "🔌"
                lines.append(f"{icon} Bay {bid}: {tag(s['user_phone'])} · {elapsed(s['claimed_at'])}")
                found = True
        resp.message("\n".join(lines) if found else "All 7 bays are free! 🎉")

    else:
        resp.message(
            "⚡ *EV Charger Bot*\n\n"
            "🔌 Universal: Bays 1–4\n"
            "⚡ Tesla only: Bays 5–7\n\n"
            "• *status* — see all bays\n"
            "• *claim [1-7]* — claim a bay\n"
            "• *release [1-7]* — free your bay\n"
            "• *who* — see who's charging\n"
            "• *help* — show this menu"
        )

    return str(resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
