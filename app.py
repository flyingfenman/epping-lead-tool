import os
import smtplib
import ssl
import sqlite3
import csv
import io
import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client as TwilioClient

load_dotenv()

app = Flask(__name__)

# ── Epping Car Sales — the selling side, merged in under /sales ───────────────
# All selling pages (enquiries, stock list, sales templates) live at
# http://localhost:5000/sales/ and use their own database (sales.db) and their
# own email identity (SALES_* keys in .env). See sales.py.
from sales import sales_bp, add_stock as sales_add_stock
app.register_blueprint(sales_bp)

YOUR_NAME        = os.getenv("YOUR_NAME", "Henry")
YOUR_PHONE       = os.getenv("YOUR_PHONE", "+44 1992 367909")
# EMAIL_ADDRESS is the canonical key; GMAIL_ADDRESS kept as fallback so existing .env files don't break.
YOUR_EMAIL       = os.getenv("EMAIL_ADDRESS") or os.getenv("GMAIL_ADDRESS")
EMAIL_PASSWORD   = os.getenv("EMAIL_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD")
DISPLAY_NAME     = os.getenv("DISPLAY_NAME", YOUR_NAME)
# Zoho Mail SMTP defaults — EU data centre. Change to smtp.zoho.com if the mailbox is on the US data centre.
SMTP_HOST        = os.getenv("SMTP_HOST", "smtp.zoho.eu")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "465"))
TWILIO_SID       = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM      = os.getenv("TWILIO_FROM", "")  # Either a Twilio phone number or your approved alphanumeric sender ID
# Master switch for all SMS — set SMS_ENABLED=true in .env once Twilio is configured.
SMS_ENABLED      = os.getenv("SMS_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")
FB_VERIFY_TOKEN  = os.getenv("FACEBOOK_VERIFY_TOKEN", "")
FB_PAGE_TOKEN    = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
FB_PAGE_ID       = os.getenv("FACEBOOK_PAGE_ID", "1134446903093122")

DATA_DIR         = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH          = os.path.join(DATA_DIR, "leads.db")

# Logo embedded as the email signature (an email-sized copy, kept small on purpose)
LOGO_PATH        = os.path.join(os.path.dirname(__file__), "static", "ECBlogo_email.jpg")


# ── Default email templates (seeded into DB on first run; editable from the UI) ───────────────

