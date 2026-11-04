"""
filesender.py — Pi Zero 2W Frame Capture + Upload
==================================================
Single file supporting all camera types via camera.ini:
  type=1 : Raspberry Pi camera (rpicam-vid)
  type=2 : USB webcam (v4l2)
  type=3 : Imou Ranger S2 (RTSP)

PTZ support via camera.ini:
  ptz.enabled=1, ptz.type=onvif  → Imou ONVIF PTZ
  ptz.enabled=1, ptz.type=servo  → GPIO servo PTZ
  ptz.enabled=0                  → no PTZ

Audio via camera.ini:
  audio.enabled=0 → /tmp/frames/ pipeline (default, no buffering)
  audio.enabled=1 → RTSP MediaPlayer (requires camera type=3)

Run:
    python3 ~/filesender.py
Config:
    ~/camera.ini
"""

import configparser
import subprocess
import requests
import os
import glob
import time
import queue
import threading
import logging
import shutil
from datetime import datetime

# ── Load config ───────────────────────────────────────────────────────────────
cfg = configparser.ConfigParser()
cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera.ini")
cfg.read(cfg_path)

# Camera
CAMERA_TYPE = cfg.getint("camera", "type")
CAM_WIDTH   = cfg.getint("camera", "width")
CAM_HEIGHT  = cfg.getint("camera", "height")
CAM_FPS     = cfg.getint("camera", "fps")

# Webcam overrides
if CAMERA_TYPE == 2:
    CAM_WIDTH  = cfg.getint("webcam", "width",  fallback=640)
    CAM_HEIGHT = cfg.getint("webcam", "height", fallback=480)
    CAM_FPS    = cfg.getint("webcam", "fps",    fallback=10)

# PTZ
PTZ_ENABLED = cfg.getboolean("ptz", "enabled", fallback=False)
PTZ_TYPE    = cfg.get("ptz", "type", fallback="none")

# Audio
AUDIO_ENABLED = cfg.getboolean("audio", "enabled", fallback=False)

# Server
EC2_URL     = cfg.get("server", "ec2_url")
CAMERA_UUID = cfg.get("server", "camera_uuid")

# Capture
BATCH_FRAMES   = cfg.getint("capture", "batch_frames",   fallback=45)
OVERLAP_FRAMES = cfg.getint("capture", "overlap_frames", fallback=15)
ADVANCE_FRAMES = cfg.getint("capture", "advance_frames", fallback=30)
KEEP_FRAMES    = cfg.getint("capture", "keep_frames",    fallback=200)
QUEUE_DEPTH    = cfg.getint("capture", "queue_depth",    fallback=3)
UPLOAD_TIMEOUT = cfg.getint("capture", "upload_timeout", fallback=25)
MODE_POLL_SEC  = cfg.getfloat("capture", "mode_poll_sec", fallback=2.0)
PTZ_POLL_SEC   = cfg.getfloat("capture", "ptz_poll_sec",  fallback=0.5)

CAPTURE_DIR = "/tmp/frames"
LOG_FILE    = "/home/medtrainai/camera_sender/sender/sender.log"
# ──────────────────────────────────────────────────────────────────────────────


# ── Logging ───────────────────────────────────────────────────────────────────
class MsFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created)
        return ct.strftime("%Y-%m-%d %H:%M:%S.") + f"{ct.microsecond // 1000:03d}"

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(handlers=[
    logging.FileHandler(LOG_FILE),
    logging.StreamHandler()
], level=logging.INFO)
for h in logging.root.handlers:
    h.setFormatter(MsFormatter("%(asctime)s | %(levelname)s | %(message)s"))

log = logging.getLogger("sender")

upload_queue = queue.Queue(maxsize=QUEUE_DEPTH)
current_mode = "NORMAL"
mode_lock    = threading.Lock()


# ── Frame cleanup ─────────────────────────────────────────────────────────────

def cleanup_old_frames():
    frames    = sorted(glob.glob(f"{CAPTURE_DIR}/frame*.jpg"))
    to_delete = frames[:-KEEP_FRAMES] if len(frames) > KEEP_FRAMES else []
    for f in to_delete:
        try: os.remove(f)
        except: pass
    if to_delete:
        log.info(f"CLEANUP | deleted {len(to_delete)} | remaining={len(frames)-len(to_delete)}")


# ── Camera streams ────────────────────────────────────────────────────────────

