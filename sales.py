# ── Epping Car Sales — selling side of the merged tool ────────────────────────
# This is the old epping-sales-tool app folded in as a Flask Blueprint.
# Everything lives under /sales/… and uses its own database (sales.db) and its
# own email account (SALES_* keys in .env), so the buying side is untouched.

import os
import re
import smtplib
import ssl
import sqlite3
import csv
import io
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from flask import Blueprint, render_template, request, jsonify, send_file
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from twilio.rest import Client as TwilioClient

load_dotenv()

sales_bp = Blueprint("sales", __name__, url_prefix="/sales")

# SALES_* keys let the selling side use its own email/SMS identity.
# Non-critical values fall back to the shared ones so nothing breaks.
YOUR_NAME      = os.getenv("SALES_YOUR_NAME") or os.getenv("YOUR_NAME", "Henry")
YOUR_PHONE     = os.getenv("SALES_YOUR_PHONE") or os.getenv("YOUR_PHONE", "+44 1992 367909")
YOUR_EMAIL     = os.getenv("SALES_EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("SALES_EMAIL_PASSWORD")
DISPLAY_NAME   = os.getenv("SALES_DISPLAY_NAME", "Epping Car Sales")
SMTP_HOST      = os.getenv("SALES_SMTP_HOST", "smtp.zoho.eu")
SMTP_PORT      = int(os.getenv("SALES_SMTP_PORT", "465"))
TWILIO_SID     = os.getenv("SALES_TWILIO_ACCOUNT_SID") or os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN   = os.getenv("SALES_TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM    = os.getenv("SALES_TWILIO_FROM_NUMBER")
SMS_ENABLED    = os.getenv("SALES_SMS_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")

DB_PATH = os.path.join(os.path.dirname(__file__), "sales.db")

# The buying side's database — read-only here, used so a reg search on the sales
# enquiry form can find cars you bought through Epping Car Buyer.
BUYER_DB_PATH = os.path.join(os.path.dirname(__file__), "leads.db")

# Logo embedded as the email signature (an email-sized copy, kept small on purpose)
LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "ECSlogo_email.png")


# ── Default email templates ───────────────────────────────────────────────────

DEFAULT_TEMPLATES = {
    "fu1_body": (
        "Hi {name},\n\n"
        "I just wanted to follow up on your enquiry about our {car} — I wanted to make sure my previous email reached you.\n\n"
        "The car is still available at £{price} and has been well looked after. We're happy to arrange a viewing at a time that suits you, and we're flexible on times.\n\n"
        "If you'd like to book a viewing or have any questions, feel free to reply here or give me a call.\n\n"
        "{SIGN_OFF}"
    ),
    "fu2_body": (
        "Hi {name},\n\n"
        "This will be my last email on this — I just wanted to check whether you're still interested in our {car} at £{price}.\n\n"
        "If you've found something else or changed your mind, no problem at all — a quick reply to let me know would be much appreciated.\n\n"
        "If you're still interested, the car is available and I'm happy to arrange a viewing around a time that suits you.\n\n"
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
                interested_reg   TEXT,
                postcode         TEXT,
                car              TEXT,
                reg              TEXT,
                mileage          TEXT,
                price            TEXT,
                source           TEXT DEFAULT 'Facebook',
                notes            TEXT,
                status           TEXT DEFAULT 'New',
                created_at       TEXT DEFAULT (datetime('now','localtime')),
                fu1_send_at      TEXT,
                fu2_send_at      TEXT,
                fu1_sent_at      TEXT,
                fu2_sent_at      TEXT,
                sms_send_at      TEXT,
                sms_sent_at      TEXT,
                paused           INTEGER DEFAULT 0
            )
        """)
        for col in ["interested_reg TEXT", "postcode TEXT", "fu1_send_at TEXT", "fu2_send_at TEXT",
                    "sms_send_at TEXT", "sms_sent_at TEXT", "status TEXT", "paused INTEGER DEFAULT 0"]:
            try:
                con.execute(f"ALTER TABLE leads ADD COLUMN {col}")
            except Exception:
                pass

        con.execute("""
            CREATE TABLE IF NOT EXISTS stock (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                reg        TEXT NOT NULL UNIQUE,
                car        TEXT NOT NULL,
                year       TEXT,
                colour     TEXT,
                mileage    TEXT,
                price      TEXT,
                status     TEXT DEFAULT 'Available',
                notes      TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                key        TEXT PRIMARY KEY,
                body       TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        for key, body in DEFAULT_TEMPLATES.items():
            con.execute("INSERT OR IGNORE INTO templates (key, body) VALUES (?, ?)", (key, body))


# ── Stock helpers ─────────────────────────────────────────────────────────────

def get_all_stock():
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM stock ORDER BY created_at DESC").fetchall()]

def get_stock_by_reg(reg):
    reg = reg.upper().replace(" ", "")
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM stock WHERE UPPER(REPLACE(reg,' ','')) = ?", (reg,)
        ).fetchone()
        return dict(row) if row else None

def add_stock(data):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO stock (reg, car, year, colour, mileage, price, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("reg", "").upper().strip(),
            data.get("car", "").strip(),
            data.get("year", "").strip(),
            data.get("colour", "").strip(),
            data.get("mileage", "").strip(),
            data.get("price", "").strip(),
            data.get("status", "Available"),
            data.get("notes", "").strip(),
        ))

def update_stock(stock_id, data):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            UPDATE stock SET car=?, year=?, colour=?, mileage=?, price=?, status=?, notes=?
            WHERE id=?
        """, (
            data.get("car", ""),
            data.get("year", ""),
            data.get("colour", ""),
            data.get("mileage", ""),
            data.get("price", ""),
            data.get("status", "Available"),
            data.get("notes", ""),
            stock_id,
        ))

def delete_stock(stock_id):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM stock WHERE id = ?", (stock_id,))


# ── Lead helpers ──────────────────────────────────────────────────────────────

def save_lead(data, fu1_at, fu2_at, sms_at):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""
            INSERT INTO leads (name, email, phone, interested_reg, postcode, car, reg, mileage, price, source, notes, fu1_send_at, fu2_send_at, sms_send_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("name"), data.get("email"), data.get("phone"),
            data.get("interested_reg"), data.get("postcode"),
            data.get("car"), data.get("reg"), data.get("mileage"),
            data.get("price"),
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

def get_due_sms():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE sms_send_at <= ? AND sms_sent_at IS NULL AND phone IS NOT NULL AND phone != '' AND (paused IS NULL OR paused = 0)",
            (now,)
        ).fetchall()]

def set_paused(lead_id, paused):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE leads SET paused = ? WHERE id = ?", (1 if paused else 0, lead_id))


# ── Template helpers ──────────────────────────────────────────────────────────

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
    for k, v in kwargs.items():
        body = body.replace("{" + k + "}", str(v))
    return body

init_db()


# ── Email ─────────────────────────────────────────────────────────────────────

# The body text ends with just "Kind regards," — the full signature block
# (name, title, contact details and logo) is added automatically on send.
SIGN_OFF = "Kind regards,"

PHONE_DISPLAY   = YOUR_PHONE or ""
SIG_WEBSITE     = "www.eppingcarsales.com"
SIG_ADDRESS     = "Patches Farm, Galley Hill, EN92AG"
SIGNATURE_PLAIN = f"{YOUR_NAME}\n{PHONE_DISPLAY}\n{YOUR_EMAIL}\n{SIG_WEBSITE}\n{SIG_ADDRESS}"

def initial_email_body(name, car, reg, price, mileage, greeting):
    mileage_line = f" It has {mileage} miles on the clock." if mileage else ""
    return (
        f"Hi {name},\n\n"
        f"Thank you for your enquiry. {greeting}\n\n"
        f"I'm pleased to let you know the {car} ({reg}) is still available at £{price}.{mileage_line} "
        f"It has been well looked after and we'd love to arrange a viewing at a time that suits you — "
        f"we're flexible on times.\n\n"
        f"If you have any questions beforehand, feel free to reply here or contact me by phone or WhatsApp.\n\n"
        f"{SIGN_OFF}"
    )

def fu1_body_rendered(name, car, price):
    return render_template_text(get_template("fu1_body"),
                                name=name, car=car, price=price, SIGN_OFF=SIGN_OFF)

def fu2_body_rendered(name, car, price):
    return render_template_text(get_template("fu2_body"),
                                name=name, car=car, price=price, SIGN_OFF=SIGN_OFF)

def build_html(plain_body):
    """HTML version of the email, ending with the Epping Car Sales logo as the signature."""
    paragraphs = ""
    for line in plain_body.split("\n"):
        if line.strip():
            paragraphs += f"<p style='margin:0 0 16px 0;'>{line}</p>"
    logo_html = ""
    if os.path.exists(LOGO_PATH):
        logo_html = ("<img src='cid:brandlogo' width='170' "
                     "style='width:170px;height:auto;border:0;display:block;margin-top:12px;' "
                     "alt='Epping Car Sales' />")
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
    if not YOUR_EMAIL or not EMAIL_PASSWORD:
        raise RuntimeError("SALES_EMAIL_ADDRESS and SALES_EMAIL_PASSWORD must be set in .env")
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
            logo.add_header("Content-Disposition", "inline", filename="ECSlogo.png")
            msg.attach(logo)
        except Exception:
            pass  # emails still send fine without the logo

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(YOUR_EMAIL, EMAIL_PASSWORD)
        server.send_message(msg)

def send_auto_followup(lead, num):
    try:
        body = fu1_body_rendered(lead["name"], lead["car"], lead["price"]) if num == 1 \
               else fu2_body_rendered(lead["name"], lead["car"], lead["price"])
        subject = f"{lead['interested_reg'] or lead['reg']}, {'Following Up' if num == 1 else 'Last Follow-up'} — Epping Car Sales"
        send_email(lead["email"], subject, body)
        mark_followup(lead["id"], num)
        print(f"[sales] Auto follow-up {num} sent to {lead['name']} ({lead['email']})")
    except Exception as e:
        print(f"[sales] Auto follow-up {num} failed for lead {lead['id']}: {e}")


# ── SMS ───────────────────────────────────────────────────────────────────────

def sms_body(name, car, price):
    return (
        f"Hi {name}, this is {YOUR_NAME} from Epping Car Sales — thank you for your enquiry on our {car}. "
        f"I've just emailed you the details; the car is available at £{price} and we'd love to arrange a viewing. "
        f"Reply here or WhatsApp/call me on {YOUR_PHONE}."
    )

def send_sms(to_number, message):
    client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(body=message, from_="ECSales", to=to_number)

def send_auto_sms(lead):
    if not SMS_ENABLED:
        return
    try:
        if not lead.get("phone") or not TWILIO_SID:
            return
        body = sms_body(lead["name"], lead["car"], lead["price"])
        send_sms(lead["phone"], body)
        mark_sms(lead["id"])
        print(f"[sales] Auto SMS sent to {lead['name']} ({lead['phone']})")
    except Exception as e:
        print(f"[sales] Auto SMS failed for lead {lead['id']}: {e}")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def check_sales_followups():
    fu1_due, fu2_due = get_due_followups()
    for lead in fu1_due:
        send_auto_followup(lead, 1)
    for lead in fu2_due:
        send_auto_followup(lead, 2)
    for lead in get_due_sms():
        send_auto_sms(lead)

sales_scheduler = BackgroundScheduler(
    job_defaults={"coalesce": True, "misfire_grace_time": 3600, "max_instances": 1}
)
sales_scheduler.add_job(check_sales_followups, "interval", minutes=10, id="check_sales_followups")
sales_scheduler.start()


# ── Routes (all under /sales) ─────────────────────────────────────────────────

@sales_bp.route("/")
def index():
    return render_template("sales_index.html", your_name=YOUR_NAME, your_phone=YOUR_PHONE)

def get_buyer_lead_by_reg(reg):
    """Look a reg up in the Epping Car Buyer leads — read-only."""
    reg = reg.upper().replace(" ", "")
    try:
        with sqlite3.connect(BUYER_DB_PATH) as con:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM leads WHERE UPPER(REPLACE(COALESCE(reg,''),' ','')) = ? ORDER BY id DESC LIMIT 1",
                (reg,)
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None

@sales_bp.route("/api/stock/<path:reg>")
def stock_lookup(reg):
    car = get_stock_by_reg(reg)
    if car:
        return jsonify({"ok": True, "car": car})
    # Not in stock — but is it a car bought through Epping Car Buyer?
    lead = get_buyer_lead_by_reg(reg)
    if lead:
        year = re.search(r"\b(19[89]\d|20[0-4]\d)\b", lead.get("car") or "")
        return jsonify({"ok": True, "from_lead": True, "car": {
            "car":     lead.get("car") or "",
            "reg":     (lead.get("reg") or reg).upper(),
            "mileage": lead.get("mileage") or "",
            "price":   "",
            "year":    year.group(0) if year else "",
            "colour":  "",
            "seller":  lead.get("name") or "",
            "lead_status": lead.get("status") or "",
        }})
    return jsonify({"ok": False})

@sales_bp.route("/send", methods=["POST"])
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
            sms_at = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S") if SMS_ENABLED else None
            new_id = save_lead(data, fu1_at, fu2_at, sms_at)
        if lead_id and fu_num:
            mark_followup(lead_id, fu_num)
        return jsonify({"ok": True, "lead_id": new_id})
    except Exception as e:
        print(f"[sales] Send error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@sales_bp.route("/send-sms", methods=["POST"])
def sms():
    data    = request.get_json()
    to      = data.get("phone", "").strip()
    message = data.get("message", "").strip()
    lead_id = data.get("lead_id")

    if not SMS_ENABLED:
        return jsonify({"ok": False, "error": "SMS is paused — flip SALES_SMS_ENABLED to true in .env once Twilio is approved"}), 400
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

@sales_bp.route("/status", methods=["POST"])
def status():
    data = request.get_json()
    update_status(data.get("lead_id"), data.get("status"))
    return jsonify({"ok": True})

@sales_bp.route("/leads")
def leads_page():
    return render_template("sales_leads.html", leads=get_all_leads())

@sales_bp.route("/leads/export")
def export_leads():
    leads = get_all_leads()
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id", "name", "email", "phone", "interested_reg", "postcode",
        "car", "reg", "mileage", "price", "source", "status", "notes",
        "created_at", "fu1_sent_at", "fu2_sent_at", "sms_sent_at"
    ], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(leads)
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"epping_sales_leads_{datetime.now().strftime('%Y%m%d')}.csv"
    )

@sales_bp.route("/leads/delete/<int:lead_id>", methods=["POST"])
def delete_lead(lead_id):
    delete_lead_db(lead_id)
    return jsonify({"ok": True})

@sales_bp.route("/leads/pause/<int:lead_id>", methods=["POST"])
def pause_lead(lead_id):
    data = request.get_json() or {}
    set_paused(lead_id, bool(data.get("paused", True)))
    return jsonify({"ok": True})

@sales_bp.route("/leads/pause-all", methods=["POST"])
def pause_all():
    with sqlite3.connect(DB_PATH) as con:
        before = con.execute("SELECT COUNT(*) FROM leads WHERE (paused IS NULL OR paused = 0)").fetchone()[0]
        con.execute("UPDATE leads SET paused = 1")
    return jsonify({"ok": True, "paused_count": before})

@sales_bp.route("/leads/delete-all", methods=["POST"])
def delete_all():
    with sqlite3.connect(DB_PATH) as con:
        count = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        con.execute("DELETE FROM leads")
        con.execute("DELETE FROM sqlite_sequence WHERE name='leads'")
    return jsonify({"ok": True, "deleted_count": count})

@sales_bp.route("/stock")
def stock_page():
    return render_template("sales_stock.html", stock=get_all_stock())

@sales_bp.route("/stock/add", methods=["POST"])
def stock_add():
    data = request.get_json()
    if not data.get("reg") or not data.get("car"):
        return jsonify({"ok": False, "error": "Reg and car name are required"}), 400
    try:
        add_stock(data)
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": f"Reg {data.get('reg','').upper()} is already in stock"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@sales_bp.route("/stock/update/<int:stock_id>", methods=["POST"])
def stock_update(stock_id):
    data = request.get_json()
    try:
        update_stock(stock_id, data)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@sales_bp.route("/stock/delete/<int:stock_id>", methods=["POST"])
def stock_delete(stock_id):
    delete_stock(stock_id)
    return jsonify({"ok": True})

@sales_bp.route("/templates", methods=["GET"])
def templates_page():
    return render_template(
        "sales_edit_templates.html",
        fu1_body=get_template("fu1_body"),
        fu2_body=get_template("fu2_body"),
        sign_off_preview=SIGN_OFF + "\n\n" + SIGNATURE_PLAIN,
        defaults=DEFAULT_TEMPLATES,
    )

@sales_bp.route("/templates/save", methods=["POST"])
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

@sales_bp.route("/templates/preview", methods=["POST"])
def templates_preview():
    data = request.get_json()
    body = data.get("body", "")
    rendered = render_template_text(
        body,
        name="John",
        car="2020 Ford Focus ST",
        price="18,995",
        reg="AB20 XYZ",
        SIGN_OFF=SIGN_OFF + "\n\n" + SIGNATURE_PLAIN,
    )
    return jsonify({"ok": True, "preview": rendered})
