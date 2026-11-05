"""Message protocol definitions for WebRTC MVP."""
from dataclasses import dataclass, asdict
from typing import Optional, Any, Dict
from enum import Enum
import json


class MessageType(str, Enum):
    """Message types for signaling protocol."""
    REGISTER = "register"
    WATCH = "watch"
    OFFER = "offer"
    ANSWER = "answer"
    ICE_CANDIDATE = "ice_candidate"
    DISCONNECT = "disconnect"
    ERROR = "error"


@dataclass
class Message:
    """Base message class."""
    type: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert message to dictionary."""
        return asdict(self)
    
    def to_json(self) -> str:
        """Convert message to JSON string."""
        return json.dumps(self.to_dict())
    
    @staticmethod
    def from_json(data: str) -> 'Message':
        """Parse message from JSON string."""
        obj = json.loads(data)
        msg_type = obj.get("type")
        
        if msg_type == MessageType.REGISTER:
            return RegisterMessage(client=obj["client"])
        elif msg_type == MessageType.WATCH:
            return WatchMessage(target=obj["target"])
        elif msg_type == MessageType.OFFER:
            return OfferMessage(
                from_client=obj["from_client"],
                to_client=obj["to_client"],
                sdp=obj["sdp"]
            )
        elif msg_type == MessageType.ANSWER:
            return AnswerMessage(
                from_client=obj["from_client"],
                to_client=obj["to_client"],
                sdp=obj["sdp"]
            )
        elif msg_type == MessageType.ICE_CANDIDATE:
            return IceCandidateMessage(
                from_client=obj["from_client"],
                to_client=obj["to_client"],
                candidate=obj["candidate"],
                sdp_mid=obj.get("sdp_mid"),
                sdp_mline_index=obj.get("sdp_mline_index")
            )
        elif msg_type == MessageType.DISCONNECT:
            return DisconnectMessage(client=obj["client"])
        elif msg_type == MessageType.ERROR:
            return ErrorMessage(error=obj["error"])
        else:
            raise ValueError(f"Unknown message type: {msg_type}")


@dataclass
class RegisterMessage(Message):
    """Register a client with the signaling server."""
    client: str
    
    def __init__(self, client: str):
        super().__init__(type=MessageType.REGISTER)
        self.client = client


@dataclass
class WatchMessage(Message):
    """Request to watch a specific client."""
    target: str
    
    def __init__(self, target: str):
        super().__init__(type=MessageType.WATCH)
        self.target = target


@dataclass
class OfferMessage(Message):
    """WebRTC Offer message."""
    from_client: str
    to_client: str
    sdp: str
    
    def __init__(self, from_client: str, to_client: str, sdp: str):
        super().__init__(type=MessageType.OFFER)
        self.from_client = from_client
        self.to_client = to_client
        self.sdp = sdp


@dataclass
class AnswerMessage(Message):
    """WebRTC Answer message."""
    from_client: str
    to_client: str
    sdp: str
    
    def __init__(self, from_client: str, to_client: str, sdp: str):
        super().__init__(type=MessageType.ANSWER)
        self.from_client = from_client
        self.to_client = to_client
        self.sdp = sdp


@dataclass
class IceCandidateMessage(Message):
    """WebRTC ICE Candidate message."""
    from_client: str
    to_client: str
    candidate: str
    sdp_mid: Optional[str] = None
    sdp_mline_index: Optional[int] = None
    
    def __init__(self, from_client: str, to_client: str, candidate: str,
                 sdp_mid: Optional[str] = None, sdp_mline_index: Optional[int] = None):
        super().__init__(type=MessageType.ICE_CANDIDATE)
        self.from_client = from_client
        self.to_client = to_client
        self.candidate = candidate
        self.sdp_mid = sdp_mid
        self.sdp_mline_index = sdp_mline_index


@dataclass
class DisconnectMessage(Message):
    """Client disconnect notification."""
    client: str
    
    def __init__(self, client: str):
        super().__init__(type=MessageType.DISCONNECT)
        self.client = client


@dataclass
class ErrorMessage(Message):
    """Error message."""
    error: str
    
    def __init__(self, error: str):
        super().__init__(type=MessageType.ERROR)
        self.error = error