DEFAULT_TEMPLATES = {
    "fu1_body": (
        "Hi {name},\n\n"
        "Just following up on my email about your {car} — I wanted to make sure it reached you.\n\n"
        "Our offer of £{price} still stands, and if the car has been well looked after there's usually room to improve on that once I've seen it in person. We can come to you, pay on the day, and handle the V5 paperwork on the spot.\n\n"
        "Would this week or next suit better for a quick look?\n\n"
        "{SIGN_OFF}"
    ),
    "fu2_body": (
        "Hi {name},\n\n"
        "This will be my last email on this — I just wanted to check you'd seen my offer of £{price} for your {car}.\n\n"
        "If you've already sold or decided to keep the car, no problem at all — a quick reply to let me know would be much appreciated.\n\n"
        "If you're still considering it, the offer stands, and if the car has been well looked after we're happy to improve on that once I've seen it. I'm happy to work around a time that suits you.\n\n"
        "{SIGN_OFF}"
    ),
}


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT,
                email            TEXT,
                phone            TEXT,
                car              TEXT,
                reg              TEXT,
                mileage          TEXT,
                postcode         TEXT,
                offer            TEXT,
                source           TEXT DEFAULT 'Facebook',
                notes            TEXT,
                status           TEXT DEFAULT 'New',
                created_at       TEXT DEFAULT (datetime('now','localtime')),
                fu1_send_at      TEXT,
                fu2_send_at      TEXT,
                fu1_sent_at      TEXT,
                fu2_sent_at      TEXT,
                sms_sent_at      TEXT
            )
        """)
        # Add columns if upgrading from older DB
        for col in ["fu1_send_at TEXT", "fu2_send_at TEXT", "sms_send_at TEXT", "sms_sent_at TEXT", "status TEXT", "paused INTEGER DEFAULT 0", "market_target TEXT", "last_name TEXT"]:
            try:
                con.execute(f"ALTER TABLE leads ADD COLUMN {col}")
            except:
                pass

        # Facebook leads inbox — raw leads before the user processes them
        con.execute("""
            CREATE TABLE IF NOT EXISTS facebook_leads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                fb_lead_id    TEXT UNIQUE,
                form_name     TEXT,
                name          TEXT,
                phone         TEXT,
                email         TEXT,
                reg           TEXT,
                mileage       TEXT,
                postcode      TEXT,
                notes         TEXT,
                created_time  TEXT,
                received_at   TEXT DEFAULT (datetime('now','localtime')),
                dismissed     INTEGER DEFAULT 0
            )
        """)

        # Templates table — editable email bodies
        con.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                key        TEXT PRIMARY KEY,
                body       TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Seed defaults if missing (won't overwrite if user has already edited)
        for key, body in DEFAULT_TEMPLATES.items():
            con.execute("INSERT OR IGNORE INTO templates (key, body) VALUES (?, ?)", (key, body))

def save_lead(data, fu1_at, fu2_at, sms_at):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""
            INSERT INTO leads (name, last_name, email, phone, car, reg, mileage, postcode, offer, market_target, source, notes, fu1_send_at, fu2_send_at, sms_send_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("name"), data.get("last_name", ""), data.get("email"), data.get("phone"),
            data.get("car"), data.get("reg"), data.get("mileage"),
            data.get("postcode"), data.get("price"), data.get("market_target", ""),
            data.get("source", "Facebook"), data.get("notes", ""),
            fu1_at, fu2_at, sms_at
        ))
        return cur.lastrowid

def mark_followup(lead_id, num):
    col = f"fu{num}_sent_at"
    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"UPDATE leads SET {col} = datetime('now','localtime') WHERE id = ?", (lead_id,))

def mark_sms(lead_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE leads SET sms_sent_at = datetime('now','localtime') WHERE id = ?", (lead_id,))

def update_status(lead_id, status):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))

def get_all_leads():
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM leads ORDER BY created_at DESC").fetchall()]

def get_lead(lead_id):
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None

def _save_fb_lead(lead_data, form_name=""):
    fb_lead_id = lead_data.get("id")
    if not fb_lead_id:
        return
    fd = lead_data.get("field_data", [])
    name     = _fb_field(fd, "full_name", "name")
    email    = _fb_field(fd, "email")
    phone    = _fb_field(fd, "phone_number", "phone")
    reg      = _fb_field(fd, "registration", "reg_", "vehicle_reg")
    mileage  = _fb_field(fd, "mileage")
    postcode = _fb_field(fd, "post_code", "postcode", "zip")
    service  = _fb_field(fd, "service_history", "service", "history")
    notes = f"Service history: {service}" if service else ""
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute("""
                INSERT OR IGNORE INTO facebook_leads
                (fb_lead_id, form_name, name, phone, email, reg, mileage, postcode, notes, created_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (fb_lead_id, form_name, name, phone, email,
                  reg.upper() if reg else "", mileage, postcode, notes,
                  lead_data.get("created_time", "")))
            if con.execute("SELECT changes()").fetchone()[0] > 0:
                print(f"FB inbox: new lead {name} ({reg})")
    except Exception as e:
        print(f"FB inbox save error: {e}")

def get_facebook_inbox():
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM facebook_leads WHERE dismissed = 0 ORDER BY received_at DESC"
        ).fetchall()]

def dismiss_fb_lead_db(fb_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE facebook_leads SET dismissed = 1 WHERE id = ?", (fb_id,))

def delete_lead_db(lead_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM leads WHERE id = ?", (lead_id,))

def get_due_followups():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        fu1 = [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE fu1_send_at <= ? AND fu1_sent_at IS NULL AND email IS NOT NULL AND (paused IS NULL OR paused = 0)",
            (now,)
        ).fetchall()]
        fu2 = [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE fu2_send_at <= ? AND fu2_sent_at IS NULL AND email IS NOT NULL AND (paused IS NULL OR paused = 0)",
            (now,)
        ).fetchall()]
    return fu1, fu2

