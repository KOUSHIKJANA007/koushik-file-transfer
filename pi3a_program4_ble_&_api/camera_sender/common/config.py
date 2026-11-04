"""Configuration management for WebRTC MVP."""
import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

# Load .env file
load_dotenv()


def _safe_int(env_var: str, default: int, min_val: int = 1, max_val: int = 3600) -> int:
    """Return a bounded integer environment setting, or its safe default."""
    try:
        value = int(os.getenv(env_var, str(default)))
    except (TypeError, ValueError):
        return default
    return value if min_val <= value <= max_val else default


@dataclass
class Config:
    """Application configuration."""
    
    # Signaling Server
    SIGNALING_HOST: str = os.getenv("SIGNALING_HOST", "localhost")
    SIGNALING_PORT: int = int(os.getenv("SIGNALING_PORT", "8000"))
    SIGNALING_URL: str = os.getenv("SIGNALING_URL", "ws://localhost:8000/ws")
    
    def get_signaling_url(self) -> str:
        """Get complete signaling server URL."""
        # Use SIGNALING_URL if set, otherwise construct from host/port
        if self.SIGNALING_URL and self.SIGNALING_URL != "ws://localhost:8000/ws":
            return self.SIGNALING_URL.replace('ws://', 'http://').replace('wss://', 'https://').replace('/ws', '')
        else:
            return f"http://{self.SIGNALING_HOST}:{self.SIGNALING_PORT}"
    
    # WebRTC
    STUN_SERVER: str = os.getenv("STUN_SERVER", "stun:stun.l.google.com:19302")
    TURN_SERVER: Optional[str] = os.getenv("TURN_SERVER", "turn:openrelay.metered.ca:80")
    TURN_USERNAME: Optional[str] = os.getenv("TURN_USERNAME", "openrelayproject")
    TURN_PASSWORD: Optional[str] = os.getenv("TURN_PASSWORD", "openrelayproject")
    
    # Video
    VIDEO_WIDTH: int = int(os.getenv("VIDEO_WIDTH", "640"))
    VIDEO_HEIGHT: int = int(os.getenv("VIDEO_HEIGHT", "480"))
    VIDEO_FPS: int = int(os.getenv("VIDEO_FPS", "30"))
    VIDEO_CODEC: str = os.getenv("VIDEO_CODEC", "h264")  # h264 or vp8
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "DEBUG")
    LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # Timeouts
    ICE_TIMEOUT: int = int(os.getenv("ICE_TIMEOUT", "30"))
    RECONNECT_INTERVAL: int = int(os.getenv("RECONNECT_INTERVAL", "5"))
    RECONNECT_MAX_ATTEMPTS: int = int(os.getenv("RECONNECT_MAX_ATTEMPTS", "0"))  # 0 = infinite
    KEEPALIVE_INTERVAL: int = field(
        default_factory=lambda: _safe_int("KEEPALIVE_INTERVAL", 10)
    )
    RECONNECT_GRACE_PERIOD: int = field(
        default_factory=lambda: _safe_int("RECONNECT_GRACE_PERIOD", 15)
    )
    
    # Client
    CLIENT_ID: Optional[str] = os.getenv("CLIENT_ID", None)
    
    def get_ice_servers(self) -> list:
        """Get ICE server configuration."""
        servers = [self.STUN_SERVER]
        if self.TURN_SERVER:
            servers.append({
                "urls": self.TURN_SERVER,
                "username": self.TURN_USERNAME,
                "password": self.TURN_PASSWORD
            })
        return servers


# Global config instance
config = Config()
