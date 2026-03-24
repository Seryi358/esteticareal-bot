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

HEADERS = ["Teléfono", "Nombre", "Ciudad", "Servicio", "Estado", "Cita", "Fecha actualización"]


def _get_service():
    creds = _get_credentials()
    if not creds:
        return None
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _ensure_headers(service, sheet_id: str) -> None:
    """Create headers if the sheet is empty."""
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="A1:G1"
        ).execute()
        values = result.get("values", [])
        if not values or values[0] != HEADERS:
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range="A1:G1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()
            logger.info("[Sheets] Headers created")
    except Exception as e:
        logger.error(f"[Sheets] Error creating headers: {e}")


def _sync(phone: str, nombre: str | None, ciudad: str | None,
          servicio: str | None, is_booked: bool, appointment_dt: str | None) -> None:
    """Upsert a row in the Google Sheet."""
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

    # Ensure headers exist
    _ensure_headers(service, sheet_id)

    status = "Cita agendada" if is_booked else "En conversación"
    cita = appointment_dt or ""

    new_row = [
        phone,
        nombre or "",
        ciudad or "",
        servicio or "",
        status,
        cita,
        now,
    ]

    try:
        # Read column A to find if phone already exists
        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="A:A"
        ).execute()
        values = result.get("values", [])

        row_index = None
        for i, row in enumerate(values):
            if row and row[0] == phone:
                row_index = i + 1  # 1-based
                break

        if row_index:
            range_str = f"A{row_index}:G{row_index}"
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=range_str,
                valueInputOption="RAW",
                body={"values": [new_row]},
            ).execute()
            logger.info(f"[Sheets] Updated row {row_index} for {phone}")
        else:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="A:G",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [new_row]},
            ).execute()
            logger.info(f"[Sheets] Appended new row for {phone}")

    except Exception as e:
        logger.error(f"[Sheets] Error syncing {phone}: {e}")


async def sync_conversation(
    phone: str,
    nombre: str | None = None,
    ciudad: str | None = None,
    servicio: str | None = None,
    is_booked: bool = False,
    appointment_dt: str | None = None,
) -> None:
    """Async wrapper — runs the blocking Sheets API call in a thread."""
    await asyncio.get_event_loop().run_in_executor(
        None, partial(_sync, phone, nombre, ciudad, servicio, is_booked, appointment_dt)
    )