def set_paused(lead_id, paused):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE leads SET paused = ? WHERE id = ?", (1 if paused else 0, lead_id))

def first_name_of(full_name):
    """Extract the first word of a full name — used so auto-messages say 'Hi Jess' when the DB has 'Jess McLovin'."""
    if not full_name:
        return ""
    parts = str(full_name).strip().split()
    return parts[0] if parts else str(full_name)

def get_template(key):
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT body FROM templates WHERE key = ?", (key,)).fetchone()
        return row[0] if row else DEFAULT_TEMPLATES.get(key, "")

def save_template(key, body):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO templates (key, body, updated_at) VALUES (?, ?, datetime('now','localtime'))
            ON CONFLICT(key) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at
        """, (key, body))

def render_template_text(body, **kwargs):
    """Safe placeholder substitution — uses .replace so unknown {x} placeholders won't crash."""
    for k, v in kwargs.items():
        body = body.replace("{" + k + "}", str(v))
    return body

def get_due_sms():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE sms_send_at <= ? AND sms_sent_at IS NULL AND phone IS NOT NULL AND phone != '' AND (paused IS NULL OR paused = 0)",
            (now,)
        ).fetchall()]

init_db()


# ── Email ─────────────────────────────────────────────────────────────────────

# The body text ends with just "Kind regards," — the full signature block
# (name, title, contact details and logo) is added automatically on send.
SIGN_OFF = "Kind regards,"

PHONE_DISPLAY   = YOUR_PHONE or ""
SIG_WEBSITE     = "www.eppingcarbuyer.com"
SIG_ADDRESS     = "Patches Farm, Galley Hill, EN92AG"
SIGNATURE_PLAIN = f"{YOUR_NAME}\n{PHONE_DISPLAY}\n{YOUR_EMAIL}\n{SIG_WEBSITE}\n{SIG_ADDRESS}"

def email_body(name, car, price, greeting):
    return f"""Hi {name},

Thank you for getting in touch. {greeting}

Based on what you've told me, I think we could achieve around £{price} for your {car}. We'd advertise it across all the main platforms, handle enquiries and viewings, and sort all the paperwork — you just hand over the keys when it sells.

If you'd like to go ahead or want to have a quick chat first, just reply here or give me a call.

{SIGN_OFF}"""

def fu1_body(name, car, price):
    return render_template_text(get_template("fu1_body"),
                                name=name, car=car, price=price, SIGN_OFF=SIGN_OFF)

def fu2_body(name, car, price, market_target=""):
    body = render_template_text(get_template("fu2_body"),
                                name=name, car=car, price=price, SIGN_OFF=SIGN_OFF)
    # For trade-only leads who never saw a Market target, tag a "last chance" Market & Sell pitch on the end
    # (leads who already saw both options don't need this).
    if not market_target:
        pitch = (
            f"\n\nBefore you go — if my direct offer wasn't quite right for your {car}, "
            f"take a look at our Market & Sell service. We list your car professionally, handle every enquiry, "
            f"viewing and negotiation, and get you significantly closer to full retail. No upfront cost — you only pay if it sells.\n\n"
            f"See how it works: https://eppingcarbuyer.com/market-and-sell"
        )
        # Splice the pitch in just before the SIGN_OFF so it reads as a natural P.S. rather than dangling below the signature.
        if SIGN_OFF in body:
            body = body.replace(SIGN_OFF, pitch.lstrip("\n") + "\n\n" + SIGN_OFF)
        else:
            body = body + pitch
    return body