class RaspiCamStream:
    """Raspberry Pi camera via rpicam-vid + ffmpeg."""

    def __init__(self):
        self.vid    = None
        self.ffmpeg = None
        self.quality = cfg.getint("raspicam", "quality", fallback=70)

    def start(self):
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        for f in glob.glob(f"{CAPTURE_DIR}/frame*.jpg"):
            os.remove(f)
        log.info(f"RASPICAM START | {CAM_WIDTH}x{CAM_HEIGHT}@{CAM_FPS}fps")
        self.vid = subprocess.Popen([
            "rpicam-vid",
            "--width",     str(CAM_WIDTH),
            "--height",    str(CAM_HEIGHT),
            "--framerate", str(CAM_FPS),
            "--codec",     "mjpeg",
            "--quality",   str(self.quality),
            "--inline", "--nopreview", "-t", "0", "-o", "-"
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.ffmpeg = subprocess.Popen([
            "ffmpeg", "-f", "mjpeg", "-i", "pipe:0",
            "-q:v", "2", "-y", f"{CAPTURE_DIR}/frame%08d.jpg"
        ], stdin=self.vid.stdout, stderr=subprocess.DEVNULL)
        self.vid.stdout.close()
        return self._wait_for_frames("RASPICAM READY")

    def stop(self):
        for p in [self.ffmpeg, self.vid]:
            if p:
                try: p.terminate(); p.wait(timeout=5)
                except: p.kill()

    def alive(self):
        return (self.vid and self.vid.poll() is None and
                self.ffmpeg and self.ffmpeg.poll() is None)

    def _wait_for_frames(self, msg, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if glob.glob(f"{CAPTURE_DIR}/frame*.jpg"):
                log.info(f"{msg} | frames arriving")
                return True
            time.sleep(0.5)
        log.error(f"{msg} TIMEOUT")
        return False


class WebcamStream:
    """USB webcam via v4l2 + ffmpeg."""

    def __init__(self):
        self.ffmpeg = None
        self.device = cfg.get("webcam", "device", fallback="/dev/video0")

    def start(self):
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        for f in glob.glob(f"{CAPTURE_DIR}/frame*.jpg"):
            os.remove(f)
        log.info(f"WEBCAM START | {self.device} | {CAM_WIDTH}x{CAM_HEIGHT}@{CAM_FPS}fps")
        self.ffmpeg = subprocess.Popen([
            "ffmpeg",
            "-f",          "v4l2",
            "-video_size", f"{CAM_WIDTH}x{CAM_HEIGHT}",
            "-framerate",  str(CAM_FPS),
            "-i",          self.device,
            "-vf",         "format=yuvj420p",
            "-q:v",        "2", "-y",
            f"{CAPTURE_DIR}/frame%08d.jpg"
        ], stderr=subprocess.DEVNULL)
        return self._wait_for_frames("WEBCAM READY")

    def stop(self):
        if self.ffmpeg:
            try: self.ffmpeg.terminate(); self.ffmpeg.wait(timeout=5)
            except: self.ffmpeg.kill()

    def alive(self):
        return self.ffmpeg and self.ffmpeg.poll() is None

    def _wait_for_frames(self, msg, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if glob.glob(f"{CAPTURE_DIR}/frame*.jpg"):
                log.info(f"{msg} | frames arriving")
                return True
            time.sleep(0.5)
        log.error(f"{msg} TIMEOUT")
        return False


class ImouStream:
    """Imou Ranger S2 via RTSP + ffmpeg."""

    def __init__(self):
        self.ffmpeg  = None
        self.rtsp    = cfg.get("imou", "rtsp_url")

    def start(self):
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        for f in glob.glob(f"{CAPTURE_DIR}/frame*.jpg"):
            os.remove(f)
        log.info(f"IMOU START | {CAM_WIDTH}x{CAM_HEIGHT}@{CAM_FPS}fps")
        self.ffmpeg = subprocess.Popen([
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-i",              self.rtsp,
            "-vf",             f"fps={CAM_FPS},scale={CAM_WIDTH}:{CAM_HEIGHT}",
            "-q:v",            "2", "-y",
            f"{CAPTURE_DIR}/frame%08d.jpg"
        ], stderr=subprocess.DEVNULL)
        return self._wait_for_frames("IMOU READY")

    def stop(self):
        if self.ffmpeg:
            try: self.ffmpeg.terminate(); self.ffmpeg.wait(timeout=5)
            except: self.ffmpeg.kill()

    def alive(self):
        return self.ffmpeg and self.ffmpeg.poll() is None

    def _wait_for_frames(self, msg, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if glob.glob(f"{CAPTURE_DIR}/frame*.jpg"):
                log.info(f"{msg} | frames arriving")
                return True
            time.sleep(0.5)
        log.error(f"{msg} TIMEOUT")
        return False


def create_stream():
    """Factory — returns correct stream based on camera.ini."""
    if CAMERA_TYPE == 1:
        return RaspiCamStream()
    elif CAMERA_TYPE == 2:
        return WebcamStream()
    elif CAMERA_TYPE == 3:
        return ImouStream()
    else:
        raise ValueError(f"Unknown camera type: {CAMERA_TYPE}")


# ── PTZ controller ────────────────────────────────────────────────────────────

class NullPTZ:
    """No-op PTZ for cameras without pan/tilt."""
    def execute(self, command, step=5): return 0, 0
    def centre(self): return 0, 0
    @property
    def position(self): return {"pan": 0, "tilt": 0, "available": False}


def create_ptz():
    """Factory — returns correct PTZ controller based on camera.ini."""
    if not PTZ_ENABLED:
        log.info("PTZ | disabled in camera.ini")
        return NullPTZ()
    if PTZ_TYPE == "onvif":
        from ptz_onvif import ONVIFController
        return ONVIFController()
    elif PTZ_TYPE == "servo":
        from ptz_servo import ServoController
        return ServoController()
    else:
        log.info(f"PTZ | unknown type '{PTZ_TYPE}' — disabled")
        return NullPTZ()


# ── Mode polling ──────────────────────────────────────────────────────────────

WIFI_FORCE_AP_FLAG = "/run/wifi_manager/force_ap"


def request_wifi_setup():
    """EC2 asked for Wi-Fi setup: ack it, then tell wifi_manager to start Bluetooth setup."""
    log.info("WIFI SETUP | requested by server — starting Bluetooth setup mode")
    try:
        # Lets the server clear the flag so the camera doesn't re-enter setup later
        requests.post(f"{EC2_URL}/camera/wifi-setup/ack",
                      json={"camera_uuid": CAMERA_UUID}, timeout=5)
    except Exception as e:
        log.warning(f"WIFI SETUP | ack failed: {e}")
    try:
        with open(WIFI_FORCE_AP_FLAG, "w"):
            pass
    except OSError as e:
        log.error(f"WIFI SETUP | could not signal wifi_manager ({e}) — is it installed?")


def mode_poll_loop():
    global current_mode
    prev = "NORMAL"
    prev_wifi_setup = False
    while True:
        try:
            r = requests.get(
                f"{EC2_URL}/camera/mode/{CAMERA_UUID}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                # Optional field: {"mode": "...", "wifi_setup": true}
                wifi_setup = bool(data.get("wifi_setup", False))
                if wifi_setup and not prev_wifi_setup:
                    request_wifi_setup()
                prev_wifi_setup = wifi_setup
                new = data.get("mode", "NORMAL")
                with mode_lock:
                    current_mode = new
                if prev == "NORMAL" and new == "LIVE":
                    log.info("MODE | NORMAL → LIVE | pausing uploads")
                elif prev == "LIVE" and new == "NORMAL":
                    log.info("MODE | LIVE → NORMAL | resuming uploads")
                prev = new
        except Exception as e:
            log.error(f"MODE POLL ERROR | {e}")
        time.sleep(MODE_POLL_SEC)


# ── PTZ polling ───────────────────────────────────────────────────────────────

def ptz_poll_loop(ptz):
    while True:
        try:
            r = requests.get(
                f"{EC2_URL}/camera/ptz/{CAMERA_UUID}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                cmd  = data.get("command")
                step = data.get("step", 5)

                if cmd:
                    log.info(f"PTZ RECEIVED | command={cmd} | step={step}")

                moved_pan, moved_tilt = ptz.execute(cmd, step)

                if cmd:
                    log.info(f"PTZ REPORTING | moved_pan={moved_pan} | moved_tilt={moved_tilt}")

                requests.post(
                    f"{EC2_URL}/camera/ptz/report",
                    json={"camera_uuid": CAMERA_UUID,
                          "moved_pan":   moved_pan,
                          "moved_tilt":  moved_tilt},
                    timeout=5
                )
        except Exception as e:
            log.error(f"PTZ POLL ERROR | {e}")
        time.sleep(PTZ_POLL_SEC)


# ── Capture loop ──────────────────────────────────────────────────────────────

def capture_loop(stream):
    log.info(f"CAPTURE LOOP | waiting for first {BATCH_FRAMES} frames...")
    deadline = time.time() + 20
    while time.time() < deadline:
        if len(glob.glob(f"{CAPTURE_DIR}/frame*.jpg")) >= BATCH_FRAMES:
            break
        time.sleep(0.2)
    else:
        log.error("CAPTURE LOOP | timeout — camera issue?")
        return

    log.info("CAPTURE LOOP | starting")

    while stream.alive():
        with mode_lock:
            if current_mode == "LIVE":
                time.sleep(0.5)
                continue

        cycle_start = time.time()

        while stream.alive():
            with mode_lock:
                if current_mode == "LIVE": break
            frames = sorted(glob.glob(f"{CAPTURE_DIR}/frame*.jpg"))
            if len(frames) >= BATCH_FRAMES:
                break
            time.sleep(0.1)

        with mode_lock:
            if current_mode == "LIVE": continue

        cleanup_old_frames()
        frames = sorted(glob.glob(f"{CAPTURE_DIR}/frame*.jpg"))
        if len(frames) < BATCH_FRAMES:
            time.sleep(1)
            continue

        selected  = frames[-BATCH_FRAMES:]
        batch_id  = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        batch_dir = f"/tmp/batch_{batch_id}"
        os.makedirs(batch_dir, exist_ok=True)

        for i, src in enumerate(selected):
            shutil.copy2(src, f"{batch_dir}/frame{i:04d}.jpg")

        log.info(f"CAPTURE DONE | batch={batch_id} | frames={len(selected)}")

        try:
            upload_queue.put_nowait((batch_id, batch_dir))
            log.info(f"QUEUED | batch={batch_id} | q={upload_queue.qsize()}")
        except queue.Full:
            try:
                did, ddir = upload_queue.get_nowait()
                log.warning(f"QUEUE FULL | DROPPED={did}")
                shutil.rmtree(ddir, ignore_errors=True)
                upload_queue.put_nowait((batch_id, batch_dir))
            except Exception as e:
                log.error(f"QUEUE ERROR | {e}")
                shutil.rmtree(batch_dir, ignore_errors=True)

        time.sleep(max(0, (ADVANCE_FRAMES / CAM_FPS) - (time.time() - cycle_start)))


# ── Upload loop ───────────────────────────────────────────────────────────────

def upload_loop():
    while True:
        batch_id, batch_dir = upload_queue.get()
        with mode_lock:
            if current_mode == "LIVE":
                log.info(f"UPLOAD SKIP | batch={batch_id} | LIVE")
                shutil.rmtree(batch_dir, ignore_errors=True)
                upload_queue.task_done()
                continue

        paths = sorted(glob.glob(f"{batch_dir}/*.jpg"))
        log.info(f"UPLOAD START | batch={batch_id} | frames={len(paths)}")
        t = datetime.now()
        files = []
        try:
            files = [("frames", (os.path.basename(p), open(p,"rb"), "image/jpeg"))
                     for p in paths]
            r = requests.post(f"{EC2_URL}/upload",
                              params={"batch_id": batch_id, "camera_uuid": CAMERA_UUID},
                              files=files, timeout=UPLOAD_TIMEOUT)
            r.raise_for_status()
            log.info(f"UPLOAD OK | batch={batch_id} | "
                     f"elapsed={(datetime.now()-t).total_seconds():.3f}s")
        except requests.Timeout:
            log.error(f"UPLOAD TIMEOUT | batch={batch_id}")
        except requests.ConnectionError:
            log.error(f"UPLOAD FAIL | {EC2_URL} unreachable")
        except Exception as e:
            log.error(f"UPLOAD ERROR | {e}")
        finally:
            for _, (_, fh, _) in files:
                try: fh.close()
                except: pass
            shutil.rmtree(batch_dir, ignore_errors=True)
        upload_queue.task_done()


# ── Main ──────────────────────────────────────────────────────────────────────

CAMERA_NAMES = {1: "Raspberry Pi Camera", 2: "USB Webcam", 3: "Imou Ranger S2"}
PTZ_NAMES    = {True: f"enabled ({PTZ_TYPE})", False: "disabled"}

if __name__ == "__main__":
    log.info("=== Pi Zero 2W File Sender starting ===")
    log.info(f"Config   : {cfg_path}")
    log.info(f"Camera   : {CAMERA_NAMES.get(CAMERA_TYPE, 'unknown')} (type={CAMERA_TYPE})")
    log.info(f"PTZ      : {PTZ_NAMES[PTZ_ENABLED]}")
    log.info(f"Audio    : {'enabled' if AUDIO_ENABLED else 'disabled'}")
    log.info(f"EC2      : {EC2_URL}")
    log.info(f"Batch    : {BATCH_FRAMES} frames | Keep: {KEEP_FRAMES} max")

    # Check EC2
    try:
        requests.get(f"{EC2_URL}/health", timeout=5).raise_for_status()
        log.info(f"SERVER OK | {EC2_URL}")
    except Exception:
        log.error(f"SERVER UNREACHABLE | {EC2_URL}")
        exit(1)

    # Create camera stream
    stream = create_stream()
    if not stream.start():
        log.error("Camera failed — check camera.ini and hardware")
        exit(1)

    # Create PTZ controller
    ptz = create_ptz()

    # Start threads
    threading.Thread(target=mode_poll_loop, daemon=True).start()
    threading.Thread(target=upload_loop,    daemon=True).start()

    if PTZ_ENABLED and not isinstance(ptz, NullPTZ):
        threading.Thread(target=ptz_poll_loop, args=(ptz,), daemon=True).start()
        log.info(f"PTZ poll started | every {PTZ_POLL_SEC}s")

    # Run capture loop
    try:
        capture_loop(stream)
    except KeyboardInterrupt:
        log.info("=== Sender stopped ===")
    finally:
        stream.stop()
