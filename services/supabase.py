import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from supabase import create_client, Client
from config import get_settings

logger = logging.getLogger(__name__)

COLOMBIA_TZ = ZoneInfo("America/Bogota")

_client: Client | None = None


def _get_client() -> Client | None:
    global _client
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_key:
        logger.warning("Supabase not configured — skipping DB sync")
        return None
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_key)
    return _client


def sync_conversation(phone: str, nombre: str | None, pago_valoracion: bool) -> None:
    """Upsert conversation summary to Supabase."""
    client = _get_client()
    if not client:
        return
    try:
        now = datetime.now(COLOMBIA_TZ).isoformat()
        client.table("conversaciones").upsert(
            {
                "phone": phone,
                "nombre": nombre,
                "pago_valoracion": pago_valoracion,
                "updated_at": now,
            },
            on_conflict="phone",
        ).execute()
        logger.info(f"[Supabase] Synced {phone}")
    except Exception as e:
        logger.error(f"[Supabase] Error syncing {phone}: {e}")
