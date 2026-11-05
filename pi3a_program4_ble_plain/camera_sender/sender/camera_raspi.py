"""Camera capture for WebRTC sender — Raspberry Pi Zero 2W.

Reads JPEG frames written by the existing rpicam-vid pipeline in
sender_v3_live_v2.py (/tmp/frames/frame*.jpg).
No second camera process — reuses frames already being captured.

Drop-in replacement for camera.py (Windows DirectShow version).
Same CameraManager / OpenCVVideoTrack interface so sender.py is unchanged.
"""

import cv2
import glob
import logging
import numpy as np
from typing import Optional
from aiortc import VideoStreamTrack
from av import VideoFrame

logger = logging.getLogger(__name__)

FRAMES_DIR = "/tmp/frames"


class OpenCVVideoTrack(VideoStreamTrack):
    """Pi Zero 2W video track — reads from rpicam-vid frame output."""

    def __init__(self, width: int = 640, height: int = 480, fps: int = 10):
        super().__init__()
        self.width      = width
        self.height     = height
        self.fps        = fps
        self.counter    = 0
        self.frames_dir = FRAMES_DIR          # set here — not in a separate method
        self.cap        = None                 # not used on Pi
        logger.info(
            "Pi camera track ready — %dx%d @ %dfps — frames: %s",
            width, height, fps, FRAMES_DIR
        )

    async def recv(self) -> VideoFrame:
        """Read latest JPEG from rpicam-vid pipeline and return as VideoFrame."""
        pts, time_base = await self.next_timestamp()

        # Read latest frame from pipeline
        frame = None
        frames = sorted(glob.glob(f"{self.frames_dir}/frame*.jpg"))
        if frames:
            img = cv2.imread(frames[-1])
            if img is not None:
                frame = img

        if frame is None:
            logger.warning(
                "No frames in %s — rpicam-vid running? Using test pattern.",
                self.frames_dir
            )
            frame = self._create_test_pattern()

        # Resize if needed
        if frame.shape[:2] != (self.height, self.width):
            frame = cv2.resize(frame, (self.width, self.height))

        # BGR → RGB for WebRTC
        frame_rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts       = pts
        video_frame.time_base = time_base

        self.counter += 1
        if self.counter % 30 == 0:
            logger.info("Sent frame %d", self.counter)

        return video_frame

    def _create_test_pattern(self) -> np.ndarray:
        """Animated test pattern shown while camera pipeline warms up."""
        frame = np.full((self.height, self.width, 3), 40, dtype=np.uint8)
        x = int((self.counter * 8) % self.width)
        y = int(self.height // 2 + 80 * np.sin(self.counter * 0.07))
        cv2.circle(frame, (x, y), 25, (180, 180, 180), -1)
        cv2.putText(
            frame,
            f"Pi camera warming up... frame {self.counter}",
            (20, self.height // 2 - 20),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 1,
        )
        cv2.putText(
            frame,
            f"Waiting for {FRAMES_DIR}/frame*.jpg",
            (20, self.height // 2 + 20),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (160, 160, 160), 1,
        )
        return frame

    def close(self) -> None:
        """Nothing to release — rpicam-vid is managed by sender_v3_live_v2.py."""
        logger.info("Pi camera track closed (frame %d total)", self.counter)


class CameraManager:
    """Manages Pi Zero 2W camera track lifecycle."""

    def __init__(self, width: int = 640, height: int = 480, fps: int = 10):
        self.width  = width
        self.height = height
        self.fps    = fps
        self.track: Optional[OpenCVVideoTrack] = None

    def create_track(self) -> OpenCVVideoTrack:
        """Create and return a Pi-compatible video track."""
        self.track = OpenCVVideoTrack(self.width, self.height, self.fps)
        logger.info(
            "Pi camera track created: %dx%d @ %dfps", self.width, self.height, self.fps
        )
        return self.track

    def close(self) -> None:
        """Close camera track."""
        if self.track:
            self.track.close()
            logger.info("Camera manager closed")

    @staticmethod
    def is_pipeline_ready() -> bool:
        """Return True if rpicam-vid has written at least one frame."""
        frames = glob.glob(f"{FRAMES_DIR}/frame*.jpg")
        if not frames:
            logger.warning(
                "rpicam-vid pipeline not ready — no frames in %s. "
                "Ensure sender_v3_live_v2.py is running in NORMAL mode first.",
                FRAMES_DIR
            )
            return False
        return True

    @staticmethod
    def get_available_cameras() -> int:
        """Returns 1 if pipeline frames are available, 0 otherwise."""
        return 1 if CameraManager.is_pipeline_ready() else 0
