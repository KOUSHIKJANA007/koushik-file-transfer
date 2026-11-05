"""
ptz_onvif.py — ONVIF PTZ Controller for Imou Ranger S2
=======================================================
Calibration-based step counting approach.
GetStatus returns fake values — ignored entirely.
Position tracked by counting steps from calibrated centre.

Calibration (done once on startup):
    1. Pan right to hard stop (8 seconds full speed)
    2. Count 4 steps left → mechanical centre
    3. Centre = step 0, range = -4 to +4 steps

Each step = 0.5 seconds at full speed ≈ 38 degrees
Total range = 9 steps × 38° ≈ 342 degrees (matches 340° spec)

Imou Ranger S2:
    IP       : 192.168.29.202
    Port     : 80
    User     : admin
    Password : L21s12Q9
"""

import logging
import time

log = logging.getLogger("ptz_onvif")

IMOU_HOST       = "192.168.29.202"
IMOU_PORT       = 80
IMOU_USER       = "admin"
IMOU_PASS       = "L21s12Q9"

STEP_DURATION   = 0.5    # seconds per step at full speed
HARD_STOP_TIME  = 8.0    # seconds to reach hard stop from anywhere
STEPS_TO_CENTRE = 5      # steps from right hard stop to centre
MAX_STEPS       = 5      # ±4 steps from centre (full range)
FULL_SPEED      = 1.0    # ONVIF velocity for ContinuousMove


