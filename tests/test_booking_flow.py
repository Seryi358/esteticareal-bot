"""Tests for the booking flow — double-booking prevention, stale slots, rescheduling."""

import asyncio
import json
import os
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch, AsyncMock, MagicMock

# Set required env vars before importing modules that call get_settings()
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake-key-for-tests")

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
            patch("bot.flow.calendar.book_slot_atomic", new_callable=AsyncMock, return_value=(False, None)),
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
            patch("bot.flow.calendar.book_slot_atomic", new_callable=AsyncMock, return_value=(True, fake_event)),
            patch("bot.flow.evolution.send_text_message", new_callable=AsyncMock),
            patch("bot.flow._notify_yesica_appointment", new_callable=AsyncMock),
            patch("bot.flow.save_success_pattern"),
        ):
            await flow._create_appointment_from_saved_slot(conv)

        assert conv.phase == "appointment_confirmed"
        assert conv.calendar_event_id == "evt_123"

    @pytest.mark.asyncio
    async def test_concurrent_booking_only_one_wins(self):
        """Two clients booking the same slot concurrently: only one must succeed."""
        from services.calendar import book_slot_atomic, _slot_locks

        slot = _future_slot()
        _slot_locks.clear()  # Clean state

        # Track how many times create_appointment is called
        create_calls = []

        async def fake_verify(s):
            # Simulate slow calendar check — makes race window obvious without lock
            await asyncio.sleep(0.05)
            return len(create_calls) == 0  # Free only if nobody created yet

        async def fake_create(s, name, phone, email="", meeting_type="whatsapp"):
            create_calls.append(phone)
            return {"id": f"evt_{phone}", "hangoutLink": ""}

        async def fake_check_dups(s, eid):
            return False

        with (
            patch("services.calendar.verify_slot_available", side_effect=fake_verify),
            patch("services.calendar.create_appointment", side_effect=fake_create),
            patch("services.calendar._resolve_duplicate_events", side_effect=fake_check_dups),
            patch("services.calendar.find_existing_booking_for_phone", new_callable=AsyncMock, return_value=None),
        ):
            results = await asyncio.gather(
                book_slot_atomic(slot, "Maria", "573001111111"),
                book_slot_atomic(slot, "Laura", "573002222222"),
            )

        booked = [r for r in results if r[0] is True]
        rejected = [r for r in results if r[0] is False]

        assert len(booked) == 1, f"Exactly one client should book, got {len(booked)}"
        assert len(rejected) == 1, f"Exactly one client should be rejected, got {len(rejected)}"
        assert len(create_calls) == 1, f"create_appointment should be called once, got {len(create_calls)}"

    @pytest.mark.asyncio
    async def test_different_slots_book_in_parallel(self):
        """Two clients booking DIFFERENT slots should both succeed (no unnecessary blocking)."""
        from services.calendar import book_slot_atomic, _slot_locks

        slot_a = _future_slot(hours_ahead=26)
        slot_b = _future_slot(hours_ahead=28)
        _slot_locks.clear()

        async def fake_verify(s):
            return True

        async def fake_create(s, name, phone, email="", meeting_type="whatsapp"):
            return {"id": f"evt_{phone}", "hangoutLink": ""}

        async def fake_check_dups(s, eid):
            return False

        with (
            patch("services.calendar.verify_slot_available", side_effect=fake_verify),
            patch("services.calendar.create_appointment", side_effect=fake_create),
            patch("services.calendar._resolve_duplicate_events", side_effect=fake_check_dups),
            patch("services.calendar.find_existing_booking_for_phone", new_callable=AsyncMock, return_value=None),
        ):
            results = await asyncio.gather(
                book_slot_atomic(slot_a, "Maria", "573001111111"),
                book_slot_atomic(slot_b, "Laura", "573002222222"),
            )

        booked = [r for r in results if r[0] is True]
        assert len(booked) == 2, "Different slots should both book successfully"


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
            reminder_sent=True,
            reminder_day_before_sent=True,
            reminder_confirmation_pending=True,
            reminder_confirmed=True,
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
        # Reminder flags must be reset on reschedule
        assert conv.reminder_sent is False
        assert conv.reminder_day_before_sent is False
        assert conv.reminder_confirmation_pending is False
        assert conv.reminder_confirmed is False
        assert conv.appointment_cancelled is False

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


