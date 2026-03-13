import httpx
import logging
from urllib.parse import quote
from config import get_settings

logger = logging.getLogger(__name__)


def _headers() -> dict:
    return {
        "apikey": get_settings().evolution_api_key,
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    s = get_settings()
    return s.evolution_api_url.rstrip("/")


def _instance() -> str:
    # URL-encode so instance names with spaces work (e.g. "Estetica Real")
    return quote(get_settings().evolution_instance, safe="")


async def send_text_message(phone: str, text: str) -> bool:
    """Send a WhatsApp text message via Evolution API."""
    url = f"{_base_url()}/message/sendText/{_instance()}"
    payload = {
        "number": phone,
        "text": text,
        "delay": 1200,  # typing simulation delay in ms
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=_headers())
            response.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Error sending message to {phone}: {e}")
        return False


async def send_typing_presence(phone: str) -> None:
    """Send 'typing...' presence indicator for a more human feel."""
    url = f"{_base_url()}/chat/sendPresence/{_instance()}"
    payload = {
        "number": phone,
        "options": {"presence": "composing", "delay": 3000},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload, headers=_headers())
    except Exception:
        pass  # Non-critical, ignore errors


async def get_media_base64(message_key_id: str) -> str | None:
    """Download and return base64 of a media message (e.g. payment screenshot)."""
    url = f"{_base_url()}/chat/getBase64FromMediaMessage/{_instance()}"
    payload = {"message": {"key": {"id": message_key_id}}}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload, headers=_headers())
            response.raise_for_status()
            data = response.json()
            return data.get("base64")
    except Exception as e:
        logger.error(f"Error downloading media {message_key_id}: {e}")
        return None


def extract_phone(remote_jid: str) -> str:
    """Extract plain phone number from Evolution API remoteJid format."""
    return remote_jid.split("@")[0]


def is_group_message(remote_jid: str) -> bool:
    """Returns True if the message is from a group chat."""
    return "@g.us" in remote_jid
