import os, re, sqlite3, uuid, secrets, shutil, base64
from datetime import datetime, date
from functools import wraps
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    import pytesseract
    from PIL import Image
except Exception:
    pytesseract = None
    Image = None

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from biometric import detect_human_face, model_status, compare_document_to_selfie
except Exception:
    detect_human_face = None
    model_status = None

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
# Real implementation is strict by default. Demo mode must be explicitly enabled.
# Set DEMO_MODE=1 only for a non-hardware classroom demonstration.
DEMO_MODE = os.environ.get("DEMO_MODE", "0").strip().lower() in {"1", "true", "yes"}
CAPTURE_PASS_STATUSES = {"Matched"}
BIOMETRIC_DIR = DATA_DIR / "biometric"
DB_PATH = DATA_DIR / "smart_toll.db"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
BIOMETRIC_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "development-only-change-this-secret-key"
app.config.update(
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
VEHICLE_TYPES = ["Truck", "Bus", "Car", "Bike", "Auto"]
TOLL_REQUIRED_TYPES = {"Truck", "Bus", "Car"}
DOC_TYPES = {
    "rc_book": "RC Book",
    "fitness": "Fitness Certificate",
    "driving_license": "Driving Licence",
    "vehicle_insurance": "Vehicle Insurance Certificate",
}

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            user_id TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            mobile TEXT NOT NULL,
            vehicle_number TEXT NOT NULL,
            vehicle_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS applications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            vehicle_number TEXT NOT NULL,
            vehicle_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Draft',
            submitted_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            doc_type TEXT NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            extracted_text TEXT,
            extracted_expiry TEXT,
            extracted_vehicle_number TEXT,
            ai_status TEXT NOT NULL,
            ai_reason TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            UNIQUE(application_id, doc_type)
        );
        CREATE TABLE IF NOT EXISTS biometric_verifications(
            application_id INTEGER PRIMARY KEY REFERENCES applications(id) ON DELETE CASCADE,
            selfie_stored_name TEXT,
            status TEXT NOT NULL DEFAULT 'Not verified',
            distance REAL,
            threshold REAL,
            reason TEXT,
            consent_given INTEGER NOT NULL DEFAULT 0,
            captured_at TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL REFERENCES applications(id),
            admin_user TEXT NOT NULL,
            decision TEXT NOT NULL,
            remarks TEXT,
            reviewed_at TEXT NOT NULL
        );
        """)
        admin_user = os.environ.get("ADMIN_USER", "admin")
        admin_password = os.environ.get("ADMIN_PASSWORD", "Admin@12345")
        row = con.execute("SELECT id FROM users WHERE user_id=?", (admin_user,)).fetchone()
        # Admin credentials are stored separately from customer accounts.
        con.execute("""CREATE TABLE IF NOT EXISTS admins(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )""")
        if not con.execute("SELECT id FROM admins WHERE username=?", (admin_user,)).fetchone():
            con.execute("INSERT INTO admins(username,password_hash) VALUES(?,?)",
                        (admin_user, generate_password_hash(admin_password)))

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(24)
    return session["_csrf"]

@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "vehicle_types": VEHICLE_TYPES, "doc_types": DOC_TYPES, "demo_mode": DEMO_MODE}

@app.before_request
def protect_posts():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        expected = session.get("_csrf", "")
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Security token missing or expired. Refresh and try again.")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def normalize_vehicle(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def configure_tesseract():
    """Find Tesseract on Windows/Linux and configure pytesseract."""
    if not pytesseract:
        return False, "Python package pytesseract is not installed."
    configured = os.environ.get("TESSERACT_CMD")
    candidates = [
        configured,
        shutil.which("tesseract"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            return True, None
        if candidate and candidate == shutil.which("tesseract"):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return True, None
    return False, (
        "Tesseract OCR engine was not found. Install Tesseract OCR for Windows, "
        "then restart the terminal. You can also set the TESSERACT_CMD environment variable."
    )

def preprocess_for_ocr(image):
    """Improve common phone scans before OCR."""
    from PIL import ImageOps, ImageEnhance, ImageFilter
    image = ImageOps.exif_transpose(image)
    image = ImageOps.grayscale(image)
    # Upscale small scans to improve character recognition.
    if image.width < 1800:
        scale = min(2.5, 1800 / max(image.width, 1))
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    image = ImageEnhance.Contrast(image).enhance(1.8)
    image = image.filter(ImageFilter.SHARPEN)
    # Keep a grayscale copy; hard thresholding can damage faint printed text.
    return image

def run_ocr(image):
    """Run OCR with several layouts; raise a useful error if the engine fails."""
    if not pytesseract:
        raise RuntimeError("pytesseract is not installed. Run: pip install pytesseract")
    processed = preprocess_for_ocr(image)
    outputs, errors = [], []
    for psm in (6, 11, 3):
        try:
            value = pytesseract.image_to_string(
                processed, lang=os.environ.get("OCR_LANG", "eng"),
                config=f"--oem 3 --psm {psm}"
            )
            if value and value.strip():
                outputs.append(value.strip())
        except Exception as exc:
            errors.append(str(exc))
    if outputs:
        return max(outputs, key=len)
    if errors:
        raise RuntimeError("Tesseract OCR failed: " + errors[-1])
    return ""

def extract_text(path):
    """Return extracted text and a diagnostic error, not a generic blank result."""
    suffix = path.suffix.lower()
    text = ""
    if suffix == ".pdf":
        if not fitz:
            return "", "PDF reader unavailable. Install PyMuPDF: pip install PyMuPDF"
        try:
            with fitz.open(path) as pdf:
                page_texts = [(page.get_text("text") or "") for page in pdf]
                text = "\n".join(page_texts).strip()
                if len(text) < 35:
                    ok, err = configure_tesseract()
                    if not ok:
                        return text, err
                    ocr_pages, page_errors = [], []
                    for page in pdf:
                        try:
                            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
                            import io
                            with Image.open(io.BytesIO(pix.tobytes("png"))) as img:
                                page_result = run_ocr(img)
                            if page_result:
                                ocr_pages.append(page_result)
                        except Exception as exc:
                            page_errors.append(str(exc))
                    ocr_text = "\n".join(ocr_pages).strip()
                    if len(ocr_text) > len(text):
                        text = ocr_text
                    if not text and page_errors:
                        return "", "PDF OCR could not process the page: " + page_errors[-1]
        except Exception as exc:
            return "", f"Could not read this PDF: {type(exc).__name__}: {exc}"
    elif suffix in {".png", ".jpg", ".jpeg"}:
        if not Image:
            return "", "Image support unavailable. Install Pillow: pip install Pillow"
        ok, err = configure_tesseract()
        if not ok:
            return "", err
        try:
            with Image.open(path) as img:
                if img.width < 500 or img.height < 300:
                    return "", f"Image resolution is too low ({img.width}×{img.height}). Upload a clear, full-page scan or photo."
                text = run_ocr(img)
        except Exception as exc:
            return "", f"Image OCR failed: {type(exc).__name__}: {exc}"
    else:
        return "", "Unsupported file type. Upload PDF, PNG, JPG or JPEG."

    if not text.strip():
        return "", "OCR engine ran but found no readable words. Check that the complete document is visible, upright, in focus, well-lit and not blurred or covered by glare."
    # A few isolated characters are not enough to identify a government document.
    if len(re.sub(r'[^A-Za-z0-9]', '', text)) < 12:
        return text, "Only a few characters were recognized. Please upload a clearer, higher-resolution scan."
    return text[:30000], None

DOC_SIGNATURES = {
    "rc_book": ["REGISTRATION CERTIFICATE", "REGN. NO", "REG NO", "CHASSIS NO", "ENGINE NO", "TRANSPORT DEPARTMENT", "FORM-23", "FORM 23", "REGD. UPTO", "OWNER NAME"],
    "fitness": ["CERTIFICATE OF FITNESS", "FITNESS CERTIFICATE", "FITNESS VALID", "FITNESS UPTO", "FITNESS UP TO", "FITNESS TEST", "FITNESS CERTIFICATE NO"],
    "driving_license": ["DRIVING LICENCE", "DRIVING LICENSE", "DL NO", "LICENCE NO", "LICENSE NO", "ISSUING AUTHORITY", "TRANSPORT AUTHORITY", "VALID TILL", "NON-TRANSPORT"],
    "vehicle_insurance": ["CERTIFICATE OF INSURANCE", "MOTOR INSURANCE", "MOTOR POLICY", "POLICY NUMBER", "POLICY NO", "INSURANCE POLICY", "NAME OF INSURED", "INSURED VEHICLE", "POLICY PERIOD", "PERIOD OF INSURANCE", "VALID FROM", "VALID TO", "THIRD PARTY", "COMPREHENSIVE", "INSURED DECLARED VALUE", "IRDAI"],
}

def detect_document_type(text):
    """Conservative keyword-based document classifier; weak matches are not trusted."""
    upper = re.sub(r'\s+', ' ', (text or '').upper())
    scores = {}
    for key, phrases in DOC_SIGNATURES.items():
        scores[key] = sum(1 for phrase in phrases if phrase in upper)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] < 2:
        return None, scores
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, scores
    return ranked[0][0], scores

def parse_date(value):
    value = value.strip().replace('-', '/').replace('.', '/')
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None

def find_relevant_expiry(doc_type, text):
    """Find dates only near the appropriate validity label; avoid DOB/issue dates."""
    upper = (text or '').upper()
    if doc_type == 'rc_book':
        labels = r'(?:REGD\.?\s*UPTO|REGISTRATION\s+(?:VALID\s+)?UPTO|REGISTRATION\s+VALID\s+TILL)'
    elif doc_type == 'fitness':
        labels = r'(?:FITNESS\s+(?:CERTIFICATE\s+)?(?:VALID\s*)?(?:UPTO|UP\s*TO|TILL)|VALID\s+UPTO)'
    elif doc_type == 'driving_license':
        labels = r'(?:VALID\s+(?:TILL|UPTO|UP\s*TO)|NT\s+VALIDITY|TR\s+VALIDITY|NON.?TRANSPORT\s+VALIDITY)'
    elif doc_type == 'vehicle_insurance':
        date_rx = r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|20\d{2}[/-]\d{1,2}[/-]\d{1,2})'
        # Insurance periods commonly contain both start and end dates. Prefer the
        # second date from an explicitly labelled period, not the policy start date.
        period_labels = r'(?:POLICY\s+PERIOD|PERIOD\s+OF\s+INSURANCE|PERIOD\s+OF\s+POLICY|VALIDITY\s+PERIOD)'
        for match in re.finditer(period_labels + r'[^\n\r]{0,100}', upper):
            dates = re.findall(date_rx, match.group(0))
            if len(dates) >= 2:
                parsed = parse_date(dates[-1])
                if parsed:
                    return parsed.isoformat()
        for match in re.finditer(r'VALID\s+FROM[^\n\r]{0,100}?\bTO\b[^\n\r]{0,20}?' + date_rx, upper):
            parsed = parse_date(match.group(1))
            if parsed:
                return parsed.isoformat()
        labels = r'(?:VALID\s+(?:TILL|UPTO|UP\s*TO)|EXPIRY\s+DATE|DATE\s+OF\s+EXPIRY|VALID\s+TO)'
    else:
        return None
    date_rx = r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|20\d{2}[/-]\d{1,2}[/-]\d{1,2})'
    for match in re.finditer(labels + r'[^\n\r]{0,45}?' + date_rx, upper):
        parsed = parse_date(match.group(1))
        if parsed:
            return parsed.isoformat()
    return None

def extract_vehicle_number(text):
    upper = (text or '').upper()
    matches = re.findall(r'(?<![A-Z0-9])[A-Z]{2}\s*[-]?\s*\d{1,2}\s*[-]?\s*[A-Z]{1,3}\s*[-]?\s*\d{1,4}(?![A-Z0-9])', upper)
    return normalize_vehicle(matches[0]) if matches else None

def validate_document(doc_type, text, vehicle_number, extraction_error=None):
    if extraction_error:
        return "Needs review", extraction_error, None, None
    if not text.strip():
        return "Needs review", "No text was extracted. Check the scan quality and OCR installation.", None, None

    detected, scores = detect_document_type(text)
    if detected and detected != doc_type:
        return "Invalid", f"Wrong document uploaded. AI text check indicates this is a {DOC_TYPES[detected]}, not a {DOC_TYPES[doc_type]}. Upload the correct document.", None, extract_vehicle_number(text)
    if not detected:
        return "Needs review", "Text was extracted, but the document type could not be identified confidently. Ensure the title and issuing-authority section are visible; administrator review is required.", None, extract_vehicle_number(text)

    expiry = find_relevant_expiry(doc_type, text)
    extracted_vehicle = extract_vehicle_number(text)
    if doc_type == 'rc_book':
        if not extracted_vehicle:
            return "Needs review", "RC-like text detected, but the vehicle registration number could not be read. Check the REG. NO field and ask an administrator to verify it.", expiry, None
        if normalize_vehicle(vehicle_number) != extracted_vehicle:
            return "Invalid", f"Vehicle number mismatch: customer profile has {normalize_vehicle(vehicle_number)}, but the RC shows {extracted_vehicle}.", expiry, extracted_vehicle
        if not expiry:
            return "Needs review", f"RC document type and vehicle number matched ({extracted_vehicle}), but the REGD. UPTO date was not confidently found. Admin review required.", None, extracted_vehicle
        exp = date.fromisoformat(expiry)
        if exp < date.today():
            return "Invalid", f"RC registration validity appears expired on {exp.strftime('%d/%m/%Y')}.", expiry, extracted_vehicle
        return "Passed checks", f"RC text type detected; vehicle number matches ({extracted_vehicle}); registration validity date {exp.strftime('%d/%m/%Y')} is not past. Issuer authenticity is not verified.", expiry, extracted_vehicle

    if doc_type in {'fitness', 'driving_license'}:
        if not expiry:
            field = 'fitness validity' if doc_type == 'fitness' else 'licence validity'
            return "Needs review", f"Correct document type keywords detected, but {field} date was not confidently located. Do not assume it is valid; administrator review required.", None, extracted_vehicle
        exp = date.fromisoformat(expiry)
        if exp < date.today():
            return "Invalid", f"Document appears expired on {exp.strftime('%d/%m/%Y')}.", expiry, extracted_vehicle
        extra = " Licence class and holder details still need verification." if doc_type == 'driving_license' else " Vehicle applicability and issuing authority still need verification."
        return "Passed checks", f"Expiry date {exp.strftime('%d/%m/%Y')} is not in the past.{extra} Issuer authenticity is not verified.", expiry, extracted_vehicle

    if doc_type == 'vehicle_insurance':
        if not extracted_vehicle:
            return "Needs review", "Insurance-like document detected, but its vehicle registration number could not be read. Administrator must verify that the policy belongs to this vehicle.", expiry, None
        if normalize_vehicle(vehicle_number) != extracted_vehicle:
            return "Invalid", f"Vehicle number mismatch: customer profile has {normalize_vehicle(vehicle_number)}, but the insurance document shows {extracted_vehicle}.", expiry, extracted_vehicle
        if not expiry:
            return "Needs review", f"Insurance document type and vehicle number matched ({extracted_vehicle}), but policy expiry could not be confidently read. Administrator must verify the policy period and insurer.", None, extracted_vehicle
        exp = date.fromisoformat(expiry)
        if exp < date.today():
            return "Invalid", f"Vehicle insurance policy appears expired on {exp.strftime('%d/%m/%Y')}.", expiry, extracted_vehicle
        return "Passed checks", f"Insurance document keywords detected; vehicle number matches ({extracted_vehicle}); OCR-read policy end date {exp.strftime('%d/%m/%Y')} is not past. This does not authenticate the policy or confirm insurer records; administrator verification is mandatory.", expiry, extracted_vehicle

    return "Needs review", "Automated screening completed; administrator verification is required.", expiry, extracted_vehicle

def current_customer():
    return session.get("customer_id")

def customer_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_customer():
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_user"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

def get_or_create_application(user):
    with db() as con:
        row = con.execute("""SELECT * FROM applications WHERE user_id=? AND status IN ('Draft','Needs correction')
                             ORDER BY id DESC LIMIT 1""", (user["id"],)).fetchone()
        if row:
            return row
        cur = con.execute("""INSERT INTO applications(user_id,vehicle_number,vehicle_type,status,created_at)
                             VALUES(?,?,?,'Draft',?)""",
                          (user["id"], user["vehicle_number"], user["vehicle_type"], datetime.now().isoformat(timespec="seconds")))
        return con.execute("SELECT * FROM applications WHERE id=?", (cur.lastrowid,)).fetchone()

@app.route("/")
def index():
    if session.get("customer_id"):
        return redirect(url_for("customer_dashboard"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        user_id = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")
        mobile = re.sub(r"\D", "", request.form.get("mobile", ""))
        vehicle = normalize_vehicle(request.form.get("vehicle_number", ""))
        vtype = request.form.get("vehicle_type", "")
        if not all([full_name, user_id, password, mobile, vehicle, vtype]):
            flash("Complete all required fields.", "danger")
        elif len(password) < 10:
            flash("Password must contain at least 10 characters.", "danger")
        elif not (10 <= len(mobile) <= 15):
            flash("Enter a valid mobile number.", "danger")
        elif vtype not in VEHICLE_TYPES:
            flash("Select a valid vehicle type.", "danger")
        else:
            try:
                with db() as con:
                    con.execute("""INSERT INTO users(full_name,user_id,password_hash,mobile,vehicle_number,vehicle_type,created_at)
                                   VALUES(?,?,?,?,?,?,?)""",
                                (full_name, user_id, generate_password_hash(password), mobile, vehicle, vtype,
                                 datetime.now().isoformat(timespec="seconds")))
                flash("Registration successful. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That User ID is already registered. Choose another.", "danger")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user_id = request.form.get("user_id", "").strip()
        password = request.form.get("password", "")
        with db() as con:
            user = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["customer_id"] = user["id"]
            session["_csrf"] = secrets.token_urlsafe(24)
            return redirect(url_for("customer_dashboard"))
        flash("Incorrect User ID or password.", "danger")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))

@app.route("/customer")
@customer_required
def customer_dashboard():
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE id=?", (current_customer(),)).fetchone()
        approw = get_or_create_application(user)
        docs = con.execute("SELECT * FROM documents WHERE application_id=?", (approw["id"],)).fetchall()
    docmap = {d["doc_type"]: d for d in docs}
    exempt = user["vehicle_type"] not in TOLL_REQUIRED_TYPES
    with db() as con:
        biometric = con.execute("SELECT * FROM biometric_verifications WHERE application_id=?", (approw["id"],)).fetchone()
    model_ready, model_error = model_status() if model_status else (False, "Biometric module is unavailable. Reinstall project requirements.")
    return render_template("customer.html", user=user, application=approw, docmap=docmap, exempt=exempt,
                           biometric=biometric, biometric_model_ready=model_ready, biometric_model_error=model_error)

@app.route("/upload/<doc_type>", methods=["POST"])
@customer_required
def upload_document(doc_type):
    if doc_type not in DOC_TYPES:
        abort(404)
    file = request.files.get("document")
    if not file or not file.filename:
        flash("Choose a file first.", "danger")
        return redirect(url_for("customer_dashboard"))
    if not allowed_file(file.filename):
        flash("Only PDF, PNG, JPG and JPEG files are accepted.", "danger")
        return redirect(url_for("customer_dashboard"))
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE id=?", (current_customer(),)).fetchone()
        if user["vehicle_type"] not in TOLL_REQUIRED_TYPES:
            flash("This vehicle category is exempt from the document workflow in this prototype.", "info")
            return redirect(url_for("customer_dashboard"))
        approw = get_or_create_application(user)
        safe_original = secure_filename(file.filename) or "document"
        stored = f"{uuid.uuid4().hex}_{safe_original}"
        path = UPLOAD_DIR / stored
        file.save(path)
        # Replacing the Driving Licence invalidates any previous face match.
        if doc_type == "driving_license":
            previous_bio = con.execute("SELECT selfie_stored_name FROM biometric_verifications WHERE application_id=?", (approw["id"],)).fetchone()
            con.execute("DELETE FROM biometric_verifications WHERE application_id=?", (approw["id"],))
            if previous_bio and previous_bio["selfie_stored_name"]:
                (BIOMETRIC_DIR / previous_bio["selfie_stored_name"]).unlink(missing_ok=True)
        extracted, extraction_error = extract_text(path)
        status, reason, expiry, extracted_vehicle = validate_document(
            doc_type, extracted, user["vehicle_number"], extraction_error
        )
        con.execute("""INSERT INTO documents(application_id,doc_type,original_name,stored_name,extracted_text,
                       extracted_expiry,extracted_vehicle_number,ai_status,ai_reason,uploaded_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(application_id,doc_type) DO UPDATE SET
                       original_name=excluded.original_name,stored_name=excluded.stored_name,
                       extracted_text=excluded.extracted_text,extracted_expiry=excluded.extracted_expiry,
                       extracted_vehicle_number=excluded.extracted_vehicle_number,ai_status=excluded.ai_status,
                       ai_reason=excluded.ai_reason,uploaded_at=excluded.uploaded_at""",
                    (approw["id"], doc_type, safe_original, stored, extracted, expiry, extracted_vehicle,
                     status, reason, datetime.now().isoformat(timespec="seconds")))
        con.execute("UPDATE applications SET status='Draft' WHERE id=?", (approw["id"],))
    flash(f"{DOC_TYPES[doc_type]} uploaded. Automated result: {status}.", "success" if status == "Passed checks" else "warning")
    return redirect(url_for("customer_dashboard"))

@app.route("/file/<int:doc_id>")
@customer_required
def customer_file(doc_id):
    with db() as con:
        row = con.execute("""SELECT d.* FROM documents d JOIN applications a ON d.application_id=a.id
                             WHERE d.id=? AND a.user_id=?""", (doc_id, current_customer())).fetchone()
    if not row:
        abort(404)
    return send_from_directory(UPLOAD_DIR, row["stored_name"], as_attachment=False)

@app.route("/biometric/verify", methods=["POST"])
@customer_required
def biometric_verify():
    """Enroll a consented reference photo; strict mode requires a real DL portrait match."""
    image_data = request.form.get("image_data", "")
    if request.form.get("consent") != "yes":
        return {"ok": False, "status": "Consent required", "message": "Please tick the consent box before capturing."}, 400
    if not image_data.startswith("data:image/") or "," not in image_data:
        return {"ok": False, "status": "No image", "message": "No webcam image received. Please capture again."}, 400
    try:
        header, encoded = image_data.split(",", 1)
        if len(encoded) > 7_000_000:
            return {"ok": False, "status": "Too large", "message": "Captured image is too large. Please capture again."}, 413
        selfie_bytes = base64.b64decode(encoded, validate=True)
    except Exception:
        return {"ok": False, "status": "Invalid image", "message": "Captured image data is invalid. Please capture again."}, 400

    # Enrollment photo must match the portrait in the uploaded Driving Licence.
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE id=?", (current_customer(),)).fetchone()
        approw = get_or_create_application(user)
        license_row = con.execute("SELECT stored_name FROM documents WHERE application_id=? AND doc_type='driving_license'", (approw["id"],)).fetchone()
    if not license_row:
        return {"ok": False, "status": "Driving licence required", "message": "Upload the Driving Licence before capturing the reference photo."}, 400
    license_path = UPLOAD_DIR / license_row["stored_name"]
    try:
        matched, distance, threshold = compare_document_to_selfie(str(license_path), selfie_bytes)
    except Exception as exc:
        return {"ok": False, "status": "Verification unavailable", "message": str(exc)}, 400
    with db() as con:
        previous_bio = con.execute("SELECT selfie_stored_name FROM biometric_verifications WHERE application_id=?", (approw["id"],)).fetchone()
        if previous_bio and previous_bio["selfie_stored_name"]:
            (BIOMETRIC_DIR / previous_bio["selfie_stored_name"]).unlink(missing_ok=True)
        selfie_name = f"selfie_{approw['id']}_{uuid.uuid4().hex}.jpg"
        (BIOMETRIC_DIR / selfie_name).write_bytes(selfie_bytes)
        status = "Matched" if matched else "Mismatch"
        reason = f"Reference photo {status.lower()} against Driving Licence portrait. SFace L2 distance={distance}; threshold={threshold}."
        con.execute("""INSERT INTO biometric_verifications(application_id,selfie_stored_name,status,distance,threshold,reason,consent_given,captured_at)
                       VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(application_id) DO UPDATE SET
                       selfie_stored_name=excluded.selfie_stored_name,status=excluded.status,distance=excluded.distance,
                       threshold=excluded.threshold,reason=excluded.reason,consent_given=excluded.consent_given,
                       captured_at=excluded.captured_at""",
                    (approw["id"], selfie_name, status, distance, threshold, reason, 1, datetime.now().isoformat(timespec="seconds")))
    if not matched:
        return {"ok": False, "status": "Mismatch", "message": reason}
    return {"ok": True, "status": "Matched", "message": reason}

@app.route("/review")
@customer_required
def review():
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE id=?", (current_customer(),)).fetchone()
        approw = get_or_create_application(user)
        docs = con.execute("SELECT * FROM documents WHERE application_id=? ORDER BY doc_type", (approw["id"],)).fetchall()
        biometric = con.execute("SELECT * FROM biometric_verifications WHERE application_id=?", (approw["id"],)).fetchone()
        uploaded_keys = {d["doc_type"] for d in docs}
        required_keys = set(DOC_TYPES)
        required_docs = [d for d in docs if d["doc_type"] in required_keys]
        all_required_uploaded = required_keys.issubset(uploaded_keys)
        has_invalid_required = any(d["ai_status"] == "Invalid" for d in required_docs)
    return render_template("review.html", user=user, application=approw, docs=docs,
                           required=list(DOC_TYPES.items()), biometric=biometric,
                           all_required_uploaded=all_required_uploaded,
                           has_invalid_required=has_invalid_required)

@app.route("/submit-application", methods=["POST"])
@customer_required
def submit_application():
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE id=?", (current_customer(),)).fetchone()
        approw = get_or_create_application(user)
        if user["vehicle_type"] not in TOLL_REQUIRED_TYPES:
            flash("This vehicle category is marked exempt in this prototype.", "info")
            return redirect(url_for("customer_dashboard"))
        docs = con.execute("SELECT * FROM documents WHERE application_id=?", (approw["id"],)).fetchall()
        uploaded = {d["doc_type"]: d for d in docs}
        missing = [label for key, label in DOC_TYPES.items() if key not in uploaded]
        if missing:
            flash("Upload all required documents before submitting: " + ", ".join(missing), "danger")
            return redirect(url_for("review"))
        invalid = [DOC_TYPES[d["doc_type"]] for d in docs if d["doc_type"] in DOC_TYPES and d["ai_status"] == "Invalid"]
        if invalid:
            flash("Replace documents marked Invalid before submitting: " + ", ".join(invalid), "danger")
            return redirect(url_for("review"))
        biometric = con.execute("SELECT * FROM biometric_verifications WHERE application_id=?", (approw["id"],)).fetchone()
        captured_ok = biometric and biometric["selfie_stored_name"] and biometric["status"] in CAPTURE_PASS_STATUSES
        if not captured_ok:
            flash("Capture your webcam photo before submitting the application.", "danger")
            return redirect(url_for("review"))
        con.execute("UPDATE applications SET status='Submitted', submitted_at=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), approw["id"]))
    flash("Application submitted to the administrator for review.", "success")
    return redirect(url_for("customer_dashboard"))

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as con:
            admin = con.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        if admin and check_password_hash(admin["password_hash"], password):
            session.clear()
            session["admin_user"] = username
            session["_csrf"] = secrets.token_urlsafe(24)
            return redirect(url_for("admin_dashboard"))
        flash("Invalid administrator credentials.", "danger")
    return render_template("admin_login.html")

@app.route("/admin")
@admin_required
def admin_dashboard():
    q = normalize_vehicle(request.args.get("vehicle", ""))
    status_filter = request.args.get("status", "")
    sql = """SELECT a.*,u.full_name,u.mobile,u.user_id FROM applications a
             JOIN users u ON a.user_id=u.id WHERE 1=1"""
    args = []
    if q:
        sql += " AND REPLACE(REPLACE(UPPER(a.vehicle_number),' ',''),'-','') LIKE ?"
        args.append(f"%{q}%")
    if status_filter in {"Draft", "Submitted", "Approved", "Rejected"}:
        sql += " AND a.status=?"
        args.append(status_filter)
    sql += " ORDER BY a.created_at DESC"
    with db() as con:
        apps = con.execute(sql, args).fetchall()
        stats = {
            "total": con.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
            "pending": con.execute("SELECT COUNT(*) FROM applications WHERE status='Submitted'").fetchone()[0],
            "approved": con.execute("SELECT COUNT(*) FROM applications WHERE status='Approved'").fetchone()[0],
            "rejected": con.execute("SELECT COUNT(*) FROM applications WHERE status='Rejected'").fetchone()[0],
        }
    return render_template("admin.html", apps=apps, stats=stats, q=q, status_filter=status_filter)

@app.route("/admin/application/<int:app_id>")
@admin_required
def admin_application(app_id):
    with db() as con:
        approw = con.execute("""SELECT a.*,u.full_name,u.mobile,u.user_id FROM applications a
                                JOIN users u ON a.user_id=u.id WHERE a.id=?""", (app_id,)).fetchone()
        if not approw:
            abort(404)
        docs = con.execute("SELECT * FROM documents WHERE application_id=? ORDER BY doc_type", (app_id,)).fetchall()
        reviews = con.execute("SELECT * FROM admin_reviews WHERE application_id=? ORDER BY reviewed_at DESC", (app_id,)).fetchall()
        biometric = con.execute("SELECT * FROM biometric_verifications WHERE application_id=?", (app_id,)).fetchone()
    return render_template("admin_application.html", application=approw, docs=docs, reviews=reviews, biometric=biometric)

@app.route("/admin/document/<int:doc_id>")
@admin_required
def admin_document(doc_id):
    with db() as con:
        row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not row:
        abort(404)
    return send_from_directory(UPLOAD_DIR, row["stored_name"], as_attachment=False)

@app.route("/admin/biometric/<int:app_id>")
@admin_required
def admin_biometric_image(app_id):
    with db() as con:
        row = con.execute("SELECT selfie_stored_name FROM biometric_verifications WHERE application_id=?", (app_id,)).fetchone()
    if not row or not row["selfie_stored_name"]:
        abort(404)
    return send_from_directory(BIOMETRIC_DIR, row["selfie_stored_name"], as_attachment=False)

@app.route("/admin/decision/<int:app_id>", methods=["POST"])
@admin_required
def admin_decision(app_id):
    decision = request.form.get("decision")
    remarks = request.form.get("remarks", "").strip()
    if decision not in {"Approved", "Rejected"}:
        abort(400)
    with db() as con:
        approw = con.execute("SELECT id FROM applications WHERE id=?", (app_id,)).fetchone()
        if not approw:
            abort(404)
        if decision == "Approved" and not DEMO_MODE:
            biometric = con.execute("SELECT status FROM biometric_verifications WHERE application_id=?", (app_id,)).fetchone()
            if not biometric or biometric["status"] != "Matched":
                flash("Approval blocked: webcam face verification has not passed.", "danger")
                return redirect(url_for("admin_application", app_id=app_id))
        con.execute("UPDATE applications SET status=? WHERE id=?", (decision, app_id))
        # Keep the consented reference image available for toll-lane face comparison.
        # Apply retention/deletion policy explicitly; do not delete it on approval.
        con.execute("""INSERT INTO admin_reviews(application_id,admin_user,decision,remarks,reviewed_at)
                       VALUES(?,?,?,?,?)""",
                    (app_id, session["admin_user"], decision, remarks, datetime.now().isoformat(timespec="seconds")))
    flash(f"Application marked {decision.lower()}.", "success")
    return redirect(url_for("admin_application", app_id=app_id))


@app.route("/admin/gate/<command>", methods=["POST"])
@admin_required
def admin_gate_control(command):
    if command not in {"OPEN", "CLOSE"}:
        abort(400)
    try:
        from hardware_controller import send_admin_command
        if not send_admin_command(command):
            flash("Toll lane service is not running or Arduino is disconnected. Start python toll_lane.py and check COM port.", "danger")
        else:
            flash(f"Manual gate {command.lower()} command sent to Arduino.", "success")
    except Exception as exc:
        flash(f"Hardware error: {exc}", "danger")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.errorhandler(413)
def too_large(_):
    return "File too large. Maximum upload size is 12 MB.", 413

if __name__ == "__main__":
    init_db()
    app.run(host=os.environ.get("FLASK_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)), debug=False)