class TestReminderAIClassification:
    """Intent classification is done by services.ai (GPT call). These tests
    mock the AI response and verify the flow reacts correctly."""

    @pytest.mark.asyncio
    async def test_ai_confirm_sets_flags(self):
        """AI says 'confirm' → reminder_confirmed flips true."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="evt_abc",
            reminder_day_before_sent=True,
            reminder_confirmation_pending=True,
        )

        with (
            patch("bot.flow.ai.classify_reminder_response", new_callable=AsyncMock,
                  return_value={"decision": "confirm", "has_new_time_hint": False}),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Perfecto, te esperamos"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
            patch("bot.flow.evolution.send_text_message", new_callable=AsyncMock),
        ):
            handled = await flow._handle_reminder_response(conv, "Sí, ahí estaré")

        assert handled is True
        assert conv.reminder_confirmed is True
        assert conv.reminder_confirmation_pending is False

    @pytest.mark.asyncio
    async def test_ai_reject_without_hint_cancels(self):
        """AI says 'reject' with no new time → appointment cancelled."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="evt_def",
            reminder_day_before_sent=True,
            reminder_confirmation_pending=True,
        )

        with (
            patch("bot.flow.ai.classify_reminder_response", new_callable=AsyncMock,
                  return_value={"decision": "reject", "has_new_time_hint": False}),
            patch("bot.flow.calendar.delete_event", new_callable=AsyncMock, return_value=True),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Entendido, la cancelo"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
            patch("bot.flow.evolution.send_text_message", new_callable=AsyncMock),
        ):
            handled = await flow._handle_reminder_response(conv, "No puedo asistir")

        assert handled is True
        assert conv.appointment_cancelled is True
        assert conv.calendar_event_id is None

    @pytest.mark.asyncio
    async def test_ai_reject_with_hint_routes_to_reschedule(self):
        """AI says 'reject' with has_new_time_hint=True → routes to reschedule flow."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="evt_h",
            reminder_day_before_sent=True,
            reminder_confirmation_pending=True,
        )

        with (
            patch("bot.flow.ai.classify_reminder_response", new_callable=AsyncMock,
                  return_value={"decision": "reject", "has_new_time_hint": True}),
            patch("bot.flow._handle_reschedule", new_callable=AsyncMock) as mock_reschedule,
        ):
            handled = await flow._handle_reminder_response(conv, "no puedo, pero puedo mañana")

        assert handled is True
        mock_reschedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_ai_ambiguous_falls_through(self):
        """AI says 'ambiguous' → _handle_reminder_response returns False."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            reminder_day_before_sent=True,
            reminder_confirmation_pending=True,
        )

        with patch("bot.flow.ai.classify_reminder_response", new_callable=AsyncMock,
                   return_value={"decision": "ambiguous", "has_new_time_hint": False}):
            handled = await flow._handle_reminder_response(conv, "mmm no sé")

        assert handled is False


# ---------------------------------------------------------------------------
# Pure cancellation (cancel-only, no reschedule)
# ---------------------------------------------------------------------------