def build_html(plain_body):
    """Build an HTML version of the email body — keeps formatting nice in modern email clients.
    Ends with the Epping Car Buyer logo as the signature."""
    paragraphs = ""
    for line in plain_body.split("\n"):
        if line.strip():
            paragraphs += f"<p style='margin:0 0 16px 0;'>{line}</p>"
    logo_html = ""
    if os.path.exists(LOGO_PATH):
        logo_html = ("<img src='cid:brandlogo' width='170' "
                     "style='width:170px;height:auto;border:0;display:block;margin-top:12px;' "
                     "alt='Epping Car Buyer' />")
    signature = (
        "<div style='margin-top:4px;font-size:14px;line-height:1.55;'>"
        f"<div>{YOUR_NAME}</div>"
        f"<div>{PHONE_DISPLAY}</div>"
        f"<div><a href='mailto:{YOUR_EMAIL}' style='color:#1a56db;'>{YOUR_EMAIL}</a></div>"
        f"<div><a href='https://{SIG_WEBSITE}' style='color:#1a56db;'>{SIG_WEBSITE}</a></div>"
        f"<div>{SIG_ADDRESS}</div>"
        f"{logo_html}"
        "</div>"
    )
    return f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#1a1a1a;line-height:1.7;">
<div style="max-width:600px;">{paragraphs}{signature}</div>
</body></html>"""

def send_email(to_email, subject, plain_body):
    """Send an email via Zoho Mail SMTP (or any SMTP server configured in .env).
    The logo travels inside the email itself (inline image), so it shows in
    Gmail, Outlook and Apple Mail without loading anything external."""
    if not YOUR_EMAIL or not EMAIL_PASSWORD:
        raise RuntimeError("EMAIL_ADDRESS and EMAIL_PASSWORD must be set in .env")

    msg = MIMEMultipart("related")
    msg["From"]    = f"{DISPLAY_NAME} <{YOUR_EMAIL}>"
    msg["To"]      = to_email
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(plain_body + "\n\n" + SIGNATURE_PLAIN, "plain"))
    alt.attach(MIMEText(build_html(plain_body), "html"))

    if os.path.exists(LOGO_PATH):
        try:
            with open(LOGO_PATH, "rb") as f:
                logo = MIMEImage(f.read())
            logo.add_header("Content-ID", "<brandlogo>")
            logo.add_header("Content-Disposition", "inline", filename="ECBlogo.jpg")
            msg.attach(logo)
        except Exception:
            pass  # emails still send fine without the logo

    context = ssl.create_default_context()
    if SMTP_PORT == 587:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=context)
            server.login(YOUR_EMAIL, EMAIL_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as server:
            server.login(YOUR_EMAIL, EMAIL_PASSWORD)
            server.send_message(msg)

def send_auto_followup(lead, num):
    try:
        first = first_name_of(lead["name"])
        body    = fu1_body(first, lead["car"], lead["offer"]) if num == 1 else fu2_body(first, lead["car"], lead["offer"], lead.get("market_target") or "")
        subject = f"{lead['reg']}, {'Following Up' if num == 1 else 'Last Follow-up'} — Epping Car Buyer"
        send_email(lead["email"], subject, body)
        mark_followup(lead["id"], num)
        print(f"Auto follow-up {num} sent to {lead['name']} ({lead['email']})")
    except Exception as e:
        print(f"Auto follow-up {num} failed for lead {lead['id']}: {e}")


# ── SMS ───────────────────────────────────────────────────────────────────────

def sms_body(name, car, price, market_target=""):
    # Kept strictly within GSM 7-bit characters (no em-dash, no curly quotes) so the SMS sends as 1-2 segments instead of 5.
    if market_target:
        return (
            f"Hi {name}, Henry from Epping Car Buyer here. Thanks for your enquiry on your {car}. "
            f"I've emailed two options: direct offer £{price} or Market & Sell listing at £{market_target}. "
            f"Reply by email to henry@eppingcarbuyer.com or call/WhatsApp {YOUR_PHONE}."
        )
    return (
        f"Hi {name}, Henry from Epping Car Buyer here. Thanks for your enquiry on your {car}. "
        f"I've just emailed an offer of £{price} - if the car's well looked after we can often improve on it. "
        f"Reply by email to henry@eppingcarbuyer.com or call/WhatsApp {YOUR_PHONE}."
    )

def send_sms(to_number, message):
    """Send an SMS via Twilio. TWILIO_FROM can be a phone number (+44...) or an alphanumeric sender ID."""
    if not TWILIO_SID or not TWILIO_TOKEN:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env")
    if not TWILIO_FROM:
        raise RuntimeError("TWILIO_FROM must be set in .env (Twilio number or sender ID)")
    client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(body=message, from_=TWILIO_FROM, to=to_number)

def send_auto_sms(lead):
    if not SMS_ENABLED:
        print(f"Auto SMS skipped for lead {lead['id']}: SMS_ENABLED is false in .env")
        return
    try:
        if not lead.get("phone"):
            return
        if not TWILIO_SID:
            print(f"Auto SMS skipped for lead {lead['id']}: Twilio not configured in .env")
            return
        body = sms_body(first_name_of(lead["name"]), lead["car"], lead["offer"], lead.get("market_target") or "")
        send_sms(lead["phone"], body)
        mark_sms(lead["id"])
        print(f"Auto SMS sent to {lead['name']} ({lead['phone']})")
    except Exception as e:
        print(f"Auto SMS failed for lead {lead['id']}: {e}")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def poll_facebook_leads():
    if not FB_PAGE_TOKEN or not FB_PAGE_ID:
        return
    base = "https://graph.facebook.com/v19.0"
    try:
        url = f"{base}/{FB_PAGE_ID}/leadgen_forms?access_token={FB_PAGE_TOKEN}&limit=10"
        with urllib.request.urlopen(url) as r:
            forms = json.loads(r.read()).get("data", [])
    except Exception as e:
        print(f"FB poll: forms fetch failed: {e}")
        return
    for form in forms:
        try:
            leads_url = (f"{base}/{form['id']}/leads"
                         f"?access_token={FB_PAGE_TOKEN}&limit=25"
                         f"&fields=id,field_data,created_time")
            with urllib.request.urlopen(leads_url) as r:
                for lead in json.loads(r.read()).get("data", []):
                    _save_fb_lead(lead, form.get("name", ""))
        except Exception as e:
            print(f"FB poll: form {form.get('id')} failed: {e}")

def check_followups():
    fu1_due, fu2_due = get_due_followups()
    for lead in fu1_due:
        send_auto_followup(lead, 1)
    for lead in fu2_due:
        send_auto_followup(lead, 2)
    for lead in get_due_sms():
        send_auto_sms(lead)

scheduler = BackgroundScheduler(
    job_defaults={
        "coalesce": True,            # if the Mac slept and several ticks were missed, combine them into one run
        "misfire_grace_time": 3600,  # still fire even if up to 1 hour late (e.g. after Mac wake)
        "max_instances": 1,          # never let two checks overlap
    }
)
scheduler.add_job(check_followups, "interval", minutes=10, id="check_followups")
scheduler.add_job(poll_facebook_leads, "interval", minutes=5, id="poll_facebook_leads")
scheduler.start()

# Startup catch-up disabled — we don't want a flood of pending follow-ups firing
# the moment you restart the app. The 10-minute scheduler will pick up any due items
# on its next regular tick, which gives you time to pause/delete leads first if needed.

print("─" * 60)
print("Epping Car Buyer Lead Tool — running")
print("  Open:      http://localhost:5000")
print("  Scheduler: checks every 10 minutes")
if SMS_ENABLED:
    print("  Timeline:  SMS @ 24h  •  FU1 @ 2 days  •  FU2 @ 5 days")
else:
    print("  Timeline:  SMS PAUSED  •  FU1 @ 2 days  •  FU2 @ 5 days")
    print("  SMS:       disabled (SMS_ENABLED=false in .env — flip to true when Twilio is approved)")
print("─" * 60)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return "OK", 200

@app.route("/debug/email")
def debug_email():
    import socket
    result = {"host": SMTP_HOST, "port": SMTP_PORT, "user": YOUR_EMAIL or "(not set)", "password_set": bool(EMAIL_PASSWORD)}
    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 587:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
                result["connected"] = True
                server.starttls(context=context)
                result["starttls"] = True
                server.login(YOUR_EMAIL, EMAIL_PASSWORD)
                result["login"] = True
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=10) as server:
                result["connected"] = True
                server.login(YOUR_EMAIL, EMAIL_PASSWORD)
                result["login"] = True
        result["ok"] = True
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        result["error_type"] = type(e).__name__
    return jsonify(result)

def _fb_field(field_data, *keys):
    """Pull the first matching value from Facebook's field_data list."""
    for field in field_data:
        name = field.get("name", "").lower().replace(" ", "_")
        for key in keys:
            if key in name:
                vals = field.get("values") or []
                return vals[0] if vals else ""
    return ""

