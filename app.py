import os, math, uuid, io, secrets, smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, redirect, url_for, flash, request, abort, send_from_directory, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from wtforms import StringField, PasswordField, SelectField, TextAreaField, FloatField, SubmitField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Optional, NumberRange
from PIL import Image, ImageOps
from dotenv import load_dotenv

load_dotenv()
ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(ROOT, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_urlsafe(48),
    SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(ROOT, "civicsense.db")),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    WTF_CSRF_TIME_LIMIT=3600,
)
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to open your dashboard."
login_manager.login_message_category = "info"
limiter = Limiter(
    get_remote_address, app=app, storage_uri="memory://",
    default_limits=["3000 per day", "600 per hour"],
    # Pages' own CSS/JS/photos must never eat the visitor's allowance
    default_limits_exempt_when=lambda: request.endpoint in ("static", "uploaded_file"),
)
MAX_ADMINS = int(os.environ.get("ADMIN_LIMIT", "4"))
LOGIN_LIMIT = os.environ.get("LOGIN_LIMIT", "15 per 15 minutes")  # counts FAILED sign-ins only
RESET_TOKEN_MINUTES = 30
def _failed(response):
    """Only attempts that did not succeed (no redirect) use up the allowance."""
    return response.status_code != 302

CATEGORIES = ["Pothole / Road Damage", "Garbage Accumulation", "Water Leakage", "Blocked Drainage", "Streetlight Damage", "Other"]
STATUSES = ["Submitted", "Under Review", "In Progress", "Resolved", "Rejected"]
PRIORITIES = ["Low", "Medium", "High", "Critical"]
ALLOWED_MIMES = {"JPEG", "PNG", "WEBP"}

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(12), nullable=False, default="citizen")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(24), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(1500), nullable=False)
    category = db.Column(db.String(60), nullable=False)
    location = db.Column(db.String(200))  # plain-text location typed by the citizen
    # Legacy columns kept so existing databases keep working; no longer collected.
    latitude = db.Column(db.Float, nullable=True, default=0.0)
    longitude = db.Column(db.Float, nullable=True, default=0.0)
    priority = db.Column(db.String(12), nullable=False, default="Medium")
    status = db.Column(db.String(20), nullable=False, default="Submitted")
    image_name = db.Column(db.String(100), nullable=False)
    ai_suggestion = db.Column(db.String(100))
    ai_confidence = db.Column(db.Float)
    duplicate_of = db.Column(db.Integer, db.ForeignKey("complaint.id"))
    admin_note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id"), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    details = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Message(db.Model):
    """Conversation between the citizen who reported an issue and the administrators."""
    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    sender = db.relationship("User")

class RegisterForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=80)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    password = PasswordField("Password (12+ characters)", validators=[DataRequired(), Length(min=12, max=128)])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Create account")

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign in")

class ForgotForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Send reset link")

class ResetForm(FlaskForm):
    password = PasswordField("New password (12+ characters)", validators=[DataRequired(), Length(min=12, max=128)])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password")])
    submit = SubmitField("Set new password")

class AdminCreateForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=80)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    password = PasswordField("Password (12+ characters)", validators=[DataRequired(), Length(min=12, max=128)])
    submit = SubmitField("Add admin")

class PromoteForm(FlaskForm):
    email = StringField("Email of a registered user", validators=[DataRequired(), Email()])
    submit = SubmitField("Make admin")

class ComplaintForm(FlaskForm):
    title = StringField("Issue title", validators=[DataRequired(), Length(min=5, max=120)])
    category = SelectField("Issue category", choices=[(x, x) for x in CATEGORIES], validators=[DataRequired()])
    description = TextAreaField("Describe the issue", validators=[DataRequired(), Length(min=15, max=1500)])
    location = StringField("Location (area, street or landmark)", validators=[DataRequired(), Length(min=3, max=200)])
    # Filled automatically by the GPS / map picker; optional so typing a place still works.
    latitude = FloatField("Latitude", validators=[Optional(), NumberRange(min=-90, max=90)])
    longitude = FloatField("Longitude", validators=[Optional(), NumberRange(min=-180, max=180)])
    submit = SubmitField("Submit report")

class MessageForm(FlaskForm):
    body = TextAreaField("Message", validators=[DataRequired(), Length(min=2, max=1000)])
    submit = SubmitField("Send")