class TestPureCancellation:
    @pytest.mark.asyncio
    async def test_ai_cancel_action_triggers_handle_cancel(self):
        """When AI classifies user's message as 'cancel', _handle_cancel is called."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="evt_cancel",
        )

        with (
            patch("bot.flow.ai.classify_appointment_change", new_callable=AsyncMock,
                  return_value={"action": "cancel", "needs_outside_business_hours": False}),
            patch("bot.flow._handle_cancel", new_callable=AsyncMock) as mock_cancel,
        ):
            await flow._handle_text(conv, "Quiero cancelar la cita")

        mock_cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_ai_reschedule_action_triggers_handle_reschedule(self):
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="evt_r",
        )

        with (
            patch("bot.flow.ai.classify_appointment_change", new_callable=AsyncMock,
                  return_value={"action": "reschedule", "needs_outside_business_hours": False}),
            patch("bot.flow._handle_reschedule", new_callable=AsyncMock) as mock_reschedule,
        ):
            await flow._handle_text(conv, "no puedo, mejor mañana")

        mock_reschedule.assert_called_once()
        # needs_outside_hours should be forwarded
        call_kwargs = mock_reschedule.call_args.kwargs
        assert call_kwargs.get("needs_outside_hours") is False

    @pytest.mark.asyncio
    async def test_ai_none_action_lets_normal_flow_run(self):
        """If AI says action='none', neither cancel nor reschedule fires."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
        )

        with (
            patch("bot.flow.ai.classify_appointment_change", new_callable=AsyncMock,
                  return_value={"action": "none", "needs_outside_business_hours": False}),
            patch("bot.flow._handle_cancel", new_callable=AsyncMock) as mock_cancel,
            patch("bot.flow._handle_reschedule", new_callable=AsyncMock) as mock_reschedule,
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="hola!"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._handle_text(conv, "gracias por todo")

        mock_cancel.assert_not_called()
        mock_reschedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_deletes_event_and_notifies(self):
        """Pure cancellation must delete calendar event and notify Yésica."""
        from bot import flow

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=_future_slot().isoformat(),
            calendar_event_id="evt_cancel_123",
            meeting_type="whatsapp",
        )

        delete_called_with = []

        async def fake_delete(event_id):
            delete_called_with.append(event_id)
            return True

        yesica_messages = []

        async def fake_send(phone, text):
            if "cancelada" in text.lower() or "❌" in text:
                yesica_messages.append(text)

        with (
            patch("bot.flow.calendar.delete_event", side_effect=fake_delete),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Entendido, queda cancelada"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
            patch("bot.flow.evolution.send_text_message", side_effect=fake_send),
        ):
            await flow._handle_cancel(conv)

        assert delete_called_with == ["evt_cancel_123"]
        assert conv.appointment_cancelled is True
        assert conv.calendar_event_id is None
        assert conv.phase == "chatting"
        assert len(yesica_messages) == 1  # Yésica was notified


# ---------------------------------------------------------------------------
# Auto-cancellation of unconfirmed appointments
# ---------------------------------------------------------------------------


class TestAutoCancel:
    @pytest.mark.asyncio
    async def test_auto_cancel_triggers_in_window(self):
        """Unconfirmed appointment 3.5h away should be auto-cancelled."""
        from bot import flow

        # Appointment 3.5 hours from now (within 3-4h auto-cancel window)
        # Note: auto-cancel checks time_until only, not weekday
        appointment_time = datetime.now(COLOMBIA_TZ) + timedelta(hours=3, minutes=30)

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=appointment_time.isoformat(),
            calendar_event_id="evt_auto_cancel",
            reminder_day_before_sent=True,
            reminder_confirmation_pending=True,
            reminder_confirmed=False,
        )

        with (
            patch("bot.flow.load_conversation", return_value=conv),
            patch("bot.flow.save_conversation"),
            patch("bot.flow.calendar.delete_event", new_callable=AsyncMock, return_value=True),
            patch("bot.flow.evolution.send_text_message", new_callable=AsyncMock),
        ):
            result = await flow.send_auto_cancel_if_needed("573001234567")

        assert result is True
        assert conv.appointment_cancelled is True
        assert conv.phase == "chatting"

    @pytest.mark.asyncio
    async def test_no_auto_cancel_if_confirmed(self):
        """Confirmed appointments should NOT be auto-cancelled."""
        from bot import flow

        appointment_time = datetime.now(COLOMBIA_TZ) + timedelta(hours=3, minutes=30)
        while appointment_time.weekday() > 4:
            appointment_time += timedelta(days=1)

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=appointment_time.isoformat(),
            calendar_event_id="evt_confirmed",
            reminder_day_before_sent=True,
            reminder_confirmation_pending=False,
            reminder_confirmed=True,
        )

        with (
            patch("bot.flow.load_conversation", return_value=conv),
        ):
            result = await flow.send_auto_cancel_if_needed("573001234567")

        assert result is False

    @pytest.mark.asyncio
    async def test_no_auto_cancel_too_early(self):
        """Appointment 10h away should NOT be auto-cancelled yet."""
        from bot import flow

        appointment_time = datetime.now(COLOMBIA_TZ) + timedelta(hours=10)
        while appointment_time.weekday() > 4:
            appointment_time += timedelta(days=1)

        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=appointment_time.isoformat(),
            calendar_event_id="evt_early",
            reminder_day_before_sent=True,
            reminder_confirmation_pending=True,
        )

        with (
            patch("bot.flow.load_conversation", return_value=conv),
        ):
            result = await flow.send_auto_cancel_if_needed("573001234567")

        assert result is False