@app.route("/webhook/facebook", methods=["GET", "POST"])
def facebook_webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == FB_VERIFY_TOKEN:
            return request.args.get("hub.challenge", ""), 200
        return "Forbidden", 403

    payload = request.get_json(silent=True) or {}
    print(f"FB webhook: {json.dumps(payload)}")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "leadgen":
                continue
            leadgen_id = change.get("value", {}).get("leadgen_id")
            if not leadgen_id or not FB_PAGE_TOKEN:
                print("FB webhook: missing leadgen_id or page token")
                continue

            url = f"https://graph.facebook.com/v19.0/{leadgen_id}?access_token={FB_PAGE_TOKEN}"
            try:
                with urllib.request.urlopen(url) as resp:
                    lead_data = json.loads(resp.read())
            except Exception as e:
                print(f"FB Graph API error: {e}")
                continue

            _save_fb_lead(lead_data, "Facebook Ad")

    return "OK", 200

@app.route("/")
def index():
    return render_template("index.html", your_name=YOUR_NAME, your_phone=YOUR_PHONE)

@app.route("/send", methods=["POST"])
def send():
    data     = request.get_json()
    to_email = data.get("email", "").strip()
    body     = data.get("body", "").strip()
    subject  = data.get("subject", "")
    save     = data.get("save", False)
    lead_id  = data.get("lead_id")
    fu_num   = data.get("fu_num")

    if not to_email:
        return jsonify({"ok": False, "error": "No email address provided"}), 400

    try:
        send_email(to_email, subject, body)

        new_id = None
        if save:
            fu1_at = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
            fu2_at = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
            # Only schedule the 24h SMS if SMS is currently enabled.
            # If SMS is paused (Twilio under review), leave sms_send_at as NULL so nothing will auto-fire later.
            sms_at = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S") if SMS_ENABLED else None
            new_id = save_lead(data, fu1_at, fu2_at, sms_at)
        if lead_id and fu_num:
            mark_followup(lead_id, fu_num)

        return jsonify({"ok": True, "lead_id": new_id})
    except Exception as e:
        print(f"Send error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/send-sms", methods=["POST"])
