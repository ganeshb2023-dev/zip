"""Arduino UNO serial controller. Configure TOLL_COM_PORT in environment."""
import os, threading, time
try:
    import serial
except ImportError:
    serial = None

class ArduinoController:
    def __init__(self, port=None, baudrate=9600):
        self.port = port or os.getenv("TOLL_COM_PORT", "COM3")
        self.baudrate = baudrate
        self.board = None
        self.lock = threading.Lock()
    def connect(self):
        if serial is None: return False
        if self.board and self.board.is_open: return True
        try:
            self.board = serial.Serial(self.port, self.baudrate, timeout=1, write_timeout=2)
            time.sleep(2)
            self.board.reset_input_buffer()
            return True
        except Exception as exc:
            print("Arduino connection error:", exc); self.board = None; return False
    def send(self, command):
        if command not in {"OPEN", "CLOSE", "REJECT", "STATUS"}: return False
        if not self.connect(): return False
        with self.lock:
            self.board.write((command + "\n").encode("ascii")); self.board.flush()
        return True
    def read_event(self, timeout=0.5):
        if not self.connect(): return None
        try:
            line = self.board.readline().decode("utf-8", "ignore").strip()
            return line or None
        except Exception: return None
    def close(self):
        if self.board and self.board.is_open: self.board.close()
_controller = ArduinoController()
def get_controller(): return _controller


def send_admin_command(command, host="127.0.0.1", port=8765):
    """Send admin override to the lane process that exclusively owns the serial port."""
    import socket
    if command not in {"OPEN", "CLOSE"}: return False
    try:
        with socket.create_connection((host, port), timeout=2) as sock:
            sock.sendall((command + "\n").encode("ascii"))
            return sock.recv(32).decode("ascii", "ignore").strip() == "OK"
    except OSError:
        return False
