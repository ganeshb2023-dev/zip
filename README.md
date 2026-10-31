# AI-Based Smart Toll Gate Management System (Software Prototype)

A Flask-based prototype containing a customer portal, document upload/OCR checks, a shared SQLite database, and an administrator portal.

## 1. Requirements
- Windows 10/11
- Python 3.10 or newer
- Internet only for installing Python packages
- Optional: Tesseract OCR installed locally for image OCR. PDF text extraction works through PyMuPDF; scanned PDFs also need Tesseract.

## 2. Setup on Windows
Open Command Prompt in this project folder:

```bat
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Optional OCR installation:
1. Install Tesseract OCR for Windows.
2. Add its installation folder to PATH, commonly `C:\Program Files\Tesseract-OCR`.
3. Restart Command Prompt.

## 3. Run
```bat
python app.py
```
Open:
- Customer portal: http://127.0.0.1:5000
- Admin portal: http://127.0.0.1:5000/admin/login

Demo administrator:
- Username: `admin`
- Password: `Admin@12345`

Change the demo password before any real deployment. You can set environment variables before running:
```bat
set ADMIN_USER=youradmin
set ADMIN_PASSWORD=use-a-long-unique-password
set SECRET_KEY=use-a-long-random-secret
python app.py
```

## 4. Customer workflow
1. Register with name, User ID, password, mobile, vehicle number and vehicle type.
2. Log in using User ID and password.
3. Upload RC Book, Fitness Certificate, Driving Licence and Vehicle Insurance Certificate (PDF/JPG/PNG).
4. Review extracted text and automated validation result.
5. Submit the application.
6. The application appears in the admin portal.

Bike and Auto are marked exempt from the four-document workflow in this prototype, as discussed. Truck, Bus and Car use the document workflow.

## 5. What the automated checks do
- Extract selectable PDF text using PyMuPDF.
- Use Tesseract OCR for image files and scanned PDFs if Tesseract is installed. The updated OCR pipeline preprocesses images and tries multiple page-segmentation modes.
- Look for common numeric date formats and flag past dates for Fitness and Driving Licence.
- Attempt to extract an Indian vehicle registration number from the RC and compare it with the registered vehicle number.
- Vehicle Insurance OCR checks document keywords, vehicle registration number and the policy end date where readable; an administrator must still verify the insurer and policy authenticity.

**Important:** OCR and rule-based checks do not establish that a government document is genuine. The result is a preliminary screening result, not legal/issuer verification. An authorized issuer API or official verification process is needed for authenticity. AI results should be reviewed by an administrator before a final decision.

## 6. Data and security notes
- Customer passwords are hashed.
- Admin accounts are stored separately.
- Uploaded files are stored under `data/uploads` with randomized server filenames.
- The app uses a session CSRF token for POST forms.
- This is a local academic prototype. Before deployment, add HTTPS, secure cookie settings, rate limiting, audit logging, malware scanning, encrypted backups, role management, retention/deletion policies and production-grade database/storage.
- Do not upload real identity documents during a public demo. Use synthetic/sample documents.

## 7. Patent and examiner demonstration
This is an implementation baseline, not a patentability opinion. A working prototype alone does not prove novelty or inventive step. Keep dated design notes, architecture, experiments, baseline comparisons and evidence of technical effect. Conduct a proper prior-art search before making novelty claims.


## AI-assisted document screening (updated)

When a customer clicks **Upload & AI Verify**, the server automatically:

1. Extracts selectable PDF text or runs OCR on scanned PDFs/images.
2. Reports OCR setup failures separately from unreadable/low-resolution scans.
3. Checks whether the uploaded file resembles the requested category (RC Book, Fitness Certificate, Driving Licence, or Vehicle Insurance Certificate). For example, an RC Book uploaded in the Fitness Certificate slot is flagged as the wrong document type.
4. Extracts the RC registration number and compares it with the customer's registered vehicle number.
5. Looks for validity dates only near the relevant labels (RC `REGD. UPTO`, Fitness validity, Driving Licence validity, or the Vehicle Insurance policy period / expiry label), then checks whether that date has passed.
6. Sends uncertain cases to administrator review instead of assuming that the document is valid. Documents marked `Invalid` must be replaced before submission.

**Important limitation:** this is an OCR + keyword/rule-based screening prototype (AI-assisted automation), not an issuer-authentication system. It cannot prove that a certificate is genuine, detect every forgery, or verify official government records. Those capabilities require integration with authorized government/issuer verification services. Income-certificate validity varies by purpose and issuing authority, so no expiry is invented when the certificate does not explicitly state one.

For scanned documents, install the separate Tesseract OCR application in addition to the Python `pytesseract` package. The app will display the actual OCR setup/processing reason when that dependency is missing or fails.


## Demo Mode - Capture-Based Authentication (current)

This is *not* actual biometric identity verification. The webcam step only checks that a **human face is present**; there is no match/mismatch comparison with the licence and nothing like that is shown.

| Condition | Behaviour |
|---|---|
| Webcam photo with a human face | Accepted and stored ("Captured") |
| Face not detected | Asks the customer to capture again |
| Webcam permission denied | Asks the customer to enable the camera |
| Admin review | Admin approves or rejects manually; the photo stays viewable |

Workflow: upload RC Book, Fitness Certificate, Driving Licence and Vehicle Insurance Certificate -> capture webcam photo -> photo stored -> final submission -> admin decides. If the face models are not installed (`python setup_biometric.py`), photos are accepted without the face check.

---

## Webcam face authentication (strict real mode; default)

The customer dashboard now shows **Webcam Face Authentication** below the four certificate upload cards. The customer must upload a Driving Licence, turn on the browser camera, capture a face image, and submit it for comparison against the portrait detected in the Driving Licence.

### One-time biometric setup on Windows

Run these commands from the project folder after activating your virtual environment:

```bat
pip install -r requirements.txt
python setup_biometric.py
```

`setup_biometric.py` downloads the OpenCV Zoo YuNet face detector and SFace face-recognition ONNX models into the `models` folder. Internet access is required for this one-time download. Then start/restart the application:

```bat
python app.py
```

Open the customer portal at `http://127.0.0.1:5000`. Browser webcam access works on localhost or HTTPS and requires the user to grant camera permission. Upload the Driving Licence first; then turn on the camera and click **Capture & Verify**. The result appears on the dashboard and final review page. The admin application page displays the result, comparison distance and a protected link to the captured image.

