"""
livesender.py — WebRTC live video sender (video only)
======================================================
Streams live video from camera to receiver via WebRTC P2P.
Reads frames from /tmp/frames/ written by filesender.py.
No audio — clean, low latency video only.

Retries indefinitely if receiver disconnects.
Press Ctrl+C to stop.

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

RETRY_DELAY_SEC = 5


class LiveSender:
    """WebRTC video sender — retries indefinitely."""

    def __init__(self, client_id: str, server_url: str):
        self.client_id      = client_id
        self.server_url     = server_url.replace("ws://","http://").replace("/ws","")
        self.running        = True
        self._session_count = 0

    async def _fetch_answer(self) -> Optional[str]:
        """
        Poll for answer — restarts session every 5 minutes if no answer.
        Prevents stale peer connections after long idle periods.
        """
        timeout = aiohttp.ClientTimeout(total=5)
        attempt = 0
        session_timeout = 300  # restart after 5 minutes — fresh peer connection
        while self.running:
            await asyncio.sleep(1)
            # Restart with fresh peer connection after timeout
            if attempt >= session_timeout:
                logger.info(
                    "⟳ No receiver after %ds — restarting with fresh connection",
                    session_timeout)
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
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.debug("Answer poll: %s", e)
            attempt += 1
            if attempt % 30 == 0:
                remaining = session_timeout - attempt
                logger.info(
                    "⏳ Waiting for receiver... (%ds elapsed, restart in %ds)",
                    attempt, remaining)
        return None

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

    async def _run_session(self) -> bool:
        """Run one WebRTC session. Returns True if ran, False if setup failed."""
        self._session_count += 1
        logger.info("📡 Session %d | server: %s", self._session_count, self.server_url)

        peer_connection = None
        camera_mgr      = None

        try:
            # Clear stale entries
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.server_url}/signal/clear/{self.client_id}"
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.info("🧹 Cleared %s stale entries",
                                        data.get("cleared", 0))
            except aiohttp.ClientError as e:
                logger.debug("Cleanup failed: %s", e)

            # Get video track from /tmp/frames/
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

            logger.info("📡 Offer sent — waiting for receiver...")
            logger.info("💡 Start: python3 receiver.py --watch %s", self.client_id)

            # Wait for answer — indefinitely
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
            import traceback; traceback.print_exc()
            return False
        finally:
            if peer_connection:
                await peer_connection.close()
            if camera_mgr:
                camera_mgr.close()

    async def start(self):
        logger.info("🚀 LiveSender | id=%s", self.client_id)
        logger.info("🔄 Retries automatically on disconnect")
        logger.info("🛑 Press Ctrl+C to stop")

        try:
            while self.running:
                success = await self._run_session()
                if not self.running:
                    break
                delay = RETRY_DELAY_SEC
                logger.info("⏳ Retrying in %ds...", delay)
                await asyncio.sleep(delay)

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