def sms():
    data    = request.get_json()
    to      = data.get("phone", "").strip()
    message = data.get("message", "").strip()
    lead_id = data.get("lead_id")

    if not SMS_ENABLED:
        return jsonify({"ok": False, "error": "SMS is paused — flip SMS_ENABLED to true in .env once Twilio is set up"}), 400
    if not to:
        return jsonify({"ok": False, "error": "No phone number provided"}), 400
    if not TWILIO_SID:
        return jsonify({"ok": False, "error": "Twilio not configured in .env"}), 400

    try:
        send_sms(to, message)
        if lead_id:
            mark_sms(lead_id)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/status", methods=["POST"])
def status():
    data = request.get_json()
    update_status(data.get("lead_id"), data.get("status"))
    return jsonify({"ok": True})

@app.route("/api/leads")
def api_leads():
    leads = get_all_leads()
    return jsonify([dict(l) for l in leads])

@app.route("/api/facebook-leads")
def api_facebook_leads():
    return jsonify(get_facebook_inbox())

@app.route("/api/facebook-leads/<int:fb_id>/dismiss", methods=["POST"])
def dismiss_facebook_lead(fb_id):
    dismiss_fb_lead_db(fb_id)
    return jsonify({"ok": True})

@app.route("/leads")
def leads_page():
    return render_template("leads.html", leads=get_all_leads())

@app.route("/leads/export")
def export_leads():
    leads = get_all_leads()
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id","name","last_name","email","phone","car","reg","mileage","postcode",
        "offer","market_target","source","status","notes","created_at","fu1_sent_at","fu2_sent_at","sms_sent_at"
    ], extrasaction='ignore')
    writer.writeheader()
    writer.writerows(leads)
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"epping_leads_{datetime.now().strftime('%Y%m%d')}.csv"
    )