class AdminUpdateForm(FlaskForm):
    status = SelectField("Status", choices=[(x, x) for x in STATUSES], validators=[DataRequired()])
    priority = SelectField("Priority", choices=[(x, x) for x in PRIORITIES], validators=[DataRequired()])
    admin_note = StringField("Update note", validators=[Optional(), Length(max=500)])
    reply = TextAreaField("Reply to the citizen (optional)", validators=[Optional(), Length(max=1000)])
    submit = SubmitField("Save update")

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapped

def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))

def save_safe_image(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("Please attach a photograph.")
    try:
        raw = file_storage.read()
        if len(raw) > 5 * 1024 * 1024:
            raise ValueError("Image must be 5 MB or smaller.")
        image = Image.open(io.BytesIO(raw))
        image.verify()
        image = Image.open(io.BytesIO(raw))
        if image.format not in ALLOWED_MIMES:
            raise ValueError("Only JPEG, PNG, and WEBP images are accepted.")
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1800, 1800))
        name = uuid.uuid4().hex + ".jpg"
        image.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85, optimize=True)
        return name
    except ValueError:
        raise
    except Exception:
        raise ValueError("The uploaded file is not a valid supported image.")

def ai_suggest_category(image_path):
    # Optional Gemini integration. AI output is advisory and never silently overrides citizen/admin decisions.
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, None
    try:
        import requests, base64, json
        with open(image_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        prompt = ("Inspect this civic-infrastructure photograph. Return ONLY JSON with keys "
                  "'category' and 'confidence'. category must be one of: " + ", ".join(CATEGORIES) +
                  ". If uncertain, use Other. Confidence must be a number from 0 to 1. Do not infer location or people.")
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            params={"key": api_key},
            json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":"image/jpeg","data":encoded}}]}],
                  "generationConfig":{"temperature":0,"responseMimeType":"application/json"}},
            timeout=15
        )
        response.raise_for_status()
        result = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(result)
        category = data.get("category")
        confidence = float(data.get("confidence", 0))
        if category not in CATEGORIES or not 0 <= confidence <= 1:
            return None, None
        return category, confidence
    except Exception:
        app.logger.exception("AI classification unavailable")
        return None, None

def ensure_schema():
    """Create new tables and add the `location` column to an existing database."""
    db.create_all()
    from sqlalchemy import inspect, text
    cols = [c["name"] for c in inspect(db.engine).get_columns("complaint")]
    if "location" not in cols:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE complaint ADD COLUMN location VARCHAR(200)"))

def normalise_place(value):
    return " ".join((value or "").lower().split())

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "script-src 'self' https://unpkg.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response

@app.route("/")
def index():
    recent = Complaint.query.filter_by(status="Resolved").order_by(Complaint.updated_at.desc()).limit(5).all()
    totals = {"total": Complaint.query.count(), "resolved": Complaint.query.filter_by(status="Resolved").count()}
    return render_template("index.html", recent=recent, totals=totals)

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("15 per hour", methods=["POST"], deduct_when=_failed)
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
        else:
            user = User(name=form.name.data.strip(), email=email, role="citizen")
            user.set_password(form.password.data)
            db.session.add(user); db.session.commit()
            login_user(user)
            flash("Account created. Welcome to CivicSense AI.", "success")
            return redirect(url_for("dashboard"))
    return render_template("register.html", form=form)

@app.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: LOGIN_LIMIT, methods=["POST"], deduct_when=_failed)
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.strip().lower()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:  # same-site paths only
                return redirect(nxt)
            return redirect(url_for("admin_dashboard" if user.role == "admin" else "dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html", form=form)

# ---------- Forgot / reset password ----------
_reset_serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset")

def make_reset_token(user):
    # The password-hash fragment makes each token single-use: it dies as soon as the password changes.
    return _reset_serializer.dumps({"id": user.id, "h": user.password_hash[-16:]})

def user_from_reset_token(token):
    try:
        data = _reset_serializer.loads(token, max_age=RESET_TOKEN_MINUTES * 60)
    except (BadSignature, SignatureExpired):
        return None
    user = db.session.get(User, data.get("id"))
    if not user or user.password_hash[-16:] != data.get("h"):
        return None
    return user

def smtp_configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))

def send_mail(to, subject, body):
    """Send an email through SMTP. Returns (ok, error_text)."""
    if not smtp_configured():
        return False, "SMTP_HOST, SMTP_USER and SMTP_PASSWORD are not all set in .env"
    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    user = os.environ["SMTP_USER"].strip()
    password = os.environ["SMTP_PASSWORD"].replace(" ", "")  # Gmail shows app passwords with spaces
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = (os.environ.get("SMTP_FROM") or user).strip()
    msg["To"] = to
    msg.set_content(body)
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            smtp = smtplib.SMTP(host, port, timeout=15)
            smtp.ehlo(); smtp.starttls(); smtp.ehlo()
        with smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, "The email provider rejected the username/password. For Gmail use a 16-character App Password, not your normal password."
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