### Approval rules and limitations

- The customer cannot submit the application unless the face result is `Matched`.
- The admin cannot approve an application unless a successful face match exists.
- Replacing the Driving Licence clears the previous face-match result, so the customer must verify again.
- The comparison uses OpenCV YuNet for face detection and SFace embeddings for similarity. The configured L2 distance threshold is 1.128 (OpenCV example threshold); test and calibrate it using consented representative sample images before any operational use. A false match or false mismatch is possible.
- **This implementation does not include robust liveness / anti-spoof detection.** A printed photo or screen replay may fool a simple webcam capture. Therefore this is an academic face-comparison prototype, not secure biometric identity proof or a replacement for human/official verification.
- Obtain explicit informed consent before capturing biometric data. Restrict access, use HTTPS outside localhost, define a short retention period, and delete webcam images/biometric records when no longer needed. Do not use real identity images in public demonstrations; use consented test samples.


## Real hardware integration
See `REAL_HARDWARE_SETUP.md` for Arduino UNO wiring assumptions, the uploaded sketch, Python serial controller, toll lane camera flow, and admin manual gate controls. Hardware is fail-closed by default; demo biometric mode is disabled by default.


## Real hardware integration update
See `REAL_HARDWARE_SETUP.md` for the Windows setup, USB serial workflow, customer reference-photo handling, admin gate control, hardware assumptions, and fail-closed test checklist. Real mode is the default (`DEMO_MODE=0`). The Arduino UNO sketch will be provided separately when requested.
#   z i p  
 