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
BUSINESS_DAYS = {0, 1, 2, 3, 4}  # Monday=0 to Friday=4 (Saturday & Sunday excluded)

# ---------------------------------------------------------------------------
# Slot-level locks to prevent double-booking race conditions.
# Keyed by slot ISO string — ensures only one booking attempt per slot at a time.
# ---------------------------------------------------------------------------
_slot_locks: dict[str, asyncio.Lock] = {}
_slot_locks_meta_lock = asyncio.Lock()


async def _get_slot_lock(slot: datetime) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a specific time slot."""
    key = slot.astimezone(COLOMBIA_TZ).replace(second=0, microsecond=0).isoformat()
    async with _slot_locks_meta_lock:
        # Prune old locks (keys for past dates) to prevent memory leak
        if len(_slot_locks) > 200:
            now_key = datetime.now(COLOMBIA_TZ).isoformat()
            stale = [k for k in _slot_locks if k < now_key and not _slot_locks[k].locked()]
            for k in stale:
                del _slot_locks[k]
        if key not in _slot_locks:
            _slot_locks[key] = asyncio.Lock()
        return _slot_locks[key]


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
            # Persist refreshed token to file
            with open(token_path, "w") as f:
                f.write(creds.to_json())
            # Also update in-memory env var so subsequent calls in this process
            # don't start from the stale base64 token every time
            try:
                import base64 as _b64
                os.environ["GOOGLE_TOKEN_JSON"] = _b64.b64encode(
                    creds.to_json().encode()
                ).decode()
            except Exception:
                pass  # Non-critical — file fallback still works
            logger.info("Google credentials refreshed successfully")
        except Exception as e:
            logger.error(f"Error refreshing Google credentials: {e}")
            return None

    if not creds or not creds.valid:
        if creds and creds.expired and not creds.refresh_token:
            logger.error(
                "Google credentials expired and NO refresh_token available. "
                "Re-run setup_calendar.py to generate a new token."
            )
        else:
            logger.error("Google credentials invalid or missing")
        return None

    return creds


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
        loop = asyncio.get_running_loop()

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
                if slot >= now + timedelta(hours=2):
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


async def verify_slot_available(slot: datetime) -> bool | None:
    """Re-check Google Calendar right before creating an event.

    Returns:
        True — slot is free
        False — slot is taken (someone booked it)
        None — calendar unavailable (auth/network failure)
    """
    service = _get_service()
    if not service:
        logger.error("Cannot verify slot — Calendar not configured (blocking booking)")
        return None

    if slot.tzinfo is None:
        logger.warning(f"verify_slot_available: naive datetime {slot.isoformat()}, assuming America/Bogota")
        slot = slot.replace(tzinfo=COLOMBIA_TZ)

    settings = get_settings()
    slot_start = slot.astimezone(COLOMBIA_TZ)
    slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MINUTES)

    try:
        loop = asyncio.get_running_loop()

        def _check():
            events_result = service.events().list(
                calendarId=settings.google_calendar_id,
                timeMin=slot_start.isoformat(),
                timeMax=slot_end.isoformat(),
                singleEvents=True,
            ).execute()
            return events_result.get("items", [])

        events = await asyncio.wait_for(
            loop.run_in_executor(None, _check),
            timeout=10,
        )

        # Filter out all-day events
        timed_events = [
            ev for ev in events
            if "dateTime" in ev.get("start", {})
        ]

        if timed_events:
            names = [ev.get("summary", "?") for ev in timed_events]
            logger.warning(
                f"Slot {slot_start.isoformat()} is NO LONGER available — "
                f"conflicts with: {names}"
            )
            return False

        return True
    except Exception as e:
        logger.error(f"Error verifying slot availability: {e}")
        return None  # Calendar unavailable — let caller handle


async def delete_event(event_id: str) -> bool:
    """Delete a calendar event by its ID. Used when rescheduling."""
    service = _get_service()
    if not service:
        logger.warning("Cannot delete event — Calendar not configured")
        return False

    settings = get_settings()

    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: service.events().delete(
                    calendarId=settings.google_calendar_id,
                    eventId=event_id,
                ).execute(),
            ),
            timeout=10,
        )
        logger.info(f"Calendar event deleted: {event_id}")
        return True
    except Exception as e:
        logger.error(f"Error deleting calendar event {event_id}: {e}")
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
    """Format hour simply in Spanish: '9 a.m.', '2 p.m.', '10:30 a.m.'"""
    minute = dt.strftime("%M")
    hour = dt.hour
    period = "a.m." if hour < 12 else "p.m."
    if hour == 0:
        h = 12
    elif hour > 12:
        h = hour - 12
    else:
        h = hour
    if minute == "00":
        return f"{h} {period}"
    return f"{h}:{minute} {period}"


def format_slots_for_whatsapp(slots: list[datetime]) -> str:
    """Format available slots grouped by day, showing morning and afternoon separately."""
    ranges = group_slots_into_ranges(slots)
    if not ranges:
        return "No hay horarios disponibles en este momento."

    days_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo",
    }

    now = datetime.now(COLOMBIA_TZ)

    # Group ranges by day, keep up to 3 days max
    day_ranges: dict[str, list[str]] = {}
    for start, end in ranges:
        if start.date() == now.date():
            label = "hoy"
        elif start.date() == (now + timedelta(days=1)).date():
            label = "mañana"
        else:
            label = f"el {days_es[start.weekday()]}"

        time_range = f"de {_format_hour(start)} a {_format_hour(end)}"
        if label not in day_ranges:
            if len(day_ranges) >= 3:
                break  # Don't start a 4th day
            day_ranges[label] = []
        day_ranges[label].append(time_range)

    # Format: "mañana de 9am a 12pm y de 2pm a 5pm" or "mañana de 9am a 12pm, y el jueves de 9am a 5pm"
    day_parts = []
    for label, time_ranges in day_ranges.items():
        if len(time_ranges) == 1:
            day_parts.append(f"{label} {time_ranges[0]}")
        else:
            joined = " y ".join(time_ranges)
            day_parts.append(f"{label} {joined}")

    if len(day_parts) == 1:
        return day_parts[0]
    return ", ".join(day_parts[:-1]) + " y " + day_parts[-1]


def format_slots_detailed(slots: list[datetime]) -> str:
    """Format slots with morning/afternoon breakdown per day for GPT context."""
    if not slots:
        return "No hay horarios disponibles."

    days_es = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo",
    }
    now = datetime.now(COLOMBIA_TZ)

    # Group by day
    from collections import defaultdict
    by_day: dict[str, dict] = {}

    for slot in slots:
        if slot.date() == now.date():
            label = "hoy"
        elif slot.date() == (now + timedelta(days=1)).date():
            label = "mañana"
        else:
            label = f"el {days_es[slot.weekday()]}"

        if label not in by_day:
            by_day[label] = {"mañana": [], "tarde": []}

        if slot.hour < 12:
            by_day[label]["mañana"].append(slot)
        else:
            by_day[label]["tarde"].append(slot)

    parts = []
    for day_label, franjas in list(by_day.items())[:5]:
        morning = franjas["mañana"]
        afternoon = franjas["tarde"]
        detail = []
        if morning:
            detail.append(f"mañana ({_format_hour(morning[0])}-{_format_hour(morning[-1] + timedelta(minutes=SLOT_DURATION_MINUTES))})")
        if afternoon:
            detail.append(f"tarde ({_format_hour(afternoon[0])}-{_format_hour(afternoon[-1] + timedelta(minutes=SLOT_DURATION_MINUTES))})")
        if not morning:
            detail.append("mañana NO disponible")
        if not afternoon:
            detail.append("tarde NO disponible")
        parts.append(f"{day_label}: {', '.join(detail)}")

    return " | ".join(parts)


async def create_appointment(
    slot: datetime,
    user_name: str,
    user_phone: str,
    user_email: str = "",
    meeting_type: str = "whatsapp",
) -> dict | None:
    """Create a calendar event for the valoracion appointment.

    Args:
        meeting_type: "whatsapp" for WhatsApp video call, "meet" for Google Meet link.
    Returns the created event dict (includes hangoutLink for Meet events).
    """
    service = _get_service()
    if not service:
        logger.error(
            "Google Calendar NOT configured — cannot create appointment for "
            f"{user_name} ({user_phone}) at {slot.isoformat()}"
        )
        return None

    # Same defensive check as verify_slot_available — ensure timezone-aware
    if slot.tzinfo is None:
        logger.warning(f"create_appointment: naive datetime {slot.isoformat()}, assuming America/Bogota")
        slot = slot.replace(tzinfo=COLOMBIA_TZ)

    settings = get_settings()
    event_end = slot + timedelta(minutes=30)

    attendees = []
    if user_email:
        attendees.append({"email": user_email})

    is_meet = meeting_type == "meet"

    if is_meet:
        summary = f"💻 Google Meet Valoración {user_name} {user_phone}"
        description = (
            f"VALORACIÓN VIRTUAL — Google Meet\n"
            f"Cliente agendado via WhatsApp Bot\n"
            f"Nombre: {user_name}\n"
            f"Telefono: {user_phone}\n"
            f"\nEl enlace de Meet se genera automáticamente."
        )
    else:
        summary = f"📲 Videollamada Valoración {user_name} {user_phone}"
        description = (
            f"VALORACIÓN VIRTUAL — Videollamada de WhatsApp\n"
            f"Cliente agendado via WhatsApp Bot\n"
            f"Nombre: {user_name}\n"
            f"Telefono: {user_phone}\n"
            f"\nRecuerda llamar al cliente por videollamada de WhatsApp a este número."
        )

    event_body = {
        "summary": summary,
        "description": description,
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

    # Add Google Meet conference if requested
    if is_meet:
        import uuid
        event_body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    try:
        loop = asyncio.get_running_loop()
        conference_ver = 1 if is_meet else 0
        event = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: service.events()
                .insert(
                    calendarId=settings.google_calendar_id,
                    body=event_body,
                    conferenceDataVersion=conference_ver,
                )
                .execute(),
            ),
            timeout=15,
        )
        meet_link = event.get("hangoutLink", "")
        logger.info(f"Calendar event created: {event.get('id')}, meet_link={meet_link}")
        return event
    except asyncio.TimeoutError:
        logger.error("Timeout creating calendar event")
        return None
    except Exception as e:
        logger.error(f"Error creating calendar event: {e}")
        return None


async def book_slot_atomic(
    slot: datetime,
    user_name: str,
    user_phone: str,
    user_email: str = "",
    meeting_type: str = "whatsapp",
) -> tuple[bool, dict | None]:
    """Atomically verify availability and create appointment under a per-slot lock.

    Prevents double-booking by ensuring only one coroutine can verify+create
    for a given time slot at any moment.

    Returns:
        (is_available, event_or_none):
            - (True, event_dict) if booked successfully
            - (False, None) if slot was already taken or calendar is unavailable
    """
    lock = await _get_slot_lock(slot)

    async with lock:
        is_available = await verify_slot_available(slot)
        if is_available is None:
            # Calendar service unavailable — cannot verify or create
            logger.error(
                f"book_slot_atomic: Calendar unavailable for {slot.isoformat()} "
                f"({user_phone}) — cannot proceed"
            )
            return False, None  # Cannot verify — treat as unavailable to prevent unverified booking
        if not is_available:
            logger.warning(
                f"book_slot_atomic: slot {slot.isoformat()} taken "
                f"(blocked for {user_phone})"
            )
            return False, None

        event = await create_appointment(
            slot, user_name, user_phone, user_email, meeting_type
        )

        if not event:
            # Calendar API failed — return True (slot IS available) but None event
            # so the caller can distinguish "taken" from "API failure"
            logger.error(
                f"book_slot_atomic: Calendar API failed for {slot.isoformat()} "
                f"({user_phone}) — event not created"
            )
            return True, None

        # Post-creation safety: verify no duplicates were created
        has_duplicates = await _check_for_duplicate_events(slot, event.get("id"))
        if has_duplicates:
            logger.error(
                f"DUPLICATE DETECTED for slot {slot.isoformat()} — "
                f"this should not happen with locking. Keeping our event."
            )

        return True, event


async def _check_for_duplicate_events(
    slot: datetime, our_event_id: str | None
) -> bool:
    """Safety net: check if multiple events exist in the same slot after creation."""
    service = _get_service()
    if not service or not our_event_id:
        return False

    settings = get_settings()
    slot_start = slot.astimezone(COLOMBIA_TZ)
    slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MINUTES)

    try:
        loop = asyncio.get_running_loop()

        def _check():
            return service.events().list(
                calendarId=settings.google_calendar_id,
                timeMin=slot_start.isoformat(),
                timeMax=slot_end.isoformat(),
                singleEvents=True,
            ).execute().get("items", [])

        events = await asyncio.wait_for(
            loop.run_in_executor(None, _check), timeout=10
        )

        timed = [
            ev for ev in events
            if "dateTime" in ev.get("start", {}) and ev.get("id") != our_event_id
        ]
        if timed:
            logger.error(
                f"Found {len(timed)} OTHER events in slot "
                f"{slot_start.isoformat()}: {[e.get('summary') for e in timed]}"
            )
            return True
    except Exception as e:
        logger.error(f"Error checking for duplicates: {e}")

    return False
