"""Download the OpenCV Zoo YuNet and SFace model files once."""
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "models"
MODEL_DIR.mkdir(exist_ok=True)
MODELS = {
    "face_detection_yunet_2023mar.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}

for filename, url in MODELS.items():
    target = MODEL_DIR / filename
    if target.exists() and target.stat().st_size > 100_000:
        print(f"Already present: {filename}")
        continue
    print(f"Downloading {filename} ...")
    try:
        with urlopen(url, timeout=60) as response, target.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
        if target.stat().st_size < 100_000:
            target.unlink(missing_ok=True)
            raise RuntimeError("Downloaded model is unexpectedly small; check your internet connection.")
        print(f"Saved: {target}")
    except Exception:
        target.unlink(missing_ok=True)
        raise
print("Biometric models are ready.")
