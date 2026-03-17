import httpx
import logging
from urllib.parse import quote
from config import get_settings

logger = logging.getLogger(__name__)

# Track IDs of messages sent BY the bot so we can distinguish them
# from messages Yesica types manually (both have fromMe=true)
_bot_sent_ids: set[str] = set()
_MAX_SENT_IDS = 5000  # prevent unbounded growth


def _headers() -> dict:
    return {
        "apikey": get_settings().evolution_api_key,
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return get_settings().evolution_api_url.rstrip("/")


def _instance() -> str:
    # URL-encode so instance names with spaces work (e.g. "Estetica Real")
    return quote(get_settings().evolution_instance, safe="")


async def send_text_message(phone: str, text: str) -> bool:
    """Send a WhatsApp text message via Evolution API. Tracks the sent message ID."""
    url = f"{_base_url()}/message/sendText/{_instance()}"
    payload = {
        "number": phone,
        "text": text,
        "delay": 1200,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=_headers())
            response.raise_for_status()
            data = response.json()
            # Store the message ID so we can identify bot-sent messages in webhooks
            msg_id = data.get("key", {}).get("id")
            if msg_id:
                _bot_sent_ids.add(msg_id)
                if len(_bot_sent_ids) > _MAX_SENT_IDS:
                    _bot_sent_ids.clear()
            return True
    except Exception as e:
        logger.error(f"Error sending message to {phone}: {e}")
        return False


def is_bot_sent_message(message_id: str) -> bool:
    """Returns True if this message ID was sent by the bot (not typed by Yesica)."""
    return message_id in _bot_sent_ids


async def send_typing_presence(phone: str) -> None:
    """Send 'typing...' presence indicator for a more human feel."""
    url = f"{_base_url()}/chat/sendPresence/{_instance()}"
    payload = {
        "number": phone,
        "presence": "composing",
        "delay": 3000,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload, headers=_headers())
    except Exception:
        pass


async def get_media_base64(message_key_id: str, phone: str | None = None) -> str | None:
    """Download and return base64 of a media message."""
    url = f"{_base_url()}/chat/getBase64FromMediaMessage/{_instance()}"
    # Build full message key — Evolution API often needs remoteJid to locate the message
    key_obj: dict = {"id": message_key_id}
    if phone:
        key_obj["remoteJid"] = f"{phone}@s.whatsapp.net"
        key_obj["fromMe"] = False
    payload = {"message": {"key": key_obj}}
    logger.info(f"Downloading media {message_key_id} for phone={phone}")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload, headers=_headers())
            response.raise_for_status()
            data = response.json()
            b64 = data.get("base64")
            if b64:
                logger.info(f"Media downloaded: {len(b64)} chars base64")
            else:
                logger.warning(f"Media response had no base64 field. Keys: {list(data.keys())}")
            return b64
    except Exception as e:
        logger.error(f"Error downloading media {message_key_id}: {e}")
        return None


def extract_phone(remote_jid: str) -> str:
    """Extract plain phone number from Evolution API remoteJid format."""
    return remote_jid.split("@")[0]


def is_group_message(remote_jid: str) -> bool:
    """Returns True if the message is from a group chat."""
    return "@g.us" in remote_jid
