# Epping Car Buyer + Epping Car Sales — one tool

Both sides of the business in one app, running on http://localhost:5000

- **Buying** (Epping Car Buyer) — everything at the usual pages: lead form at `/`, all leads at `/leads`.
- **Selling** (Epping Car Sales) — merged in at `/sales/`: buyer enquiries, the stock list (`/sales/stock`) and sales email templates. Uses its own database (`sales.db`) and its own email account (`SALES_*` keys in `.env`).
- **Bought a car?** On the All Leads page, click **List for sale** on the lead, paste your advert description, and the car goes straight into the Car Sales stock list — car, reg and mileage are filled in from the lead, and the seller's auto-messages are paused automatically.

The old separate `epping-sales-tool` folder is no longer needed — don't run it, or emails could send twice. It's kept only as a backup.

## Setup

1. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

2. **Configure your credentials**

   Edit `.env` and fill in your details:
   ```
   GMAIL_ADDRESS=you@gmail.com
   GMAIL_APP_PASSWORD=your_16_character_app_password
   YOUR_NAME=Henry
   YOUR_PHONE=+44 1992 367909
   ```

   To get a Gmail App Password:
   - Go to myaccount.google.com
   - Security → 2-Step Verification (must be enabled)
   - Search for "App passwords" → create one → copy the 16-character code

3. **Run in PyCharm**

   Open the project folder in PyCharm, then either:
   - Right-click `app.py` → Run
   - Or in the terminal: `python app.py`

4. **Open in browser**

   Go to: http://localhost:5000

## Notes

- `.env` is in `.gitignore` — your credentials will never be pushed to GitHub
- The app only runs while PyCharm is open
- To stop it, press the red square in PyCharm or Ctrl+C in the terminal
