import csv
import io
from datetime import datetime

from flask import request, send_file

from app import app, get_all_leads


@app.route("/leads/marketing-export")
def export_marketing_leads():
    """Download a simple marketing CSV containing first name and email for every saved lead."""
    leads = get_all_leads()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["First Name", "Email"])

    for lead in leads:
        full_name = str(lead.get("name") or "").strip()
        first_name = full_name.split()[0] if full_name else ""
        email = str(lead.get("email") or "")
        writer.writerow([first_name, email])

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"epping_marketing_leads_{datetime.now().strftime('%Y%m%d')}.csv",
    )


@app.after_request
def add_marketing_csv_button(response):
    """Add a one-click Marketing CSV export beside the existing full CSV export on All Leads."""
    if request.path == "/leads" and response.content_type.startswith("text/html"):
        html = response.get_data(as_text=True)
        existing_button = '<a href="/leads/export" class="btn">Export CSV</a>'
        marketing_button = '<a href="/leads/marketing-export" class="btn">Marketing CSV</a>'
        if existing_button in html and marketing_button not in html:
            html = html.replace(
                existing_button,
                existing_button + "\n      " + marketing_button,
                1,
            )
            response.set_data(html)
    return response