@app.route("/leads/delete/<int:lead_id>", methods=["POST"])
def delete(lead_id):
    delete_lead_db(lead_id)
    return jsonify({"ok": True})

@app.route("/leads/pause/<int:lead_id>", methods=["POST"])
def pause(lead_id):
    data = request.get_json() or {}
    paused = bool(data.get("paused", True))
    set_paused(lead_id, paused)
    return jsonify({"ok": True, "paused": paused})

@app.route("/leads/pause-all", methods=["POST"])
def pause_all():
    """Pause auto-messages for every lead in the DB. Useful when rebranding or stepping away."""
    with sqlite3.connect(DB_PATH) as con:
        before = con.execute("SELECT COUNT(*) FROM leads WHERE (paused IS NULL OR paused = 0)").fetchone()[0]
        con.execute("UPDATE leads SET paused = 1")
    return jsonify({"ok": True, "paused_count": before})

@app.route("/leads/list-for-sale/<int:lead_id>", methods=["POST"])
def list_for_sale(lead_id):
    """Bought this car? One click sends it to the Epping Car Sales stock list.
    Car, reg and mileage come straight from the lead; the advert description you
    paste in is saved with the car, so nothing needs typing twice."""
    data = request.get_json() or {}
    lead = get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "Lead not found"}), 404

    reg = (data.get("reg") or lead.get("reg") or "").strip()
    car = (data.get("car") or lead.get("car") or "").strip()
    if not reg or not car:
        return jsonify({"ok": False, "error": "Reg and car are needed — fill them in on the form"}), 400

    try:
        sales_add_stock({
            "reg": reg,
            "car": car,
            "year": (data.get("year") or "").strip(),
            "colour": (data.get("colour") or "").strip(),
            "mileage": (data.get("mileage") or lead.get("mileage") or "").strip(),
            "price": (data.get("price") or "").strip(),
            "status": "Available",
            "notes": (data.get("description") or "").strip(),
        })
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": f"{reg.upper()} is already in the Car Sales stock list"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # The car is yours now — mark the lead Bought and stop chasing the seller.
    update_status(lead_id, "Bought")
    set_paused(lead_id, True)
    return jsonify({"ok": True, "stock_url": "/sales/stock"})

@app.route("/leads/delete-all", methods=["POST"])
def delete_all():
    """Wipe every lead in the database. Destructive and irreversible."""
    with sqlite3.connect(DB_PATH) as con:
        count = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        con.execute("DELETE FROM leads")
        # Reset autoincrement so the next lead starts at id=1
        con.execute("DELETE FROM sqlite_sequence WHERE name='leads'")
    return jsonify({"ok": True, "deleted_count": count})

@app.route("/templates", methods=["GET"])
def templates_page():
    return render_template(
        "edit_templates.html",
        fu1_body=get_template("fu1_body"),
        fu2_body=get_template("fu2_body"),
        sign_off_preview=SIGN_OFF + "\n\n" + SIGNATURE_PLAIN,
        defaults=DEFAULT_TEMPLATES,
    )

@app.route("/templates/save", methods=["POST"])
def templates_save():
    data = request.get_json()
    key  = data.get("key", "")
    body = data.get("body", "")
    if key not in DEFAULT_TEMPLATES:
        return jsonify({"ok": False, "error": "Unknown template key"}), 400
    if not body.strip():
        return jsonify({"ok": False, "error": "Template cannot be empty"}), 400
    try:
        save_template(key, body)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/templates/preview", methods=["POST"])
def templates_preview():
    """Render the template body with sample lead data so the user can see what it looks like."""
    data = request.get_json()
    body = data.get("body", "")
    rendered = render_template_text(
        body,
        name="John",
        car="2018 Ford Focus",
        price="4500",
        reg="AB18 XYZ",
        SIGN_OFF=SIGN_OFF + "\n\n" + SIGNATURE_PLAIN,
    )
    return jsonify({"ok": True, "preview": rendered})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=port)
