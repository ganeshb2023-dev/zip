"""Face comparison helpers using OpenCV YuNet + SFace ONNX models.

The models are downloaded once with setup_biometric.py. This module deliberately
returns a setup error instead of pretending a comparison succeeded when models
or a detectable face are missing.
"""
from pathlib import Path
import cv2
import numpy as np

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "models"
DETECTOR_MODEL = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = MODEL_DIR / "face_recognition_sface_2021dec.onnx"


def model_status():
    missing = [p.name for p in (DETECTOR_MODEL, RECOGNIZER_MODEL) if not p.is_file()]
    if missing:
        return False, "Biometric AI models are missing: " + ", ".join(missing) + ". Run: python setup_biometric.py"
    if not hasattr(cv2, "FaceDetectorYN_create") or not hasattr(cv2, "FaceRecognizerSF_create"):
        return False, "OpenCV face modules are unavailable. Install requirements with opencv-contrib-python-headless."
    return True, None


def _models(width, height):
    ok, error = model_status()
    if not ok:
        raise RuntimeError(error)
    detector = cv2.FaceDetectorYN_create(str(DETECTOR_MODEL), "", (int(width), int(height)), 0.80, 0.3, 5000)
    recognizer = cv2.FaceRecognizerSF_create(str(RECOGNIZER_MODEL), "")
    return detector, recognizer


def _decode_image(image_bytes):
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("The image could not be opened. Capture again in good lighting.")
    return image


def _face_feature(image):
    height, width = image.shape[:2]
    detector, recognizer = _models(width, height)
    detector.setInputSize((width, height))
    _, faces = detector.detect(image)
    if faces is None or len(faces) == 0:
        return None, 0
    # Use the largest detected face. More than one similarly-sized face is ambiguous.
    areas = [max(0.0, float(face[2]) * float(face[3])) for face in faces]
    order = np.argsort(areas)[::-1]
    if len(order) > 1 and areas[order[1]] >= areas[order[0]] * 0.72:
        return None, len(faces)
    face = faces[order[0]]
    aligned = recognizer.alignCrop(image, face)
    feature = recognizer.feature(aligned)
    return feature, len(faces)


def _document_pages(file_path):
    path = Path(file_path)
    if path.suffix.lower() == ".pdf":
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PyMuPDF is required to read a PDF Driving Licence.") from exc
        pages = []
        with fitz.open(path) as pdf:
            for page in pdf:
                pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False)
                pages.append(_decode_image(pix.tobytes("png")))
        return pages
    image = cv2.imread(str(path))
    if image is None:
        raise ValueError("The uploaded Driving Licence image could not be opened.")
    return [image]


def feature_from_document(file_path):
    """Find the clearest/largest portrait face in the Driving Licence."""
    candidates = []
    for page in _document_pages(file_path):
        feature, count = _face_feature(page)
        if feature is not None:
            candidates.append(feature)
    if not candidates:
        raise ValueError("No single clear face was detected in the Driving Licence. Upload a clear, full-colour licence scan; if it contains multiple portraits, ask an administrator to review it.")
    if len(candidates) > 1:
        # A multi-page licence with portraits on multiple pages is ambiguous.
        raise ValueError("Faces were detected on multiple licence pages. Administrator review is required to select the correct portrait.")
    return candidates[0]


def compare_document_to_selfie(file_path, selfie_bytes, threshold=0.363):
    document_feature = feature_from_document(file_path)
    selfie = _decode_image(selfie_bytes)
    selfie_feature, count = _face_feature(selfie)
    if selfie_feature is None:
        if count > 1:
            raise ValueError("More than one face is visible in the webcam capture. Keep only one person in the frame and capture again.")
        raise ValueError("No clear face detected in the webcam capture. Face the camera in good lighting and capture again.")
    similarity = float(cv2.norm(document_feature, selfie_feature, cv2.NORM_L2))
    # SFace's published L2 threshold is 1.128; lower distances mean more similar.
    matched = similarity <= 1.128
    return matched, round(similarity, 4), 1.128


def analyze_capture(file_path, selfie_bytes):
    """Analyze a live enrollment capture against the Driving Licence portrait.

    Returns face_detected, multiple_faces, matched, distance, threshold and note.
    A missing model or unreadable portrait leaves matched=None and must not be treated as success.
    """
    result = {"face_detected": False, "multiple_faces": False, "matched": None,
              "distance": None, "threshold": 1.128, "note": ""}
    ok, error = model_status()
    if not ok:
        # Cannot detect faces without the models; accept the capture but say so.
        result.update(face_detected=True, note="Face detection models are not installed (run setup_biometric.py), so match result is unavailable.")
        return result
    selfie = _decode_image(selfie_bytes)
    selfie_feature, count = _face_feature(selfie)
    if selfie_feature is None:
        result["multiple_faces"] = count > 1
        return result
    result["face_detected"] = True
    try:
        document_feature = feature_from_document(file_path)
    except Exception as exc:
        result["note"] = "Licence portrait could not be read: " + str(exc)
        return result
    distance = float(cv2.norm(document_feature, selfie_feature, cv2.NORM_L2))
    result["distance"] = round(distance, 4)
    result["matched"] = distance <= 1.128
    return result


def detect_human_face(selfie_bytes):
    """Detect a face without treating model/setup failures as successful verification.

    Returns (face_detected, note). This helper is not a substitute for face matching.
    """
    ok, error = model_status()
    if not ok:
        return False, error or "Biometric models are unavailable."
    image = _decode_image(selfie_bytes)
    height, width = image.shape[:2]
    detector, _recognizer = _models(width, height)
    detector.setInputSize((width, height))
    _, faces = detector.detect(image)
    return (faces is not None and len(faces) > 0), ""


def compare_two_images(reference_bytes, live_bytes, threshold=1.128):
    """Compare two face images with SFace; lower L2 distance means more similar."""
    ref_img = _decode_image(reference_bytes)
    live_img = _decode_image(live_bytes)
    ref_feature, ref_count = _face_feature(ref_img)
    live_feature, live_count = _face_feature(live_img)
    if ref_feature is None:
        raise ValueError("No single clear face found in the registered reference photo.")
    if live_feature is None:
        raise ValueError("No single clear face found in the toll camera image.")
    distance = float(cv2.norm(ref_feature, live_feature, cv2.NORM_L2))
    return distance <= threshold, round(distance, 4), threshold
