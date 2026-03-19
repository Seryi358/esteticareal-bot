import asyncio
import logging
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from config import get_settings
from services.calendar import _get_credentials

logger = logging.getLogger(__name__)

COLOMBIA_TZ = ZoneInfo("America/Bogota")

# Column order: A=phone, B=nombre, C=pago_valoracion, D=updated_at
HEADERS = ["phone", "nombre", "pago_valoracion", "updated_at"]


def _get_service():
    creds = _get_credentials()
    if not creds:
        return None
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _sync(phone: str, nombre: str | None, pago_valoracion: bool) -> None:
    """Upsert a row in the Google Sheet (runs in thread via asyncio)."""
    settings = get_settings()
    if not settings.google_sheet_id:
        logger.warning("GOOGLE_SHEET_ID not configured — skipping Sheets sync")
        return

    service = _get_service()
    if not service:
        logger.warning("Google credentials not available — skipping Sheets sync")
        return

    sheet_id = settings.google_sheet_id
    now = datetime.now(COLOMBIA_TZ).strftime("%Y-%m-%d %H:%M")
    new_row = [phone, nombre or "", "Sí" if pago_valoracion else "No", now]

    try:
        # Read existing data to find if phone already exists
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="A:A"
        ).execute()
        values = result.get("values", [])

        # Find row index (1-based, row 1 = headers)
        row_index = None
        for i, row in enumerate(values):
            if row and row[0] == phone:
                row_index = i + 1  # 1-based
                break

        if row_index:
            # Update existing row
            range_str = f"A{row_index}:D{row_index}"
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=range_str,
                valueInputOption="RAW",
                body={"values": [new_row]},
            ).execute()
            logger.info(f"[Sheets] Updated row {row_index} for {phone}")
        else:
            # Append new row
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="A:D",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [new_row]},
            ).execute()
            logger.info(f"[Sheets] Appended new row for {phone}")

    except Exception as e:
        logger.error(f"[Sheets] Error syncing {phone}: {e}")


async def sync_conversation(phone: str, nombre: str | None, pago_valoracion: bool) -> None:
    """Async wrapper — runs the blocking Sheets API call in a thread."""
    await asyncio.get_event_loop().run_in_executor(
        None, partial(_sync, phone, nombre, pago_valoracion)
    )