def send_reset_email(user, link):
    """Email the reset link; if email is not working, print the link in the server console."""
    ok, error = send_mail(
        user.email, "Reset your CivicSense AI password",
        f"Hello {user.name},\n\nUse this link to set a new password (valid for {RESET_TOKEN_MINUTES} minutes):\n{link}\n\n"
        "If you did not ask for this, ignore this email; your password stays the same.\n")
    if ok:
        app.logger.info("Password reset email sent to %s", user.email)
    else:
        app.logger.warning("Reset email NOT sent to %s (%s). Link for manual use: %s", user.email, error, link)
    return ok

@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("8 per 15 minutes", methods=["POST"])
def forgot_password():
    form = ForgotForm()
    demo_link = None
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.strip().lower()).first()
        if user:
            token = make_reset_token(user)
            base = os.environ.get("PUBLIC_URL", "").rstrip("/")
            link = (base + url_for("reset_password", token=token)) if base else url_for("reset_password", token=token, _external=True)
            send_reset_email(user, link)
            # DEMO ONLY: show the link on screen when SHOW_RESET_LINK=1 (never enable on a public site)
            if os.environ.get("SHOW_RESET_LINK") == "1":
                demo_link = link
        flash("If that email is registered, a password reset link has been sent.", "success")
        if not demo_link:
            return redirect(url_for("login"))
    return render_template("forgot.html", form=form, demo_link=demo_link)

