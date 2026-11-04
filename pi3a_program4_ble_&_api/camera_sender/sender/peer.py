"""WebRTC peer connection management for sender."""
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, VideoStreamTrack
import logging
from typing import Optional, Callable, Awaitable
import asyncio


logger = logging.getLogger(__name__)


class PeerConnection:
    """Manages WebRTC peer connection for sender."""
    
    def __init__(self, stun_server: str, turn_servers: Optional[list] = None):
        """
        Initialize peer connection.
        
        Args:
            stun_server: STUN server URL
            turn_servers: List of TURN server configs
        """
        self.stun_server = stun_server
        self.turn_servers = turn_servers or []
        self.pc: Optional[RTCPeerConnection] = None
        self.video_track: Optional[VideoStreamTrack] = None
        self.on_offer: Optional[Callable[[str], Awaitable[None]]] = None
        self.on_icecandidate: Optional[Callable[[object], Awaitable[None]]] = None
        
        # Create ICE servers configuration
        ice_servers = [RTCIceServer([self.stun_server])]
        for turn_server in self.turn_servers:
            ice_servers.append(RTCIceServer(
                urls=[turn_server["urls"]],
                username=turn_server.get("username"),
                credential=turn_server.get("credential")
            ))
        
        self.config = RTCConfiguration(iceServers=ice_servers)
    
    async def initialize(self, video_track: VideoStreamTrack) -> None:
        """
        Initialize peer connection.
        
        Args:
            video_track: Video stream track to send
        """
        logger.info("Initializing peer connection")
        
        # Create peer connection with configuration
        self.pc = RTCPeerConnection(configuration=self.config)
        self.video_track = video_track
        
        # Add video track
        self.pc.addTrack(video_track)
        logger.info("Video track added to peer connection")
        
        # Set up event handlers
        @self.pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate:
                logger.debug(f"ICE candidate: {candidate.candidate[:50]}")
                if self.on_icecandidate:
                    await self.on_icecandidate(candidate)
        
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"Connection state: {self.pc.connectionState}")
        
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info(f"ICE connection state: {self.pc.iceConnectionState}")
        
        @self.pc.on("signalingstatechange")
        async def on_signalingstatechange():
            logger.debug(f"Signaling state: {self.pc.signalingState}")
    
    async def create_offer(self) -> str:
        """
        Create WebRTC offer.
        
        Returns:
            SDP offer string
        """
        if not self.pc:
            raise RuntimeError("Peer connection not initialized")
        
        logger.info("Creating WebRTC offer")
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        
        logger.info("Offer created, local description set")
        return self.pc.localDescription.sdp
    
    async def set_remote_description(self, sdp: str, description_type: str = "answer") -> None:
        """
        Set remote description (answer).
        
        Args:
            sdp: SDP string
            description_type: Type of description (answer or offer)
        """
        if not self.pc:
            raise RuntimeError("Peer connection not initialized")
        
        logger.info(f"Setting remote {description_type} description")
        
        from aiortc import RTCSessionDescription
        
        if description_type == "answer":
            description = RTCSessionDescription(sdp=sdp, type="answer")
        elif description_type == "offer":
            description = RTCSessionDescription(sdp=sdp, type="offer")
        else:
            raise ValueError(f"Unknown description type: {description_type}")
        
        try:
            await self.pc.setRemoteDescription(description)
            logger.info(f"Remote {description_type} description set")
        except Exception as e:
            logger.error(f"Error setting remote description: {e}")
            raise
    
    async def add_ice_candidate(self, candidate: str, sdp_mid: Optional[str] = None,
                               sdp_mline_index: Optional[int] = None) -> None:
        """
        Add ICE candidate.
        
        Args:
            candidate: Candidate string
            sdp_mid: SDP media line ID
            sdp_mline_index: SDP media line index
        """
        if not self.pc:
            logger.warning("Peer connection not initialized, ignoring ICE candidate")
            return
        
        try:
            from aiortc import RTCIceCandidate
            ice_candidate = RTCIceCandidate(
                candidate=candidate,
                sdpMid=sdp_mid,
                sdpMLineIndex=sdp_mline_index
            )
            await self.pc.addIceCandidate(ice_candidate)
            logger.debug("ICE candidate added")
        except Exception as e:
            logger.error(f"Error adding ICE candidate: {e}")
    
    async def close(self) -> None:
        """Close peer connection."""
        if self.pc:
            await self.pc.close()
            logger.info("Peer connection closed")
            self.pc = None
    
    @property
    def connection_state(self) -> Optional[str]:
        """Get connection state."""
        return self.pc.connectionState if self.pc else None
    
    @property
    def ice_connection_state(self) -> Optional[str]:
        """Get ICE connection state."""
        return self.pc.iceConnectionState if self.pc else None
    
    @property
    def signaling_state(self) -> Optional[str]:
        """Get signaling state."""
        return self.pc.signalingState if self.pc else None
