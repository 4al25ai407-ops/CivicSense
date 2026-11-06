# CivicSense AI

Citizens report civic issues (photo + description + GPS-pinned location with an editable address). Administrators review each report, update status/priority, and reply to the citizen. Citizens see replies and can answer back.

## Run locally (Windows PowerShell)
```powershell
cd civicsense_ai
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SECRET_KEY in .env
flask --app app init-db
flask --app app seed-admins   # creates up to 4 admins (or create-admin for one)
python app.py
```
Open http://127.0.0.1:5000

## How it works
1. Citizen registers, signs in, clicks **Report issue**, uploads a photo and types the location.
2. Admin signs in (account made with `create-admin`) and opens the **Admin dashboard**.
3. Admin clicks **Review & respond**: change status/priority, add an update note, and write a reply.
4. Citizen opens the report from **My reports** to see the reply and send a message back.
5. The dashboard flags reports where the citizen replied last.

## Deploy for everyone (Render)
- Build command: `pip install -r requirements.txt`  Start command: `gunicorn app:app`
- Environment variables: `SECRET_KEY`, `COOKIE_SECURE=1`, optional `GEMINI_API_KEY`, and `DATABASE_URL` (Render PostgreSQL; also add `psycopg2-binary` to requirements.txt).
- Free hosts erase local files on restart: use PostgreSQL and external image storage for anything beyond a demo.
- Create the admin once from the host's shell: `flask --app app create-admin`.

## Notes
Hackathon MVP, not an official municipal channel. Not for emergencies. No email/SMS notifications yet.

## GPS location
- Click **Use my current location**, allow the browser permission, and wait a few seconds: the page keeps the most accurate reading, shows the accuracy in metres, and fills in the address.
- Phones give true GPS accuracy (a few metres). Laptops usually have no GPS, so they report an approximate Wi-Fi/IP location; drag the pin or tap the map to set the exact spot.
- Location works on `http://127.0.0.1` locally and on any `https://` site. It does not work on plain `http://192.168...` addresses.
- Address lookup uses OpenStreetMap Nominatim from the server, so the host needs internet access. If it fails, the coordinates are filled in and the address can be typed.

## Admins (maximum 4)
- `flask --app app seed-admins` creates admins one after another until 4 exist; `create-admin` adds one.
- Signed-in admins can open **Team** to add a new admin, promote a registered user, or remove admin access. Change the cap with `ADMIN_LIMIT` in `.env`.
- Emergency recovery: `flask --app app reset-password`.

## Forgot password
- **Forgot your password?** on the sign-in page sends a single-use link valid for 30 minutes.
- Set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` to send real emails (e.g. a Gmail app password). Without SMTP, the link is printed in the server terminal.
- For a quick demo set `SHOW_RESET_LINK=1` to show the link on screen. Never enable that on a public site.

## Sign-in limits
Only failed sign-ins count (`LOGIN_LIMIT`, default 15 per 15 minutes). Restarting the app clears the counters.

## Setting up email (Gmail)
1. Turn on 2-Step Verification in your Google account, then create an App Password at https://myaccount.google.com/apppasswords
2. In `.env` set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER` and `SMTP_FROM` to your Gmail address, and `SMTP_PASSWORD` to the 16-character app password.
3. Restart the app and run `flask --app app test-email` to confirm it works.
