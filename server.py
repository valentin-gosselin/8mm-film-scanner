from flask import Flask, Response, request
import json
import os
import logging

from filmscanner import FilmScanner
from notification import MailCallback
from utils import DashboardCallback

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="frontend/build", static_url_path="/")

# Fichier de configuration pour persister les paramètres
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "scanner_config.json")

def load_config():
    """Charge la configuration depuis le fichier JSON"""
    default_config = {
        "shutter_speed": 30000,
        "analog_gain": 1.0,
        "digital_gain": 1.0,
        "brightness": 0.0,
        "contrast": 1.0,
        "saturation": 1.0,
        "sharpness": 1.0,
        "awb_mode": "auto",
        "flip_h": False,
        "flip_v": False,
        "n_frames": 3800,
        "output_directory": "/media/pi/PortableSSD/unnamed/frames"
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                saved_config = json.load(f)
                # Fusionner avec les valeurs par défaut
                default_config.update(saved_config)
    except Exception as e:
        print(f"Erreur lors du chargement de la config: {e}")
    return default_config

def save_config(config):
    """Sauvegarde la configuration dans le fichier JSON"""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Erreur lors de la sauvegarde de la config: {e}")

def get_current_config():
    """Récupère la configuration actuelle depuis le scanner"""
    return {
        "shutter_speed": scanner.camera.shutter_speed,
        "analog_gain": scanner.camera.analog_gain,
        "digital_gain": scanner.camera.digital_gain,
        "brightness": scanner.camera.brightness,
        "contrast": scanner.camera.contrast,
        "saturation": scanner.camera.saturation,
        "sharpness": scanner.camera.sharpness,
        "awb_mode": scanner.camera.awb_mode,
        "flip_h": scanner.camera.software_flip_h,
        "flip_v": scanner.camera.software_flip_v,
        "n_frames": scanner.n_frames,
        "output_directory": scanner.output_directory
    }

dashboard_callback = DashboardCallback()
mail_callback = MailCallback()

scanner = FilmScanner(callback=[dashboard_callback, mail_callback])
scanner.camera.resolution = (800, 600)

# Charger et appliquer la configuration sauvegardée
config = load_config()
logger.info(f"=== STARTUP: Loading config ===")
logger.info(f"Config shutter_speed: {config['shutter_speed']}")
logger.info(f"Camera shutter_speed BEFORE: {scanner.camera.shutter_speed}")
try:
    logger.info(f"Camera exposure_speed BEFORE: {scanner.camera.exposure_speed}")
except Exception as e:
    logger.info(f"Camera exposure_speed BEFORE: (not available: {e})")
try:
    logger.info(f"Camera exposure_mode BEFORE: {scanner.camera.exposure_mode}")
except Exception as e:
    logger.info(f"Camera exposure_mode BEFORE: (not available: {e})")

# Force manual exposure mode before applying shutter_speed
scanner.camera.exposure_mode = "off"
logger.info(f"Set exposure_mode to 'off'")

scanner.camera.shutter_speed = config["shutter_speed"]
logger.info(f"Set shutter_speed to {config['shutter_speed']}")
logger.info(f"Camera shutter_speed AFTER: {scanner.camera.shutter_speed}")
try:
    logger.info(f"Camera exposure_speed AFTER: {scanner.camera.exposure_speed}")
except Exception as e:
    logger.info(f"Camera exposure_speed AFTER: (not available: {e})")

scanner.camera.analog_gain = config["analog_gain"]
scanner.camera.digital_gain = config["digital_gain"]
scanner.camera.brightness = config["brightness"]
scanner.camera.contrast = config["contrast"]
scanner.camera.saturation = config["saturation"]
scanner.camera.sharpness = config["sharpness"]
scanner.camera.awb_mode = config["awb_mode"]
scanner.camera.software_flip_h = config["flip_h"]
scanner.camera.software_flip_v = config["flip_v"]
scanner.n_frames = config["n_frames"]
scanner.output_directory = config["output_directory"]
logger.info(f"=== STARTUP: Config applied ===")


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/backend/advance", methods=("POST",))
def advance():
    if scanner.is_advance_allowed:
        scanner.advance()
    return "", 204


@app.route("/backend/dashboard")
def dashboard():
    return dashboard_callback.scanner_state_dict


@app.route("/backend/dashboard-sse")
def dashboard_sse():
    return Response(dashboard_callback.subscribe_to_sse(), mimetype="text/event-stream")


@app.route("/backend/dismiss", methods=("POST",))
def dismiss():
    scanner.last_scan_end_info = "dismissed"
    return "", 204


@app.route("/backend/fastforward", methods=("POST",))
def fast_forward():
    if not scanner.is_fast_forwarding and scanner.is_fast_forward_allowed:
        scanner.start_fast_forward()
    return "", 204


@app.route("/backend/rewind", methods=("POST",))
def rewind():
    if not scanner.is_fast_forwarding and scanner.is_fast_forward_allowed:
        scanner.start_rewind()
    return "", 204


@app.route("/backend/stop", methods=("POST",))
def stop_motor():
    if scanner.is_fast_forwarding:
        scanner.stop_fast_forward()
    return "", 204


@app.route("/backend/focuszoom", methods=("POST",))
def toggle_focus_zoom():
    if scanner.is_zoom_toggle_allowed:
        scanner.toggle_zoom()
    return "", 204


@app.route("/backend/light", methods=("POST",))
def toggle_light():
    if scanner.is_light_toggle_allowed:
        scanner.toggle_light()
    return "", 204


@app.route("/backend/poweroff", methods=("POST",))
def poweroff():
    scanner.poweroff()
    return "", 204


@app.route("/backend/preview")
def preview():
    def generate():
        for frame in scanner.preview():
            yield b"--frame\r\n" + b"ContentType: image/jpeg\r\n\r\n" + frame + b"\r\n\r\n"

    return app.response_class(
        generate(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/backend/scan", methods=("POST",))
def scan():
    if not scanner.is_scanning and scanner.is_scanning_allowed:
        data = request.get_json()
        output_directory = data["output_directory"]
        n_frames = int(data["n_frames"])
        # Sauvegarder les paramètres de scan
        scanner.output_directory = output_directory
        scanner.n_frames = n_frames
        save_config(get_current_config())
        scanner.start_scan(
            output_directory=output_directory,
            n_frames=n_frames,
        )
    elif scanner.is_scanning:
        scanner.stop_scan()
    return "", 204


@app.route("/backend/scan-setup")
def scan_setup():
    return {"n_frames": scanner.n_frames, "output_directory": scanner.output_directory}


@app.route("/backend/camera-settings")
def get_camera_settings():
    return {
        "shutter_speed": scanner.camera.shutter_speed,
        "analog_gain": scanner.camera.analog_gain,
        "digital_gain": scanner.camera.digital_gain,
        "brightness": scanner.camera.brightness,
        "contrast": scanner.camera.contrast,
        "saturation": scanner.camera.saturation,
        "sharpness": scanner.camera.sharpness,
        "awb_mode": scanner.camera.awb_mode,
        "flip_h": scanner.camera.software_flip_h,
        "flip_v": scanner.camera.software_flip_v,
    }


@app.route("/backend/camera-settings", methods=("POST",))
def set_camera_settings():
    data = request.get_json()
    logger.info(f"=== POST camera-settings ===")
    logger.info(f"Received data: {data}")
    logger.info(f"Camera shutter_speed BEFORE: {scanner.camera.shutter_speed}")

    # Always ensure exposure_mode is off for manual control
    scanner.camera.exposure_mode = "off"
    if "shutter_speed" in data:
        scanner.camera.shutter_speed = int(data["shutter_speed"])
        logger.info(f"Set shutter_speed to {data['shutter_speed']}")
        logger.info(f"Camera shutter_speed AFTER: {scanner.camera.shutter_speed}")
    if "analog_gain" in data:
        scanner.camera.analog_gain = float(data["analog_gain"])
    if "digital_gain" in data:
        scanner.camera.digital_gain = float(data["digital_gain"])
    if "brightness" in data:
        scanner.camera.brightness = float(data["brightness"])
    if "contrast" in data:
        scanner.camera.contrast = float(data["contrast"])
    if "saturation" in data:
        scanner.camera.saturation = float(data["saturation"])
    if "sharpness" in data:
        scanner.camera.sharpness = float(data["sharpness"])
    if "awb_mode" in data:
        scanner.camera.awb_mode = data["awb_mode"]
    if "flip_h" in data:
        scanner.camera.software_flip_h = bool(data["flip_h"])
    if "flip_v" in data:
        scanner.camera.software_flip_v = bool(data["flip_v"])
    # Sauvegarder la config après chaque modification
    save_config(get_current_config())
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
