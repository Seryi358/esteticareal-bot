import asyncio
import os
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from config import get_settings

logger = logging.getLogger(__name__)

COLOMBIA_TZ = ZoneInfo("America/Bogota")
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Business hours (local Colombia time)
BUSINESS_HOURS_START = 9   # 9:00 AM
BUSINESS_HOURS_END = 17    # 5:00 PM
SLOT_DURATION_MINUTES = 30  # 30 min slots (2:00, 2:30, 3:00, etc.)
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

    # Fetch actual events from Google Calendar (more reliable than freebusy)
    try:
        cal_id = settings.google_calendar_id
        loop = asyncio.get_event_loop()

        def _fetch_events():
            events_result = service.events().list(
                calendarId=cal_id,
                timeMin=now.isoformat(),
                timeMax=end_range.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            return events_result.get("items", [])

        events = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_events),
            timeout=15,
        )
        logger.info(f"Calendar events found: {len(events)} in range")
        for ev in events:
            ev_start = ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", "?"))
            logger.info(f"  Event: '{ev.get('summary', 'Sin titulo')}' @ {ev_start}")
    except asyncio.TimeoutError:
        logger.error("Timeout fetching events from Google Calendar")
        return []
    except Exception as e:
        logger.error(f"Error fetching calendar events: {e}")
        return []

    # Build busy intervals from events
    busy_intervals = []
    for ev in events:
        ev_start = ev.get("start", {})
        ev_end = ev.get("end", {})
        # Skip all-day events (they have "date" instead of "dateTime")
        if "dateTime" not in ev_start:
            continue
        start = datetime.fromisoformat(ev_start["dateTime"]).astimezone(COLOMBIA_TZ)
        end = datetime.fromisoformat(ev_end["dateTime"]).astimezone(COLOMBIA_TZ)
        busy_intervals.append((start, end))

    # Generate all potential slots
    available = []
    # Start from today at business open — per-slot check handles filtering past/too-soon slots
    current_day = now.replace(hour=BUSINESS_HOURS_START, minute=0, second=0, microsecond=0)

    while current_day < end_range:
        if current_day.weekday() in BUSINESS_DAYS:
            slot = current_day
            while slot.hour < BUSINESS_HOURS_END:
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


def group_slots_into_ranges(slots: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Group consecutive slots into (range_start, range_end) tuples."""
    if not slots:
        return []

    ranges = []
    range_start = slots[0]
    prev = slots[0]

    for slot in slots[1:]:
        # Consecutive if same day and starts right after previous slot ends
        same_day = slot.date() == prev.date()
        consecutive = slot == prev + timedelta(minutes=SLOT_DURATION_MINUTES)
        if same_day and consecutive:
            prev = slot
        else:
            # Close current range — end is when the last slot's session finishes
            ranges.append((range_start, prev + timedelta(minutes=SLOT_DURATION_MINUTES)))
            range_start = slot
            prev = slot

    # Close last range
    ranges.append((range_start, prev + timedelta(minutes=SLOT_DURATION_MINUTES)))
    return ranges


def _format_hour(dt: datetime) -> str:
    """Format hour simply: '9am', '2pm', '10:30am'."""
    minute = dt.strftime("%M")
    hour = dt.hour
    period = "am" if hour < 12 else "pm"
    if hour == 0:
        h = 12
    elif hour > 12:
        h = hour - 12
    else:
        h = hour
    if minute == "00":
        return f"{h}{period}"
    return f"{h}:{minute}{period}"


def format_slots_for_whatsapp(slots: list[datetime]) -> str:
    """Format available slots as a single natural sentence with max 2 time ranges."""
    ranges = group_slots_into_ranges(slots)
    if not ranges:
        return "No hay horarios disponibles en este momento."

    days_es = {
        0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves",
        4: "viernes", 5: "sabado", 6: "domingo",
    }

    # Limit to 2 ranges max — keep it ultra simple
    ranges = ranges[:2]

    now = datetime.now(COLOMBIA_TZ)
    parts = []
    for start, end in ranges:
        start_str = _format_hour(start)
        end_str = _format_hour(end)

        if start.date() == now.date():
            label = "hoy"
        elif start.date() == (now + timedelta(days=1)).date():
            label = "mañana"
        else:
            label = f"el {days_es[start.weekday()]}"

        parts.append(f"{label} de {start_str} a {end_str}")

    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} y {parts[1]}"


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
        "summary": f"Valoración {user_name} {user_phone}",
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
        loop = asyncio.get_event_loop()
        event = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: service.events()
                .insert(calendarId=settings.google_calendar_id, body=event_body)
                .execute(),
            ),
            timeout=15,
        )
        logger.info(f"Calendar event created: {event.get('id')}")
        return event
    except asyncio.TimeoutError:
        logger.error("Timeout creating calendar event")
        return None
    except Exception as e:
        logger.error(f"Error creating calendar event: {e}")
        return None
