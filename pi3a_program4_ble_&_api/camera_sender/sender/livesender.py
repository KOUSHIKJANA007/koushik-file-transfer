"""
livesender.py — WebRTC live video sender (video only)
======================================================
Streams live video from camera to receiver via WebRTC P2P.
Reads frames from /tmp/frames/ written by filesender.py.
No audio — clean, low latency video only.

Designed for multi-camera deployments:
  - liveserver.py being DOWN is a normal condition — not an error
  - Retries silently every 30s when liveserver unreachable
  - Restarts session every 5 minutes to prevent stale peer connections
  - Never exits — only Ctrl+C stops it

Usage:
    cd ~/camera_sender/sender
    python3 livesender.py --id cam001 --server http://151.185.47.108:8080
"""

import asyncio
import logging
import sys
import os
import argparse
import aiohttp
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import config
from common.logger import setup_logger
from camera_raspi import CameraManager
from peer import PeerConnection

logger = setup_logger(__name__)

RETRY_DELAY_SEC    = 5    # wait between session attempts
SERVER_RETRY_SEC   = 30   # wait between liveserver reachability checks
SESSION_TIMEOUT    = 300  # restart session after 5 min if no receiver


class LiveSender:
    """
    WebRTC video sender — retries indefinitely.
    Treats liveserver being down as normal — waits silently.
    """

    def __init__(self, client_id: str, server_url: str):
        self.client_id      = client_id
        self.server_url     = server_url.replace("ws://","http://").replace("/ws","")
        self.running        = True
        self._session_count = 0
        self._server_up     = False

    # ── Liveserver health check ───────────────────────────────────────

    async def _is_server_up(self) -> bool:
        """Check if liveserver is reachable — silent, no error logs."""
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.server_url}/health"
                ) as response:
                    return response.status == 200
        except Exception:
            return False

    async def _wait_for_server(self):
        """
        Wait for liveserver to come up — silently.
        Logs only on state change (up→down, down→up).
        """
        attempt = 0
        while self.running:
            up = await self._is_server_up()
            if up and not self._server_up:
                logger.info("🟢 Liveserver online | %s", self.server_url)
                self._server_up = True
                return
            elif not up and self._server_up:
                logger.info("🔴 Liveserver offline | will keep retrying silently")
                self._server_up = False
            elif not up:
                attempt += 1
                # Log only every 10 minutes to avoid log spam
                if attempt == 1 or attempt % 20 == 0:
                    logger.info(
                        "⏸  Liveserver unreachable | retrying every %ds",
                        SERVER_RETRY_SEC)
            if up:
                return
            await asyncio.sleep(SERVER_RETRY_SEC)

    # ── Answer polling ────────────────────────────────────────────────

    async def _fetch_answer(self) -> Optional[str]:
        """
        Poll for answer — restarts session every 5 minutes if no answer.
        If liveserver goes down mid-wait — detects it and returns None
        so session restarts cleanly.
        """
        timeout = aiohttp.ClientTimeout(total=5)
        attempt = 0
        consecutive_failures = 0

        while self.running:
            await asyncio.sleep(1)

            # Session timeout — restart with fresh peer connection
            if attempt >= SESSION_TIMEOUT:
                logger.info(
                    "⟳ No receiver after %ds — restarting session",
                    SESSION_TIMEOUT)
                return None

            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        f"{self.server_url}/signal/{self.client_id}/answer"
                    ) as response:
                        if response.status == 200:
                            answer_sdp = (await response.json()).get("sdp")
                            if answer_sdp:
                                logger.info("✅ Receiver connected")
                                return answer_sdp
                        consecutive_failures = 0  # server responded, just no answer yet

            except (aiohttp.ClientError, asyncio.TimeoutError):
                consecutive_failures += 1
                # After 3 consecutive failures — liveserver likely down
                if consecutive_failures >= 3:
                    logger.info(
                        "🔴 Liveserver went offline mid-session — restarting")
                    self._server_up = False
                    return None

            attempt += 1
            if attempt % 30 == 0:
                remaining = SESSION_TIMEOUT - attempt
                logger.info(
                    "⏳ Waiting for receiver... (%ds elapsed, restart in %ds)",
                    attempt, remaining)
        return None

    # ── ICE wait ─────────────────────────────────────────────────────

    async def _wait_for_ice(self, pc: PeerConnection) -> bool:
        for _ in range(config.ICE_TIMEOUT):
            state = pc.ice_connection_state if pc else "closed"
            if state in {"connected","completed"}:
                return True
            if state in {"failed","closed"}:
                logger.error("❌ ICE failed: %s", state)
                return False
            await asyncio.sleep(1)
        logger.error("❌ ICE timeout")
        return False

    # ── One session ───────────────────────────────────────────────────

    async def _run_session(self) -> bool:
        """Run one WebRTC session. Returns True if ran, False if setup failed."""
        self._session_count += 1
        logger.info("📡 Session %d | %s", self._session_count, self.server_url)

        peer_connection = None
        camera_mgr      = None

        try:
            # Clear stale signaling entries
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.server_url}/signal/clear/{self.client_id}"
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info("🧹 Cleared %s stale entries",
                                        data.get("cleared", 0))
            except Exception as e:
                logger.debug("Cleanup failed: %s", e)

            # Get video track
            camera_mgr  = CameraManager(
                config.VIDEO_WIDTH, config.VIDEO_HEIGHT, config.VIDEO_FPS)
            video_track = camera_mgr.create_track()

            if not video_track:
                logger.error("❌ No video track — is filesender.py running?")
                return False

            # TURN servers
            turn_servers = []
            if config.TURN_SERVER:
                turn_servers.append({
                    "urls":       config.TURN_SERVER,
                    "username":   config.TURN_USERNAME,
                    "credential": config.TURN_PASSWORD,
                })

            # Create peer connection
            peer_connection = PeerConnection(config.STUN_SERVER, turn_servers)
            await peer_connection.initialize(video_track)

            # Send offer
            offer_sdp = await peer_connection.create_offer()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.server_url}/signal",
                        json={
                            "type":      "offer",
                            "client_id": self.client_id,
                            "sdp":       offer_sdp
                        }
                    ) as response:
                        if response.status != 200:
                            logger.error("❌ Offer failed: %s", response.status)
                            return False
            except Exception as e:
                logger.info("🔴 Liveserver unreachable when sending offer: %s", e)
                self._server_up = False
                return False

            logger.info("📡 Offer sent — waiting for receiver...")

            # Wait for answer
            answer_sdp = await self._fetch_answer()
            if not answer_sdp or not self.running:
                return False

            await peer_connection.set_remote_description(answer_sdp, "answer")

            if not await self._wait_for_ice(peer_connection):
                return False

            logger.info("📺 P2P established — streaming video")

            # Stream until connection drops
            while self.running:
                state = peer_connection.ice_connection_state \
                    if peer_connection else "closed"
                if state in {"failed","closed","disconnected"}:
                    logger.warning("⚡ P2P lost (ICE: %s)", state)
                    break
                await asyncio.sleep(1)

            logger.info("📴 Session %d ended", self._session_count)
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
        logger.info("🚀 LiveSender | id=%s", self.client_id)
        logger.info("🔄 Retries automatically | liveserver down = normal")
        logger.info("🛑 Press Ctrl+C to stop")

        try:
            while self.running:
                # Wait for liveserver to be reachable before attempting session
                await self._wait_for_server()

                if not self.running:
                    break

                success = await self._run_session()

                if not self.running:
                    break

                if not self._server_up:
                    # Server went down — wait for it to come back
                    continue

                # Normal session end — brief pause before retry
                await asyncio.sleep(RETRY_DELAY_SEC)

        except KeyboardInterrupt:
            logger.info("🛑 Stopped by user")
        except asyncio.CancelledError:
            raise
        finally:
            self.running = False
            logger.info("✅ LiveSender stopped (sessions: %d)",
                        self._session_count)


async def main():
    parser = argparse.ArgumentParser(description="WebRTC Live Video Sender")
    parser.add_argument("--id",     default="cam001", help="Camera ID")
    parser.add_argument("--server", default=config.get_signaling_url(),
                        help="Signaling server URL")
    args = parser.parse_args()

    sender = LiveSender(args.id, args.server)
    await sender.start()


if __name__ == "__main__":
    asyncio.run(main())