class ONVIFController:
    """
    Controls Imou Ranger S2 PTZ via ONVIF.
    Uses step counting — does not rely on GetStatus (returns fake values).
    Calibrates on startup by finding right hard stop then counting to centre.
    """

    def __init__(self, calibrate_on_start: bool = False):
        self._available  = False
        self._ptz        = None
        self._token      = "Profile000"
        self._step_pos   = 0      # current position in steps (0 = centre)
        self._tilt_pos   = 0.0    # tilt tracking (approximate)
        self._calibrated = False
        self._init()
        if calibrate_on_start and self._available:
            self.calibrate()

    def _init(self):
        try:
            from onvif import ONVIFCamera
            cam             = ONVIFCamera(IMOU_HOST, IMOU_PORT, IMOU_USER, IMOU_PASS)
            self._ptz       = cam.create_ptz_service()
            self._available = True
            log.info(f"ONVIF ready | {IMOU_HOST}:{IMOU_PORT} | token={self._token}")
        except Exception as e:
            log.error(f"ONVIF init failed: {e} — PTZ disabled")
            self._available = False

    # ── Low level move ────────────────────────────────────────────────────────

    def _move(self, pan_vel: float, tilt_vel: float, duration: float):
        """Run ContinuousMove for duration seconds then stop."""
        req              = self._ptz.create_type("ContinuousMove")
        req.ProfileToken = self._token
        req.Velocity     = {"PanTilt": {"x": pan_vel, "y": tilt_vel}}
        self._ptz.ContinuousMove(req)
        time.sleep(duration)
        self._ptz.Stop({"ProfileToken": self._token})
        time.sleep(0.3)   # settle time

    # ── Calibration ───────────────────────────────────────────────────────────

    def calibrate(self):
        """
        Find mechanical centre by:
        1. Pan right to hard stop (full speed, HARD_STOP_TIME seconds)
        2. Move left STEPS_TO_CENTRE steps
        3. Mark this as step 0 (centre)
        """
        if not self._available:
            return
        log.info("PTZ CALIBRATE | panning to right hard stop...")
        self._move(FULL_SPEED, 0.0, HARD_STOP_TIME)

        log.info(f"PTZ CALIBRATE | moving {STEPS_TO_CENTRE} steps left to centre...")
        for i in range(STEPS_TO_CENTRE):
            self._move(-FULL_SPEED, 0.0, STEP_DURATION)
            log.info(f"PTZ CALIBRATE | step {i+1}/{STEPS_TO_CENTRE}")

        self._step_pos   = 0
        self._tilt_pos   = 0.0
        self._calibrated = True
        log.info("PTZ CALIBRATE | complete — at centre (step 0)")

    # ── Public API ────────────────────────────────────────────────────────────

    def execute(self, command: str, step_degrees: float = 5.0) -> tuple:
        """
        Execute PTZ command from EC2.
        Returns (moved_pan_steps, moved_tilt) — pan in steps, tilt approximate.
        """
        if not self._available or command is None:
            return 0, 0

        if   command == "pan_left":   return self._pan(-1)
        elif command == "pan_right":  return self._pan(+1)
        elif command == "tilt_up":    return self._tilt(+1)
        elif command == "tilt_down":  return self._tilt(-1)
        elif command == "centre":     return self._centre()
        else:
            log.warning(f"Unknown PTZ command: {command}")
            return 0, 0

    def _pan(self, direction: int) -> tuple:
        """
        Pan one step in direction (+1=right, -1=left).
        Respects ±MAX_STEPS limit.
        """
        new_pos = self._step_pos + direction
        if new_pos > MAX_STEPS:
            log.warning(f"PTZ PAN | at right limit ({MAX_STEPS} steps)")
            return 0, 0
        if new_pos < -MAX_STEPS:
            log.warning(f"PTZ PAN | at left limit (-{MAX_STEPS} steps)")
            return 0, 0

        vel = FULL_SPEED if direction > 0 else -FULL_SPEED
        try:
            self._move(vel, 0.0, STEP_DURATION)
            self._step_pos = new_pos
            moved = direction * 34   # approx degrees per step
            log.info(
                f"PTZ PAN | dir={'+' if direction>0 else '-'} | "
                f"step_pos={self._step_pos} | {moved:+d}°"
            )
            return moved, 0
        except Exception as e:
            log.error(f"PTZ pan error: {e}")
            return 0, 0

    def _tilt(self, direction: int) -> tuple:
        """Tilt one step up (+1) or down (-1)."""
        vel = FULL_SPEED if direction > 0 else -FULL_SPEED
        try:
            self._move(0.0, vel, STEP_DURATION)
            self._tilt_pos += direction * 34
            moved = direction * 34
            log.info(
                f"PTZ TILT | dir={'+' if direction>0 else '-'} | "
                f"{moved:+d}°"
            )
            return 0, moved
        except Exception as e:
            log.error(f"PTZ tilt error: {e}")
            return 0, 0

    def _centre(self) -> tuple:
        """
        Return to centre by reversing tracked steps.
        If not calibrated — run full calibration.
        """
        if not self._available:
            return 0, 0

        if not self._calibrated:
            log.info("PTZ CENTRE | not calibrated — running calibration")
            self.calibrate()
            return 0, 0

        if self._step_pos == 0 and self._tilt_pos == 0:
            log.info("PTZ CENTRE | already at centre")
            return 0, 0

        log.info(f"PTZ CENTRE | returning from step={self._step_pos}")

        # Pan back to centre
        pan_moved = 0
        if self._step_pos != 0:
            direction = -1 if self._step_pos > 0 else 1
            steps     = abs(self._step_pos)
            for _ in range(steps):
                vel = FULL_SPEED if direction > 0 else -FULL_SPEED
                self._move(vel, 0.0, STEP_DURATION)
            pan_moved      = -self._step_pos * 34
            self._step_pos = 0

        # Tilt back to centre
        tilt_moved = 0
        if self._tilt_pos != 0:
            direction = -1 if self._tilt_pos > 0 else 1
            vel = FULL_SPEED if direction > 0 else -FULL_SPEED
            self._move(0.0, vel, abs(self._tilt_pos) / 38 * STEP_DURATION)
            tilt_moved     = -int(self._tilt_pos)
            self._tilt_pos = 0.0

        log.info(f"PTZ CENTRE | done | pan_moved={pan_moved} tilt_moved={tilt_moved}")
        return pan_moved, tilt_moved

    @property
    def position(self) -> dict:
        return {
            "step_pos":   self._step_pos,
            "tilt_pos":   self._tilt_pos,
            "available":  self._available,
            "calibrated": self._calibrated,
            "max_steps":  MAX_STEPS,
        }