@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("15 per 15 minutes", methods=["POST"])
def reset_password(token):
    user = user_from_reset_token(token)
    if not user:
        flash("That reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("forgot_password"))
    form = ResetForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash("Password updated. You can now sign in.", "success")
        return redirect(url_for("login"))
    return render_template("reset.html", form=form)

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    complaints = Complaint.query.filter_by(user_id=current_user.id).order_by(Complaint.created_at.desc()).all()
    return render_template("dashboard.html", complaints=complaints)

@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    form = ComplaintForm()
    if form.validate_on_submit():
        try:
            image_name = save_safe_image(request.files.get("photo"))
            category = form.category.data
            ai_category, confidence = ai_suggest_category(os.path.join(UPLOAD_DIR, image_name))
            # AI is advisory; only auto-suggest when confidence is strong. Citizen selection remains authoritative.
            suggestion = ai_category if ai_category and confidence >= 0.80 else None
            if suggestion:
                category = suggestion
            place = form.location.data.strip()
            lat, lon = form.latitude.data, form.longitude.data
            has_gps = lat is not None and lon is not None
            candidates = Complaint.query.filter_by(category=category).filter(Complaint.status != "Resolved").all()
            def is_dup(c):
                if has_gps and c.latitude and c.longitude:
                    return distance_m(c.latitude, c.longitude, lat, lon) <= 50
                return normalise_place(c.location) == normalise_place(place)
            duplicate = next((c for c in candidates if is_dup(c)), None)
            complaint = Complaint(
                public_id="CS-" + datetime.now().strftime("%y%m%d") + "-" + secrets.token_hex(3).upper(),
                user_id=current_user.id, title=form.title.data.strip(), description=form.description.data.strip(),
                category=category, location=place,
                latitude=lat if has_gps else 0.0, longitude=lon if has_gps else 0.0,
                priority="Medium", status="Submitted", image_name=image_name,
                ai_suggestion=ai_category, ai_confidence=confidence, duplicate_of=duplicate.id if duplicate else None
            )
            db.session.add(complaint); db.session.commit()
            flash("Report submitted. Your tracking ID is " + complaint.public_id +
                  (". A similar nearby report may already exist; an administrator will verify it." if duplicate else ""), "success")
            return redirect(url_for("dashboard"))
        except ValueError as e:
            flash(str(e), "danger")
    return render_template("report.html", form=form)

@app.route("/api/reverse-geocode")
@login_required
@limiter.limit("30 per minute")
def reverse_geocode():
    """Turn GPS coordinates into a readable address (OpenStreetMap Nominatim, done server-side)."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
    except ValueError:
        return jsonify(error="Invalid coordinates."), 400
    try:
        import requests
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 0},
                         headers={"User-Agent": "CivicSenseAI-hackathon/1.0"}, timeout=8)
        r.raise_for_status()
        name = (r.json().get("display_name") or "").strip()
        if not name:
            return jsonify(error="No address found."), 404
        return jsonify(address=name[:200])
    except Exception:
        app.logger.exception("Reverse geocoding failed")
        return jsonify(error="Address lookup unavailable."), 502

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    complaint = Complaint.query.filter_by(image_name=filename).first_or_404()
    if current_user.role != "admin" and complaint.user_id != current_user.id:
        abort(403)
    return send_from_directory(UPLOAD_DIR, filename, mimetype="image/jpeg", as_attachment=False)

@app.route("/complaint/<public_id>")
@login_required
def complaint_detail(public_id):
    complaint = Complaint.query.filter_by(public_id=public_id).first_or_404()
    if current_user.role != "admin" and complaint.user_id != current_user.id:
        abort(403)
    updates = AuditLog.query.filter_by(complaint_id=complaint.id).order_by(AuditLog.created_at.asc()).all()
    messages = Message.query.filter_by(complaint_id=complaint.id).order_by(Message.created_at.asc()).all()
    return render_template("detail.html", complaint=complaint, updates=updates,
                           messages=messages, message_form=MessageForm())

@app.route("/complaint/<public_id>/message", methods=["POST"])
@login_required
def post_message(public_id):
    complaint = Complaint.query.filter_by(public_id=public_id).first_or_404()
    if current_user.role != "admin" and complaint.user_id != current_user.id:
        abort(403)
    form = MessageForm()
    if form.validate_on_submit():
        db.session.add(Message(complaint_id=complaint.id, sender_id=current_user.id, body=form.body.data.strip()))
        complaint.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        flash("Message sent.", "success")
    else:
        flash("Message must be between 2 and 1000 characters.", "danger")
    return redirect(url_for("admin_edit" if current_user.role == "admin" else "complaint_detail", public_id=public_id))

@app.route("/admin")
@admin_required
def admin_dashboard():
    complaints = Complaint.query.order_by(Complaint.created_at.desc()).all()
    stats = {s: Complaint.query.filter_by(status=s).count() for s in STATUSES}
    # Latest message per complaint, so the admin can see which reports have a citizen reply waiting
    last_msg = {}
    for m in Message.query.order_by(Message.created_at.asc()).all():
        last_msg[m.complaint_id] = m
    awaiting = {cid for cid, m in last_msg.items() if m.sender.role != "admin"}
    reporters = {u.id: u.name for u in User.query.all()}
    return render_template("admin.html", complaints=complaints, stats=stats, awaiting=awaiting, reporters=reporters)

@app.route("/admin/complaint/<public_id>", methods=["GET", "POST"])
@admin_required
def admin_edit(public_id):
    complaint = Complaint.query.filter_by(public_id=public_id).first_or_404()
    form = AdminUpdateForm(status=complaint.status, priority=complaint.priority, admin_note=complaint.admin_note)
    if form.validate_on_submit():
        old_status = complaint.status
        complaint.status = form.status.data
        complaint.priority = form.priority.data
        complaint.admin_note = form.admin_note.data.strip() if form.admin_note.data else None
        complaint.updated_at = datetime.now(timezone.utc)
        db.session.add(AuditLog(actor_id=current_user.id, complaint_id=complaint.id,
                                action="Status/priority updated",
                                details=f"{old_status} → {complaint.status}; Priority: {complaint.priority}; {complaint.admin_note or ''}"))
        if form.reply.data and form.reply.data.strip():
            db.session.add(Message(complaint_id=complaint.id, sender_id=current_user.id, body=form.reply.data.strip()))
        db.session.commit()
        flash("Complaint updated.", "success")
        return redirect(url_for("admin_dashboard"))
    messages = Message.query.filter_by(complaint_id=complaint.id).order_by(Message.created_at.asc()).all()
    return render_template("admin_edit.html", complaint=complaint, form=form,
                           messages=messages, message_form=MessageForm())

def admin_total():
    return User.query.filter_by(role="admin").count()

@app.route("/admin/team", methods=["GET", "POST"])
@admin_required
def admin_team():
    create_form, promote_form = AdminCreateForm(prefix="new"), PromoteForm(prefix="pro")
    full = admin_total() >= MAX_ADMINS
    if create_form.submit.data and create_form.validate_on_submit():
        email = create_form.email.data.strip().lower()
        if full:
            flash(f"The team is full ({MAX_ADMINS} admins). Remove one first.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered. Use 'Promote existing user' instead.", "danger")
        else:
            u = User(name=create_form.name.data.strip(), email=email, role="admin")
            u.set_password(create_form.password.data)
            db.session.add(u); db.session.commit()
            flash(f"{u.name} added as admin.", "success")
            return redirect(url_for("admin_team"))
    if promote_form.submit.data and promote_form.validate_on_submit():
        u = User.query.filter_by(email=promote_form.email.data.strip().lower()).first()
        if full:
            flash(f"The team is full ({MAX_ADMINS} admins). Remove one first.", "danger")
        elif not u:
            flash("No registered user with that email.", "danger")
        elif u.role == "admin":
            flash("That user is already an admin.", "danger")
        else:
            u.role = "admin"; db.session.commit()
            flash(f"{u.name} is now an admin.", "success")
            return redirect(url_for("admin_team"))
    admins = User.query.filter_by(role="admin").order_by(User.id.asc()).all()
    return render_template("team.html", admins=admins, create_form=create_form, promote_form=promote_form,
                           max_admins=MAX_ADMINS, full=full, csrf_form=FlaskForm())

@app.route("/admin/team/<int:user_id>/remove", methods=["POST"])
@admin_required
def admin_remove(user_id):
    u = db.session.get(User, user_id) or abort(404)
    if u.id == current_user.id:
        flash("You cannot remove your own admin access.", "danger")
    elif u.role != "admin":
        flash("That user is not an admin.", "danger")
    elif admin_total() <= 1:
        flash("At least one admin must remain.", "danger")
    else:
        u.role = "citizen"; db.session.commit()  # account and history are kept
        flash(f"{u.name} is now a regular user.", "success")
    return redirect(url_for("admin_team"))

@app.errorhandler(429)
def too_many(e):
    return render_template("error.html", code=429,
        message="Too many attempts. Please wait a few minutes and try again."), 429

@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="You do not have permission to view this page."), 403

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="The requested page was not found."), 404

@app.errorhandler(413)
def too_large(e):
    return render_template("error.html", code=413, message="Upload too large. Maximum request size is 5 MB."), 413

@app.cli.command("init-db")
def init_db():
    ensure_schema()
    print("Database tables created.")

def _read_new_admin():
    import getpass
    email = input("  Email: ").strip().lower()
    name = input("  Name: ").strip()
    password = getpass.getpass("  Password (12+ characters): ")
    if len(password) < 12 or not email or not name:
        print("  Skipped: name/email required and password must be 12+ characters."); return False
    if User.query.filter_by(email=email).first():
        print("  Skipped: that email is already registered."); return False
    u = User(name=name, email=email, role="admin")
    u.set_password(password)
    db.session.add(u); db.session.commit()
    print(f"  Admin '{name}' created.")
    return True

@app.cli.command("create-admin")
def create_admin():
    """Create ONE admin (maximum of ADMIN_LIMIT, default 4)."""
    if admin_total() >= MAX_ADMINS:
        print(f"Already {MAX_ADMINS} admins. Remove one on the Team page first."); return
    _read_new_admin()

@app.cli.command("seed-admins")
def seed_admins():
    """Create all admin accounts up to the limit (4), one after another."""
    while admin_total() < MAX_ADMINS:
        print(f"Admin {admin_total() + 1} of {MAX_ADMINS}:")
        if not _read_new_admin():
            if input("  Try again? [Y/n] ").strip().lower() == "n":
                break
    print(f"Admins now: {admin_total()} of {MAX_ADMINS}.")

@app.cli.command("test-email")
def test_email():
    """Send a test email to check the SMTP settings in .env."""
    to = input("Send a test email to: ").strip()
    ok, error = send_mail(to, "CivicSense AI test email", "Email sending is working.")
    print("Sent! Check the inbox (and spam folder)." if ok else "FAILED: " + error)

@app.cli.command("reset-password")
def reset_password_cli():
    """Set a new password for any account (emergency recovery)."""
    import getpass
    u = User.query.filter_by(email=input("Account email: ").strip().lower()).first()
    if not u:
        print("No such user."); return
    pw = getpass.getpass("New password (12+ characters): ")
    if len(pw) < 12:
        print("Password must be at least 12 characters."); return
    u.set_password(pw); db.session.commit()
    print("Password updated.")

# Runs under gunicorn too, so tables always exist on first start
with app.app_context():
    ensure_schema()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
