# Smart Toll Gate — real-hardware software setup

This package prepares the Flask/Python side for the hardware workflow. The Arduino UNO sketch is intentionally supplied separately when requested.

## Architecture
1. Customer portal stores approved vehicle details and the consented driver reference image in the PC/server database. The image is **not** stored in UNO memory.
2. Arduino sensor reports `VEHICLE_DETECTED` over USB serial.
3. `toll_lane.py` captures the plate with the toll-side USB camera, runs Tesseract OCR, and checks the normalized plate against an admin-approved database record.
4. Only when the plate is approved and the record has a stored reference image does Python capture the live driver face and compare it with YuNet/SFace.
5. Python sends `OPEN` only when both checks pass. Any missing camera, unreadable plate, unknown/unapproved plate, face mismatch, missing model, DB error or exception results in `REJECT`/no opening (fail closed).
6. Admin dashboard manual OPEN/CLOSE is available through a localhost bridge to the lane worker. Physical obstruction interlocks must remain in the Arduino hardware layer.

## Hardware assumptions to confirm against your actual wiring
The uploaded photograph appears to show an Arduino UNO-compatible board, a 16x2 LCD, one HC-SR04 ultrasonic sensor and a micro servo. It does not show a toll-side camera module. The camera must be a USB webcam connected to the Windows PC running Python.

Default pin plan for the later UNO sketch (confirm before wiring): HC-SR04 TRIG D7, ECHO D6; servo signal D9; I2C LCD SDA=A4, SCL=A5. LCD address commonly 0x27 (sometimes 0x3F). Python serial default is COM3; change it to the COM port shown in Windows Device Manager. Camera default is index 0.

**Electrical safety:** power the servo from a suitable regulated 5V supply if the UNO 5V rail is insufficient; connect supply GND to UNO GND. Do not drive a real barrier directly from a small hobby servo. Use a properly rated actuator/driver, limit switches, emergency stop, obstruction detection and a mechanically safe barrier. Test with no load first.

## Windows installation
Open PowerShell in the extracted `SMART-TOLL-GATE-main` folder:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python setup_biometric.py
```

Install **Tesseract OCR for Windows** separately. Confirm `tesseract.exe` is on PATH or set `TESSERACT_CMD` to its full path.

Set runtime configuration in the same PowerShell window (replace COM3 as needed):

```powershell
$env:DEMO_MODE = "0"
$env:TOLL_COM_PORT = "COM3"
$env:TOLL_CAMERA_INDEX = "0"
$env:ADMIN_USER = "admin"
$env:ADMIN_PASSWORD = "ChangeThisPasswordNow"
$env:SECRET_KEY = "replace-with-a-long-random-secret"
```

Run the web app in Terminal 1:

```powershell
python app.py
```

Run the lane worker in Terminal 2 (same project and venv):

```powershell
.\venv\Scripts\Activate.ps1
$env:DEMO_MODE = "0"
$env:TOLL_COM_PORT = "COM3"
$env:TOLL_CAMERA_INDEX = "0"
python toll_lane.py
```

Open customer portal at `http://127.0.0.1:5000` and admin portal at `http://127.0.0.1:5000/admin/login`. The first admin credentials are `ADMIN_USER` and `ADMIN_PASSWORD` at first database initialization. Change the default immediately and do not expose Flask's development server directly to the public internet.

## Customer and admin workflow
1. Customer registers and enters the vehicle number/type.
2. Upload RC, Fitness Certificate, Driving Licence and other currently required documents.
3. Capture the reference selfie with consent. Strict mode compares it against the portrait extracted from the Driving Licence; a mismatch or unavailable model blocks successful enrollment.
4. Submit application. Admin reviews documents and approves/rejects it.
5. The reference image is retained after approval because the lane worker needs it for live face comparison. Restrict access and implement an explicit retention/deletion policy.
6. At the toll, the sensor event triggers camera capture. First plate must match an `Approved` application. Then live driver face must match the stored customer reference.

## Serial protocol expected by Python
The later Arduino UNO sketch must accept newline-terminated `OPEN`, `CLOSE`, `REJECT` and `STATUS` commands at 9600 baud, and report `VEHICLE_DETECTED` when the approach sensor detects a vehicle. Useful status messages are `READY`, `GATE_OPEN`, `GATE_CLOSED` and `SENSOR_CLEAR`. Keep one process (`toll_lane.py`) as the sole owner of the COM port; close Arduino IDE Serial Monitor while it runs.

## Admin manual gate controls
Admin dashboard buttons send authenticated Flask requests. Flask forwards the command to the localhost-only bridge on `127.0.0.1:8765`; `toll_lane.py` owns the serial port. Both Flask and the lane worker must run on the same PC. Manual OPEN is an override: only use it when the lane is clear and the barrier is safe. Add an Arduino-side obstruction interlock so software commands cannot bypass physical safety.

## Bench test checklist
- Unknown plate: stays closed.
- Registered but pending/rejected vehicle: stays closed.
- Approved plate + face mismatch: stays closed.
- Approved plate + matching face: sends OPEN.
- Camera/OCR/model/database/serial failure: stays closed.
- Admin CLOSE: closes gate. Admin OPEN: only on a clear lane and with hardware safety interlock.
- Sensor detects vehicle once; repeated sensor triggers do not start overlapping verification.

## Known prototype limitations
- Tesseract currently performs OCR on the camera frame; it is not a robust production ANPR detector. Use a plate-localization model and test Tamil Nadu/Indian plate layouts, glare, night conditions, dirt and angles before any field use.
- YuNet/SFace face matching is not liveness detection. A printed photo or phone replay may spoof it. Add tested liveness/anti-spoofing before operational use.
- One ultrasonic sensor is not certified anti-crush protection. Add a second safety beam/sensor, limit switches, emergency stop, actuator feedback and manual release.
- A USB webcam cannot reliably capture plate and driver simultaneously unless camera positions and framing are engineered; this prototype asks for sequential captures. A separate plate camera and driver camera are recommended.
- This is an academic prototype, not a certified public-road toll barrier controller.

## Privacy
Obtain explicit consent before collecting face images. Restrict admin access, protect the database and uploads, avoid real identity data in public demonstrations, define retention periods, and support secure deletion when consent is withdrawn.
