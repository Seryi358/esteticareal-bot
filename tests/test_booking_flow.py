"""Tests for the booking flow — double-booking prevention, stale slots, rescheduling."""

import json
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, AsyncMock, MagicMock

from bot.conversation import ConversationState

COLOMBIA_TZ = ZoneInfo("America/Bogota")


def _make_conv(phone="573001234567", **kwargs) -> ConversationState:
    """Create a ConversationState with defaults for testing."""
    defaults = dict(
        phone=phone,
        phase="chatting",
        user_display_name="Maria",
        collected_name="Maria Lopez",
    )
    defaults.update(kwargs)
    return ConversationState(**defaults)


def _future_slot(hours_ahead=26) -> datetime:
    """Return a slot in the future during business hours."""
    now = datetime.now(COLOMBIA_TZ)
    target = now + timedelta(hours=hours_ahead)
    # Ensure it's a weekday business hour
    while target.weekday() > 4:
        target += timedelta(days=1)
    return target.replace(hour=10, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# ConversationState — new fields
# ---------------------------------------------------------------------------


class TestConversationStateNewFields:
    def test_calendar_event_id_default(self):
        conv = _make_conv()
        assert conv.calendar_event_id is None

    def test_slots_fetched_at_default(self):
        conv = _make_conv()
        assert conv.slots_fetched_at is None

    def test_serialization_roundtrip(self):
        """New fields survive to_dict/from_dict cycle."""
        conv = _make_conv(
            calendar_event_id="abc123",
            slots_fetched_at="2026-04-01T10:00:00-05:00",
        )
        data = conv.to_dict()
        restored = ConversationState.from_dict(data)
        assert restored.calendar_event_id == "abc123"
        assert restored.slots_fetched_at == "2026-04-01T10:00:00-05:00"

    def test_backward_compat_missing_fields(self):
        """Old conversation JSONs without new fields still load."""
        old_data = {
            "phone": "573001234567",
            "phase": "chatting",
            "messages": [],
        }
        conv = ConversationState.from_dict(old_data)
        assert conv.calendar_event_id is None
        assert conv.slots_fetched_at is None


# ---------------------------------------------------------------------------
# Double-booking prevention — verify_slot_available
# ---------------------------------------------------------------------------


class TestDoubleBookingPrevention:
    @pytest.mark.asyncio
    async def test_slot_conflict_aborts_creation(self):
        """If slot is taken, appointment must NOT be created."""
        from bot import flow

        slot = _future_slot()
        conv = _make_conv(
            phase="collecting_data",
            appointment_datetime=slot.isoformat(),
            meeting_type="whatsapp",
        )

        with (
            patch("bot.flow.calendar.verify_slot_available", new_callable=AsyncMock, return_value=False),
            patch("bot.flow.calendar.get_available_slots", new_callable=AsyncMock, return_value=[]),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Ese horario ya no está"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._create_appointment_from_saved_slot(conv)

        # Appointment should NOT have been confirmed
        assert conv.phase != "appointment_confirmed"
        assert conv.appointment_datetime is None

    @pytest.mark.asyncio
    async def test_slot_available_creates_event(self):
        """If slot is free, appointment is created normally."""
        from bot import flow

        slot = _future_slot()
        conv = _make_conv(
            phase="collecting_data",
            appointment_datetime=slot.isoformat(),
            meeting_type="whatsapp",
        )

        fake_event = {"id": "evt_123", "hangoutLink": ""}

        with (
            patch("bot.flow.calendar.verify_slot_available", new_callable=AsyncMock, return_value=True),
            patch("bot.flow.calendar.create_appointment", new_callable=AsyncMock, return_value=fake_event),
            patch("bot.flow.evolution.send_text_message", new_callable=AsyncMock),
            patch("bot.flow._notify_yesica_appointment", new_callable=AsyncMock),
            patch("bot.flow.save_success_pattern"),
        ):
            await flow._create_appointment_from_saved_slot(conv)

        assert conv.phase == "appointment_confirmed"
        assert conv.calendar_event_id == "evt_123"


# ---------------------------------------------------------------------------
# Stale slots detection
# ---------------------------------------------------------------------------


class TestStaleSlots:
    @pytest.mark.asyncio
    async def test_old_slots_trigger_refetch(self):
        """Slots older than 15 min must be re-fetched."""
        from bot import flow

        old_time = (datetime.now(COLOMBIA_TZ) - timedelta(minutes=20)).isoformat()
        slot = _future_slot()
        slots_json = json.dumps([slot.isoformat()])

        conv = _make_conv(
            phase="awaiting_slot_selection",
            calendar_slots_json=slots_json,
            slots_fetched_at=old_time,
        )

        refetched = False

        async def fake_fetch_and_inject(c):
            nonlocal refetched
            refetched = True
            c.calendar_slots_json = slots_json
            c.slots_fetched_at = datetime.now(COLOMBIA_TZ).isoformat()

        with (
            patch("bot.flow._fetch_and_inject_slots", side_effect=fake_fetch_and_inject),
            patch("bot.flow.ai.parse_slot_selection", new_callable=AsyncMock, return_value=slot.isoformat()),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Te confirmo?"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._try_parse_slot_selection(conv, "mañana a las 10")

        assert refetched, "Stale slots should trigger re-fetch"

    @pytest.mark.asyncio
    async def test_fresh_slots_no_refetch(self):
        """Slots younger than 15 min should NOT trigger re-fetch."""
        from bot import flow

        fresh_time = (datetime.now(COLOMBIA_TZ) - timedelta(minutes=5)).isoformat()
        slot = _future_slot()
        slots_json = json.dumps([slot.isoformat()])

        conv = _make_conv(
            phase="awaiting_slot_selection",
            calendar_slots_json=slots_json,
            slots_fetched_at=fresh_time,
        )

        refetched = False

        async def fake_fetch_and_inject(c):
            nonlocal refetched
            refetched = True

        with (
            patch("bot.flow._fetch_and_inject_slots", side_effect=fake_fetch_and_inject),
            patch("bot.flow.ai.parse_slot_selection", new_callable=AsyncMock, return_value=slot.isoformat()),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Te confirmo?"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._try_parse_slot_selection(conv, "mañana a las 10")

        assert not refetched, "Fresh slots should NOT trigger re-fetch"


# ---------------------------------------------------------------------------
# Rescheduling — old event deletion
# ---------------------------------------------------------------------------


class TestRescheduling:
    @pytest.mark.asyncio
    async def test_reschedule_deletes_old_event(self):
        """Rescheduling must delete the old Google Calendar event."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="old_evt_456",
            meeting_type="whatsapp",
        )

        delete_called_with = []

        async def fake_delete(event_id):
            delete_called_with.append(event_id)
            return True

        with (
            patch("bot.flow.calendar.delete_event", side_effect=fake_delete),
            patch("bot.flow.calendar.get_available_slots", new_callable=AsyncMock, return_value=[]),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Claro, busco otro horario [REVISAR_AGENDA]"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
            patch("bot.flow._fetch_and_inject_slots", new_callable=AsyncMock),
        ):
            await flow._handle_reschedule(conv, "no puedo a esa hora")

        assert delete_called_with == ["old_evt_456"]
        assert conv.calendar_event_id is None
        assert conv.appointment_datetime is None

    @pytest.mark.asyncio
    async def test_reschedule_without_event_id(self):
        """Rescheduling without stored event ID should not crash."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id=None,
            meeting_type="whatsapp",
        )

        with (
            patch("bot.flow.calendar.delete_event", new_callable=AsyncMock) as mock_delete,
            patch("bot.flow.calendar.get_available_slots", new_callable=AsyncMock, return_value=[]),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Busco otro horario [REVISAR_AGENDA]"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
            patch("bot.flow._fetch_and_inject_slots", new_callable=AsyncMock),
        ):
            await flow._handle_reschedule(conv, "cambiar cita")

        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_reschedule_evening_deletes_event(self):
        """Evening reschedule must also delete old event before escalating."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="evt_789",
            meeting_type="whatsapp",
        )

        delete_called = False

        async def fake_delete(event_id):
            nonlocal delete_called
            delete_called = True
            return True

        with (
            patch("bot.flow.calendar.delete_event", side_effect=fake_delete),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Te conecto con Yesica"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
            patch("bot.flow._escalate_to_yesica_evening", new_callable=AsyncMock),
        ):
            await flow._handle_reschedule(conv, "puedo después de las 5")

        assert delete_called


# ---------------------------------------------------------------------------
# Rescheduling keyword detection
# ---------------------------------------------------------------------------


class TestRescheduleKeywords:
    def test_standard_keywords(self):
        from bot.flow import _wants_to_reschedule
        assert _wants_to_reschedule("no puedo a esa hora")
        assert _wants_to_reschedule("Quiero cancelar la cita")
        assert _wants_to_reschedule("Me queda difícil ese día")
        assert _wants_to_reschedule("Puedo reagendar?")

    def test_no_reschedule(self):
        from bot.flow import _wants_to_reschedule
        assert not _wants_to_reschedule("Gracias, nos vemos!")
        assert not _wants_to_reschedule("Perfecto")
        assert not _wants_to_reschedule("Qué tratamientos tienen?")
