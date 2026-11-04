"""
livesenderNoTurn.py — WebRTC sender to the EC2 relay (no TURN)
==============================================================

Pi Zero 2W side of the relay path. Runs NEXT TO the existing livesender.py
(left unchanged for the P2P path) and talks to liveserverNoTurn.py on EC2.

How it differs from livesender.py:
  * Peer is the EC2 relay, not the phone. EC2 has a public IP, so STUN is
    enough — TURN is never used.
  * --id defaults to camera_uuid from camera.ini. The Flutter app watches a
    room by its camera_uuid, so the relay must know the camera by that id.
  * --server defaults to the relay on port 8090.
  * While streaming it asks the relay every few seconds whether anyone is
    still watching (GET /relay/{id}/status) and ends the session as soon as
    nobody is, instead of streaming until ICE times out.

Unchanged from livesender.py:
  * Frames come from /tmp/frames (written by filesender.py) via camera_raspi.
  * Same signaling calls: clear → offer → poll answer → ICE → stream.
  * Offer is renewed every 5 min if nobody answers; relay being down is normal.
  * The relay answers only when a viewer wants this camera, so the Pi does
    not encode or upload video when nobody is watching.

Usage:
    cd ~/camera_sender/sender
    python3 livesenderNoTurn.py                       # id from camera.ini
    python3 livesenderNoTurn.py --server http://151.185.47.108:8090 --id <camera_uuid>

Requires (already on the Pi for livesender.py): common/config.py,
common/logger.py, camera_raspi.py, peer.py, aiohttp, aiortc.
"""

import argparse
import asyncio
import configparser
import os
import sys
from typing import Optional

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import config
from common.logger import setup_logger
from camera_raspi import CameraManager
from peer import PeerConnection

logger = setup_logger(__name__)

DEFAULT_RELAY_URL  = "http://151.185.47.108:8090"
RETRY_DELAY_SEC    = 5     # wait between sessions
SERVER_RETRY_SEC   = 30    # wait between relay reachability checks
SESSION_TIMEOUT    = 300   # renew offer after 5 min if nobody answered
STATUS_POLL_SEC    = 5     # while streaming: is anyone still watching?
NOT_WANTED_LIMIT   = 2     # consecutive "wanted: false" replies before stopping

CAMERA_INI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "camera.ini")


