import os
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from config import get_settings

logger = logging.getLogger(__name__)

COLOMBIA_TZ = ZoneInfo("America/Bogota")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Business hours (local Colombia time)
BUSINESS_HOURS_START = 8   # 8:00 AM
BUSINESS_HOURS_END = 19    # 7:00 PM
SLOT_DURATION_MINUTES = 40  # 30 min session + 10 min buffer
BUSINESS_DAYS = {0, 1, 2, 3, 4, 5}  # Monday=0 to Saturday=5 (Sunday=6 excluded)


def _get_credentials() -> Credentials | None:
    import base64, json as _json
    settings = get_settings()
    token_path = os.path.join(settings.credentials_dir, "token.json")
    creds = None

    # Priority 1: GOOGLE_TOKEN_JSON env var (base64-encoded) — used in production
    token_b64 = os.environ.get("GOOGLE_TOKEN_JSON")
    if token_b64:
        try:
            token_data = base64.b64decode(token_b64).decode()
            creds = Credentials.from_authorized_user_info(
                _json.loads(token_data), SCOPES
            )
        except Exception as e:
            logger.error(f"Error loading token from env var: {e}")

    # Priority 2: token.json file — used in local development
    if not creds and os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            logger.error(f"Error refreshing Google credentials: {e}")
            return None

    return creds if (creds and creds.valid) else None


def _get_service():
    creds = _get_credentials()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def get_available_slots(days_ahead: int = 7) -> list[datetime]:
    """
    Returns a list of available 30-min appointment slots for the next `days_ahead` days.
    Skips slots that overlap with existing calendar events.
    """
    service = _get_service()
    if not service:
        logger.warning("Google Calendar not configured — returning empty slots")
        return []

    settings = get_settings()
    now = datetime.now(COLOMBIA_TZ)
    end_range = now + timedelta(days=days_ahead)

    # Fetch busy times from Google Calendar
    try:
        body = {
            "timeMin": now.isoformat(),
            "timeMax": end_range.isoformat(),
            "items": [{"id": settings.google_calendar_id}],
        }
        freebusy = service.freebusy().query(body=body).execute()
        busy_periods = freebusy["calendars"][settings.google_calendar_id]["busy"]
    except Exception as e:
        logger.error(f"Error fetching freebusy: {e}")
        return []

    # Build busy intervals as datetime tuples
    busy_intervals = []
    for period in busy_periods:
        start = datetime.fromisoformat(period["start"]).astimezone(COLOMBIA_TZ)
        end = datetime.fromisoformat(period["end"]).astimezone(COLOMBIA_TZ)
        busy_intervals.append((start, end))

    # Generate all potential slots
    available = []
    # Start from today at business open — per-slot check handles filtering past/too-soon slots
    current_day = now.replace(hour=BUSINESS_HOURS_START, minute=0, second=0, microsecond=0)

    while current_day < end_range and len(available) < 12:
        if current_day.weekday() in BUSINESS_DAYS:
            slot = current_day
            while slot.hour < BUSINESS_HOURS_END and len(available) < 12:
                slot_end = slot + timedelta(minutes=SLOT_DURATION_MINUTES)
                # Check if slot is in the future (at least 2 hours from now)
                if slot > now + timedelta(hours=2):
                    if not _overlaps_busy(slot, slot_end, busy_intervals):
                        available.append(slot)
                slot = slot_end
        current_day = (current_day + timedelta(days=1)).replace(
            hour=BUSINESS_HOURS_START, minute=0, second=0, microsecond=0
        )

    return available


def _overlaps_busy(
    slot_start: datetime,
    slot_end: datetime,
    busy_intervals: list[tuple],
) -> bool:
    for busy_start, busy_end in busy_intervals:
        if slot_start < busy_end and slot_end > busy_start:
            return True
    return False


def format_slots_for_whatsapp(slots: list[datetime]) -> str:
    """Format available slots as a numbered list for WhatsApp."""
    if not slots:
        return "No hay horarios disponibles en este momento."

    days_es = {
        0: "Lunes", 1: "Martes", 2: "Miercoles", 3: "Jueves",
        4: "Viernes", 5: "Sabado", 6: "Domingo",
    }
    months_es = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
    }

    lines = []
    for i, slot in enumerate(slots, 1):
        day_name = days_es[slot.weekday()]
        month_name = months_es[slot.month]
        time_str = slot.strftime("%I:%M %p").lstrip("0")
        lines.append(f"{i}. {day_name} {slot.day} de {month_name} a las {time_str}")

    return "\n".join(lines)


async def create_appointment(
    slot: datetime,
    user_name: str,
    user_phone: str,
    user_email: str = "",
) -> dict | None:
    """Create a calendar event for the valoracion appointment."""
    service = _get_service()
    if not service:
        logger.warning("Google Calendar not configured — cannot create appointment")
        return None

    settings = get_settings()
    event_end = slot + timedelta(minutes=30)

    attendees = []
    if user_email:
        attendees.append({"email": user_email})

    event_body = {
        "summary": f"Valoracion - {user_name}",
        "description": (
            f"Cliente agendado via WhatsApp Bot\n"
            f"Nombre: {user_name}\n"
            f"Telefono: {user_phone}\n"
        ),
        "start": {
            "dateTime": slot.isoformat(),
            "timeZone": "America/Bogota",
        },
        "end": {
            "dateTime": event_end.isoformat(),
            "timeZone": "America/Bogota",
        },
        "attendees": attendees,
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 30},
            ],
        },
    }

    try:
        event = (
            service.events()
            .insert(calendarId=settings.google_calendar_id, body=event_body)
            .execute()
        )
        logger.info(f"Calendar event created: {event.get('id')}")
        return event
    except Exception as e:
        logger.error(f"Error creating calendar event: {e}")
        return None
