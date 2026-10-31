"""Toll lane worker. Run separately: python toll_lane.py
USB webcam is used for both plate and driver captures. Position/camera framing must be calibrated.
"""
import os, re, time, sqlite3, cv2, socket, threading
from pathlib import Path
from PIL import Image
import pytesseract
from hardware_controller import get_controller
from biometric import compare_two_images
BASE=Path(__file__).resolve().parent; DB=BASE/'data'/'smart_toll.db'; BIO=BASE/'data'/'biometric'
CAMERA=int(os.getenv('TOLL_CAMERA_INDEX','0'))

def norm(s): return re.sub(r'[^A-Z0-9]','',(s or '').upper())
def capture(prompt, seconds=2):
    cap=cv2.VideoCapture(CAMERA)
    if not cap.isOpened(): raise RuntimeError('Toll camera unavailable. Check TOLL_CAMERA_INDEX and camera permission.')
    print(prompt); frame=None; start=time.time()
    while time.time()-start < seconds:
        ok, f=cap.read()
        if ok: frame=f
    cap.release()
    if frame is None: raise RuntimeError('Could not capture an image from toll camera.')
    return frame
def read_plate(frame):
    # OCR only: camera must be focused on a large, well-lit plate. For robust deployment use a trained plate detector.
    gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
    gray=cv2.resize(gray,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC)
    raw=pytesseract.image_to_string(gray,config='--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
    candidates=re.findall(r'[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{3,4}',norm(raw))
    return candidates[0] if candidates else None, raw
def approved_record(plate):
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row
    row=con.execute("""SELECT a.id,a.status,a.vehicle_number,b.selfie_stored_name,b.status AS bio_status
        FROM applications a JOIN biometric_verifications b ON b.application_id=a.id
        WHERE a.status='Approved' AND REPLACE(REPLACE(UPPER(a.vehicle_number),' ',''),'-','')=?
        ORDER BY a.id DESC LIMIT 1""",(plate,)).fetchone(); con.close(); return row
def handle_vehicle(board):
    # If we cannot confirm a close command, stop this cycle before any access decision.
    if not board.send('CLOSE'):
        print('FAIL-CLOSED: unable to send CLOSE to Arduino; skipping vehicle cycle')
        return
    try:
        plate_frame=capture('Point camera at the FRONT/REAR NUMBER PLATE now...')
        plate,_=read_plate(plate_frame)
        if not plate: print('REJECT: number plate OCR failed'); board.send('REJECT'); return
        row=approved_record(plate)
        if not row or row['bio_status']!='Matched': print('REJECT: vehicle not approved or customer reference biometric not verified:',plate); board.send('REJECT'); return
        print('Plate verified:',plate,'— now position driver face in camera...')
        face_frame=capture('Capture driver face...',3)
        ok,encoded=cv2.imencode('.jpg',face_frame)
        if not ok: raise RuntimeError('Face image encoding failed')
        reference=(BIO/row['selfie_stored_name']).read_bytes()
        matched,distance,threshold=compare_two_images(reference,encoded.tobytes())
        print('Face distance:',distance,'threshold:',threshold)
        if matched:
            if board.send('OPEN'):
                print('ACCESS GRANTED: OPEN command sent to Arduino')
            else:
                print('FAIL-CLOSED: Arduino did not accept OPEN command')
                board.send('REJECT')
        else:
            print('ACCESS DENIED: face mismatch'); board.send('REJECT')
    except Exception as exc:
        print('FAIL-CLOSED:',exc); board.send('REJECT')

def admin_bridge(board):
    # Localhost-only bridge lets the authenticated Flask admin panel use the same serial owner.
    server=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    server.bind(('127.0.0.1',8765)); server.listen(5)
    while True:
        conn,_=server.accept()
        with conn:
            command=conn.recv(32).decode('ascii','ignore').strip()
            ok=board.send(command) if command in {'OPEN','CLOSE'} else False
            conn.sendall(b'OK' if ok else b'ERROR')

def main():
    board=get_controller()
    if not board.connect(): raise SystemExit('Cannot connect Arduino. Set TOLL_COM_PORT, close Arduino IDE Serial Monitor, and retry.')
    threading.Thread(target=admin_bridge,args=(board,),daemon=True).start()
    print('Toll lane controller connected. Admin localhost bridge active on 127.0.0.1:8765. Waiting for VEHICLE_DETECTED...')
    busy = False
    while True:
        event=board.read_event(0.5)
        if event=='VEHICLE_DETECTED' and not busy:
            busy = True
            try:
                handle_vehicle(board)
            finally:
                busy = False
            time.sleep(1.0)
        elif event and event not in {'READY','GATE_OPEN','GATE_CLOSED','SENSOR_CLEAR'}:
            print('Arduino:',event)
        time.sleep(.05)
if __name__=='__main__': main()