def camera_uuid_from_ini() -> Optional[str]:
    """camera_uuid from camera.ini [server] — the id the app and DB use."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(CAMERA_INI)
        value = cfg.get("server", "camera_uuid", fallback="").strip()
        return value or None
    except Exception:
        return None


class LiveSenderNoTurn:
    """WebRTC sender to the EC2 relay — retries indefinitely."""

    def __init__(self, client_id: str, server_url: str):
        self.client_id      = client_id
        self.server_url     = server_url.replace("ws://", "http://").replace("/ws", "").rstrip("/")
        self.running        = True
        self._session_count = 0
        self._server_up     = False
        self._timeout       = aiohttp.ClientTimeout(total=5)

    # ── Relay health ──────────────────────────────────────────────────

    async def _is_server_up(self) -> bool:
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(f"{self.server_url}/health") as r:
                    return r.status == 200
        except Exception:
            return False

    async def _wait_for_server(self):
        """Wait silently; log only when the state changes."""
        attempt = 0
        while self.running:
            up = await self._is_server_up()
            if up:
                if not self._server_up:
                    logger.info("🟢 Relay online | %s", self.server_url)
                self._server_up = True
                return
            if self._server_up:
                logger.info("🔴 Relay offline | will keep retrying silently")
                self._server_up = False
            attempt += 1
            if attempt == 1 or attempt % 20 == 0:
                logger.info("⏸  Relay unreachable | retrying every %ds", SERVER_RETRY_SEC)
            await asyncio.sleep(SERVER_RETRY_SEC)

    # ── Signaling ─────────────────────────────────────────────────────

    async def _clear(self):
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.post(f"{self.server_url}/signal/clear/{self.client_id}") as r:
                    if r.status == 200:
                        data = await r.json()
                        logger.info("🧹 Cleared %s stale entries", data.get("cleared", 0))
        except Exception as e:
            logger.debug("Clear failed: %s", e)

    async def _send_offer(self, sdp: str) -> bool:
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.post(f"{self.server_url}/signal", json={
                    "type": "offer", "client_id": self.client_id, "sdp": sdp,
                }) as r:
                    if r.status != 200:
                        logger.error("❌ Offer rejected: HTTP %s", r.status)
                        return False
                    return True
        except Exception as e:
            logger.info("🔴 Relay unreachable when sending offer: %s", e)
            self._server_up = False
            return False

    async def _fetch_answer(self) -> Optional[str]:
        """Poll for the relay's answer. The relay answers only when a viewer
        wants this camera, so waiting here is the normal idle state."""
        attempt = 0
        failures = 0
        while self.running:
            await asyncio.sleep(1)
            if attempt >= SESSION_TIMEOUT:
                logger.info("⟳ No viewer for %ds — renewing offer", SESSION_TIMEOUT)
                return None
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as s:
                    async with s.get(f"{self.server_url}/signal/{self.client_id}/answer") as r:
                        if r.status == 200:
                            sdp = (await r.json()).get("sdp")
                            if sdp:
                                logger.info("✅ Relay answered — a viewer is waiting")
                                return sdp
                        failures = 0
            except (aiohttp.ClientError, asyncio.TimeoutError):
                failures += 1
                if failures >= 3:
                    logger.info("🔴 Relay went offline while waiting — restarting")
                    self._server_up = False
                    return None
            attempt += 1
            if attempt % 60 == 0:
                logger.info("⏳ Idle, offer posted (%ds, renew in %ds)",
                            attempt, SESSION_TIMEOUT - attempt)
        return None

    async def _still_wanted(self) -> Optional[bool]:
        """True/False from the relay; None if it could not be asked."""
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as s:
                async with s.get(f"{self.server_url}/relay/{self.client_id}/status") as r:
                    if r.status == 200:
                        return bool((await r.json()).get("wanted"))
        except Exception:
            pass
        return None

    # ── ICE ───────────────────────────────────────────────────────────

    async def _wait_for_ice(self, pc: PeerConnection) -> bool:
        for _ in range(config.ICE_TIMEOUT):
            state = pc.ice_connection_state if pc else "closed"
            if state in {"connected", "completed"}:
                return True
            if state in {"failed", "closed"}:
                logger.error("❌ ICE failed: %s", state)
                return False
            await asyncio.sleep(1)
        logger.error("❌ ICE timeout")
        return False

    # ── One session ───────────────────────────────────────────────────

    async def _run_session(self) -> bool:
        self._session_count += 1
        logger.info("📡 Session %d | %s | id=%s", self._session_count, self.server_url, self.client_id)

        peer_connection = None
        camera_mgr      = None
        try:
            await self._clear()

            camera_mgr  = CameraManager(config.VIDEO_WIDTH, config.VIDEO_HEIGHT, config.VIDEO_FPS)
            video_track = camera_mgr.create_track()
            if not video_track:
                logger.error("❌ No video track — is filesender.py running?")
                return False

            # STUN only. The relay has a public IP, so TURN is never needed.
            peer_connection = PeerConnection(config.STUN_SERVER, [])
            await peer_connection.initialize(video_track)

            offer_sdp = await peer_connection.create_offer()
            if not await self._send_offer(offer_sdp):
                return False
            logger.info("📡 Offer posted — idle until a viewer asks for this camera")

            answer_sdp = await self._fetch_answer()
            if not answer_sdp or not self.running:
                return False

            await peer_connection.set_remote_description(answer_sdp, "answer")
            if not await self._wait_for_ice(peer_connection):
                return False

            logger.info("📺 Connected to relay — streaming video")

            not_wanted = 0
            elapsed = 0
            while self.running:
                state = peer_connection.ice_connection_state if peer_connection else "closed"
                if state in {"failed", "closed", "disconnected"}:
                    logger.warning("⚡ Relay link lost (ICE: %s)", state)
                    break
                await asyncio.sleep(1)
                elapsed += 1
                if elapsed % STATUS_POLL_SEC == 0:
                    wanted = await self._still_wanted()
                    not_wanted = not_wanted + 1 if wanted is False else 0
                    if not_wanted >= NOT_WANTED_LIMIT:
                        logger.info("👋 No viewers left — stopping stream")
                        break

            logger.info("📴 Session %d ended (%ds streamed)", self._session_count, elapsed)
            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("❌ Session error: %s", e)
            return False
        finally:
            if peer_connection:
                await peer_connection.close()
            if camera_mgr:
                camera_mgr.close()

    # ── Main loop ─────────────────────────────────────────────────────

    async def start(self):
        logger.info("🚀 LiveSenderNoTurn | id=%s | relay=%s", self.client_id, self.server_url)
        logger.info("🔄 Retries automatically | relay down = normal")
        try:
            while self.running:
                await self._wait_for_server()
                if not self.running:
                    break
                await self._run_session()
                if not self.running:
                    break
                if not self._server_up:
                    continue
                await asyncio.sleep(RETRY_DELAY_SEC)
        except KeyboardInterrupt:
            logger.info("🛑 Stopped by user")
        except asyncio.CancelledError:
            raise
        finally:
            self.running = False
            logger.info("✅ LiveSenderNoTurn stopped (sessions: %d)", self._session_count)


async def main():
    parser = argparse.ArgumentParser(description="WebRTC sender to the EC2 relay (no TURN)")
    parser.add_argument("--id", default=None,
                        help="Camera id (default: camera_uuid from camera.ini)")
    parser.add_argument("--server", default=DEFAULT_RELAY_URL, help="Relay URL")
    args = parser.parse_args()

    client_id = args.id or camera_uuid_from_ini()
    if not client_id:
        logger.error("❌ No camera id: pass --id or set [server] camera_uuid in %s", CAMERA_INI)
        sys.exit(1)

    await LiveSenderNoTurn(client_id, args.server).start()


if __name__ == "__main__":
    asyncio.run(main())