# ---------------------------------------------------------------------------
# ConversationState — new reminder fields
# ---------------------------------------------------------------------------


class TestReminderFields:
    def test_new_fields_default(self):
        conv = _make_conv()
        assert conv.reminder_confirmation_pending is False
        assert conv.reminder_confirmed is False
        assert conv.appointment_cancelled is False

    def test_serialization_roundtrip_new_fields(self):
        conv = _make_conv(
            reminder_confirmation_pending=True,
            reminder_confirmed=True,
            appointment_cancelled=True,
        )
        data = conv.to_dict()
        restored = ConversationState.from_dict(data)
        assert restored.reminder_confirmation_pending is True
        assert restored.reminder_confirmed is True
        assert restored.appointment_cancelled is True

    def test_backward_compat_old_json_no_new_fields(self):
        old_data = {
            "phone": "573001234567",
            "phase": "appointment_confirmed",
            "messages": [],
        }
        conv = ConversationState.from_dict(old_data)
        assert conv.reminder_confirmation_pending is False
        assert conv.reminder_confirmed is False
        assert conv.appointment_cancelled is False


# ---------------------------------------------------------------------------
# Past appointment auto-reset
# ---------------------------------------------------------------------------


class TestPastAppointmentReset:
    @pytest.mark.asyncio
    async def test_past_appointment_resets_to_chatting(self):
        """Patient with a past appointment should be freed to start a new flow."""
        from bot import flow

        # Appointment 2 hours ago (past the 1-hour threshold)
        past_time = datetime.now(COLOMBIA_TZ) - timedelta(hours=2)
        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=past_time.isoformat(),
            calendar_event_id="evt_past_123",
            reminder_sent=True,
            reminder_day_before_sent=True,
            reminder_confirmed=True,
        )

        with (
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Hola, en qué te puedo ayudar?"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._handle_text(conv, "Hola, quiero agendar otra cita")

        assert conv.phase == "chatting"
        assert conv.appointment_datetime is None
        assert conv.calendar_event_id is None
        assert conv.reminder_sent is False
        assert conv.reminder_confirmed is False

    @pytest.mark.asyncio
    async def test_recent_appointment_not_reset(self):
        """Appointment less than 1 hour ago should NOT be reset."""
        from bot import flow

        # Appointment 30 minutes ago (within 1-hour threshold)
        recent_time = datetime.now(COLOMBIA_TZ) - timedelta(minutes=30)
        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=recent_time.isoformat(),
            calendar_event_id="evt_recent_123",
            reminder_confirmed=True,
        )

        with (
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Hola!"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._handle_text(conv, "Gracias por la cita")

        # Should still be in appointment_confirmed (not reset yet)
        assert conv.phase == "appointment_confirmed"
        assert conv.calendar_event_id == "evt_recent_123"

    @pytest.mark.asyncio
    async def test_future_appointment_not_reset(self):
        """Future appointment should obviously NOT be reset."""
        from bot import flow

        future_time = _future_slot()
        conv = _make_conv(
            phase="appointment_confirmed",
            appointment_datetime=future_time.isoformat(),
            calendar_event_id="evt_future_123",
            reminder_confirmation_pending=True,
        )

        with (
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Tu cita es pronto!"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._handle_text(conv, "Hola")

        assert conv.phase == "appointment_confirmed"
        assert conv.calendar_event_id == "evt_future_123"


# ---------------------------------------------------------------------------
# Double-booking hardening — calendar unavailable, retry safety, duplicates
# ---------------------------------------------------------------------------


class TestCalendarUnavailableBlocksBooking:
    """When the calendar API is down, booking must be BLOCKED (fail-closed)."""

    @pytest.mark.asyncio
    async def test_calendar_unavailable_returns_false(self):
        """book_slot_atomic must return (False, None) when verify returns None."""
        from services.calendar import book_slot_atomic, _slot_locks

        slot = _future_slot()
        _slot_locks.clear()

        async def verify_returns_none(s):
            return None  # Calendar API unreachable

        with (
            patch("services.calendar.verify_slot_available", side_effect=verify_returns_none),
            patch("services.calendar.create_appointment", new_callable=AsyncMock) as mock_create,
        ):
            is_available, event = await book_slot_atomic(slot, "Maria", "573001111111")

        assert is_available is False, "Calendar unavailable must block booking (fail-closed)"
        assert event is None
        mock_create.assert_not_called()  # Must NOT attempt to create without verification

    @pytest.mark.asyncio
    async def test_calendar_unavailable_flow_handles_gracefully(self):
        """Flow must handle (False, None) from calendar unavailable — offer alternatives or escalate."""
        from bot import flow

        slot = _future_slot()
        conv = _make_conv(
            phase="collecting_data",
            appointment_datetime=slot.isoformat(),
            meeting_type="whatsapp",
        )

        with (
            patch("bot.flow.calendar.book_slot_atomic", new_callable=AsyncMock, return_value=(False, None)),
            patch("bot.flow.calendar.get_available_slots", new_callable=AsyncMock, return_value=[]),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Ese horario ya no está disponible"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._create_appointment_from_saved_slot(conv)

        assert conv.phase != "appointment_confirmed"
        assert conv.appointment_datetime is None


class TestRetryGoesAtomicPath:
    """When event creation fails, retry must go through book_slot_atomic, not raw create."""

    @pytest.mark.asyncio
    async def test_retry_uses_book_slot_atomic(self):
        """Retry after API failure must call book_slot_atomic, not create_appointment directly."""
        from bot import flow

        slot = _future_slot()
        conv = _make_conv(
            phase="collecting_data",
            appointment_datetime=slot.isoformat(),
            meeting_type="whatsapp",
        )

        call_count = 0
        fake_event = {"id": "evt_retry_ok", "hangoutLink": ""}

        async def mock_book_slot_atomic(s, name, phone, email="", meeting_type="whatsapp"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True, None  # First call: slot free, but create failed
            return True, fake_event  # Retry: success

        with (
            patch("bot.flow.calendar.book_slot_atomic", side_effect=mock_book_slot_atomic),
            patch("bot.flow.evolution.send_text_message", new_callable=AsyncMock),
            patch("bot.flow._notify_yesica_appointment", new_callable=AsyncMock),
            patch("bot.flow.save_success_pattern"),
        ):
            await flow._create_appointment_from_saved_slot(conv)

        assert call_count == 2, "book_slot_atomic should be called twice (original + retry)"
        assert conv.phase == "appointment_confirmed"
        assert conv.calendar_event_id == "evt_retry_ok"

    @pytest.mark.asyncio
    async def test_retry_conflict_on_second_attempt(self):
        """If slot gets taken between first attempt and retry, must offer alternatives."""
        from bot import flow

        slot = _future_slot()
        conv = _make_conv(
            phase="collecting_data",
            appointment_datetime=slot.isoformat(),
            meeting_type="whatsapp",
        )

        call_count = 0

        async def mock_book_slot_atomic(s, name, phone, email="", meeting_type="whatsapp"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True, None  # First: API failure
            return False, None  # Retry: slot now taken by someone else

        with (
            patch("bot.flow.calendar.book_slot_atomic", side_effect=mock_book_slot_atomic),
            patch("bot.flow.calendar.get_available_slots", new_callable=AsyncMock, return_value=[]),
            patch("bot.flow._generate_reply", new_callable=AsyncMock, return_value="Ese horario se acaba de ocupar"),
            patch("bot.flow._send_and_record", new_callable=AsyncMock),
        ):
            await flow._create_appointment_from_saved_slot(conv)

        assert conv.phase != "appointment_confirmed"
        assert conv.appointment_datetime is None

    @pytest.mark.asyncio
    async def test_no_direct_create_appointment_in_retry(self):
        """create_appointment must NEVER be called directly from flow.py retry path."""
        from bot import flow

        slot = _future_slot()
        conv = _make_conv(
            phase="collecting_data",
            appointment_datetime=slot.isoformat(),
            meeting_type="whatsapp",
        )

        # book_slot_atomic returns (True, None) both times — persistent API failure
        with (
            patch("bot.flow.calendar.book_slot_atomic", new_callable=AsyncMock, return_value=(True, None)),
            patch("bot.flow.calendar.create_appointment", new_callable=AsyncMock) as mock_direct_create,
            patch("bot.flow.evolution.send_text_message", new_callable=AsyncMock),
        ):
            await flow._create_appointment_from_saved_slot(conv)

        mock_direct_create.assert_not_called()  # Direct create must never be used


class TestDuplicateEventCleanup:
    """When duplicate events are detected, deterministic winner is kept and losers deleted."""

    @pytest.mark.asyncio
    async def test_duplicate_loser_gets_deleted(self):
        """_resolve_duplicate_events keeps earliest-created winner and deletes losers."""
        from services.calendar import _resolve_duplicate_events

        slot = _future_slot()
        our_event_id = "evt_ours"
        duplicate_id = "evt_duplicate"

        # our event created FIRST → we are winner, duplicate is loser
        fake_events = [
            {"id": our_event_id, "start": {"dateTime": slot.isoformat()},
             "summary": "Our event", "created": "2026-04-20T10:00:00Z"},
            {"id": duplicate_id, "start": {"dateTime": slot.isoformat()},
             "summary": "Duplicate", "created": "2026-04-20T10:00:02Z"},
        ]

        deleted_ids = []

        async def mock_delete(event_id):
            deleted_ids.append(event_id)
            return True

        fake_service = MagicMock()
        fake_service.events().list().execute.return_value = {"items": fake_events}

        with (
            patch("services.calendar._get_service", return_value=fake_service),
            patch("services.calendar.delete_event", side_effect=mock_delete),
        ):
            canonical = await _resolve_duplicate_events(slot, our_event_id)

        assert canonical is not None
        assert canonical.get("id") == our_event_id  # earlier created → wins
        assert duplicate_id in deleted_ids
        assert our_event_id not in deleted_ids  # Winner must not be deleted

    @pytest.mark.asyncio
    async def test_our_event_is_loser_gets_self_deleted(self):
        """If our event lost the deterministic race, it must be deleted (convergence)."""
        from services.calendar import _resolve_duplicate_events

        slot = _future_slot()
        our_event_id = "evt_ours"
        winner_id = "evt_winner"

        # winner created FIRST → we are loser
        fake_events = [
            {"id": winner_id, "start": {"dateTime": slot.isoformat()},
             "summary": "Winner", "created": "2026-04-20T10:00:00Z"},
            {"id": our_event_id, "start": {"dateTime": slot.isoformat()},
             "summary": "Our event (late)", "created": "2026-04-20T10:00:02Z"},
        ]

        deleted_ids = []

        async def mock_delete(event_id):
            deleted_ids.append(event_id)
            return True

        fake_service = MagicMock()
        fake_service.events().list().execute.return_value = {"items": fake_events}

        with (
            patch("services.calendar._get_service", return_value=fake_service),
            patch("services.calendar.delete_event", side_effect=mock_delete),
        ):
            canonical = await _resolve_duplicate_events(slot, our_event_id)

        assert canonical is not None
        assert canonical.get("id") == winner_id
        assert our_event_id in deleted_ids  # losers (including us) get deleted

    @pytest.mark.asyncio
    async def test_no_duplicates_returns_none(self):
        """When no duplicates exist, _resolve_duplicate_events returns None."""
        from services.calendar import _resolve_duplicate_events

        slot = _future_slot()
        our_event_id = "evt_only_one"

        fake_events = [
            {"id": our_event_id, "start": {"dateTime": slot.isoformat()}, "summary": "Only event"},
        ]

        fake_service = MagicMock()
        fake_service.events().list().execute.return_value = {"items": fake_events}

        with (
            patch("services.calendar._get_service", return_value=fake_service),
            patch("services.calendar.delete_event", new_callable=AsyncMock) as mock_delete,
        ):
            canonical = await _resolve_duplicate_events(slot, our_event_id)

        assert canonical is None
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_phone_idempotency_prevents_duplicate_create(self):
        """find_existing_booking_for_phone short-circuits booking if phone already has event."""
        from services.calendar import book_slot_atomic, _slot_locks
        _slot_locks.clear()

        slot = _future_slot()
        existing_event = {
            "id": "evt_existing",
            "summary": "Videollamada Valoración Maria 573001111111",
            "start": {"dateTime": slot.isoformat()},
        }

        create_calls = []

        async def fake_create(*a, **k):
            create_calls.append(True)
            return {"id": "evt_new"}

        with (
            patch("services.calendar.find_existing_booking_for_phone",
                  new_callable=AsyncMock, return_value=existing_event),
            patch("services.calendar.verify_slot_available",
                  new_callable=AsyncMock, return_value=True),
            patch("services.calendar.create_appointment", side_effect=fake_create),
        ):
            ok, event = await book_slot_atomic(slot, "Maria", "573001111111")

        assert ok is True
        assert event is existing_event  # returns existing instead of creating
        assert len(create_calls) == 0  # create_appointment MUST NOT be called


class TestConcurrentBookingWithFlakiness:
    """Simulate real-world scenario: two users, flaky API, verify the system holds."""

    @pytest.mark.asyncio
    async def test_two_users_flaky_api_only_one_books(self):
        """With intermittent API failures, at most one booking must succeed per slot."""
        from services.calendar import book_slot_atomic, _slot_locks

        slot = _future_slot()
        _slot_locks.clear()

        booked_events = []
        verify_call_count = 0

        async def flaky_verify(s):
            nonlocal verify_call_count
            verify_call_count += 1
            # First call might be slow but succeeds; second sees the event
            await asyncio.sleep(0.02)
            return len(booked_events) == 0

        async def fake_create(s, name, phone, email="", meeting_type="whatsapp"):
            booked_events.append(phone)
            return {"id": f"evt_{phone}", "hangoutLink": ""}

        async def fake_check_dups(s, eid):
            return False

        with (
            patch("services.calendar.verify_slot_available", side_effect=flaky_verify),
            patch("services.calendar.create_appointment", side_effect=fake_create),
            patch("services.calendar._resolve_duplicate_events", side_effect=fake_check_dups),
            patch("services.calendar.find_existing_booking_for_phone", new_callable=AsyncMock, return_value=None),
        ):
            # Launch 3 concurrent booking attempts for the same slot
            results = await asyncio.gather(
                book_slot_atomic(slot, "Maria", "573001111111"),
                book_slot_atomic(slot, "Laura", "573002222222"),
                book_slot_atomic(slot, "Sofia", "573003333333"),
            )

        successful = [r for r in results if r[0] is True and r[1] is not None]
        rejected = [r for r in results if r[0] is False]

        assert len(successful) == 1, f"Only one booking should succeed, got {len(successful)}"
        assert len(rejected) == 2, f"Two should be rejected, got {len(rejected)}"
