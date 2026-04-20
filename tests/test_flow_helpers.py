"""Tests for pure helper functions in bot/flow.py — no async, no external deps.

Intent detection (reschedule / cancel / reminder confirm) is now handled by AI
calls (services.ai.classify_appointment_change, classify_reminder_response).
Those helpers are exercised in test_booking_flow.py with mocks — this file
keeps only the pure-sync helpers.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot.flow import (
    _month_name,
    _format_time_spanish,
    _format_appointment_datetime,
    _ensure_conversation_alive,
    _clear_attempt_counters,
)


COLOMBIA_TZ = ZoneInfo("America/Bogota")


# ---------------------------------------------------------------------------
# _month_name
# ---------------------------------------------------------------------------

class TestMonthName:
    def test_all_months(self):
        expected = [
            "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
        ]
        for i, name in enumerate(expected, 1):
            assert _month_name(i) == name

    def test_invalid_month_raises(self):
        with pytest.raises(KeyError):
            _month_name(13)

        with pytest.raises(KeyError):
            _month_name(0)


# ---------------------------------------------------------------------------
# _format_time_spanish
# ---------------------------------------------------------------------------

class TestFormatTimeSpanish:
    def test_morning_no_minutes(self):
        dt = datetime(2026, 1, 1, 9, 0, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "9 a.m."

    def test_afternoon_no_minutes(self):
        dt = datetime(2026, 1, 1, 14, 0, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "2 p.m."

    def test_with_minutes(self):
        dt = datetime(2026, 1, 1, 15, 30, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "3:30 p.m."

    def test_noon(self):
        dt = datetime(2026, 1, 1, 12, 0, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "12 p.m."

    def test_midnight(self):
        dt = datetime(2026, 1, 1, 0, 0, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "12 a.m."

    def test_one_am(self):
        dt = datetime(2026, 1, 1, 1, 0, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "1 a.m."

    def test_11pm_with_minutes(self):
        dt = datetime(2026, 1, 1, 23, 45, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "11:45 p.m."

    def test_morning_with_minutes(self):
        dt = datetime(2026, 1, 1, 10, 15, tzinfo=COLOMBIA_TZ)
        assert _format_time_spanish(dt) == "10:15 a.m."


# ---------------------------------------------------------------------------
# _format_appointment_datetime
# ---------------------------------------------------------------------------

class TestFormatAppointmentDatetime:
    def test_basic(self):
        # 2026-04-06 is a Monday
        dt = datetime(2026, 4, 6, 10, 0, tzinfo=COLOMBIA_TZ)
        result = _format_appointment_datetime(dt)
        assert result == "lunes 6 de abril a las 10 a.m."

    def test_with_minutes(self):
        # 2026-04-07 is a Tuesday
        dt = datetime(2026, 4, 7, 14, 30, tzinfo=COLOMBIA_TZ)
        result = _format_appointment_datetime(dt)
        assert result == "martes 7 de abril a las 2:30 p.m."

    def test_friday(self):
        # 2026-04-10 is a Friday
        dt = datetime(2026, 4, 10, 9, 0, tzinfo=COLOMBIA_TZ)
        result = _format_appointment_datetime(dt)
        assert result == "viernes 10 de abril a las 9 a.m."

    def test_saturday(self):
        # 2026-04-11 is a Saturday
        dt = datetime(2026, 4, 11, 11, 0, tzinfo=COLOMBIA_TZ)
        result = _format_appointment_datetime(dt)
        assert result == "sábado 11 de abril a las 11 a.m."

    def test_sunday(self):
        # 2026-04-12 is a Sunday
        dt = datetime(2026, 4, 12, 16, 0, tzinfo=COLOMBIA_TZ)
        result = _format_appointment_datetime(dt)
        assert result == "domingo 12 de abril a las 4 p.m."


# ---------------------------------------------------------------------------
# _ensure_conversation_alive
# ---------------------------------------------------------------------------

class TestEnsureConversationAlive:
    def test_empty_reply_gets_fallback(self):
        result = _ensure_conversation_alive("", "chatting")
        assert result == "¿En qué te puedo ayudar?"

    def test_none_reply_gets_fallback(self):
        result = _ensure_conversation_alive(None, "chatting")
        assert result == "¿En qué te puedo ayudar?"

    def test_whitespace_reply_gets_fallback(self):
        result = _ensure_conversation_alive("   ", "chatting")
        assert result == "¿En qué te puedo ayudar?"

    def test_normal_reply_chatting_phase(self):
        reply = "Hola, cuéntame qué te interesa"
        result = _ensure_conversation_alive(reply, "chatting")
        assert result == reply

    def test_appointment_confirmed_passes_through(self):
        reply = "Tu cita está agendada"
        result = _ensure_conversation_alive(reply, "appointment_confirmed")
        assert result == reply

    def test_escalated_passes_through(self):
        reply = "Te conecto con Yésica"
        result = _ensure_conversation_alive(reply, "escalated_to_yesica")
        assert result == reply

    def test_collecting_data_passes_through(self):
        reply = "¿Me das tu nombre?"
        result = _ensure_conversation_alive(reply, "collecting_data")
        assert result == reply

    def test_awaiting_confirmation_passes_through(self):
        reply = "¿Confirmas?"
        result = _ensure_conversation_alive(reply, "awaiting_confirmation")
        assert result == reply

    def test_awaiting_meeting_type_passes_through(self):
        reply = "¿WhatsApp o Meet?"
        result = _ensure_conversation_alive(reply, "awaiting_meeting_type")
        assert result == reply

    def test_instagram_link_passes_through(self):
        reply = "Síguenos en https://instagram.com/esteticareal.yr"
        result = _ensure_conversation_alive(reply, "chatting")
        assert result == reply


# ---------------------------------------------------------------------------
# _clear_attempt_counters
# ---------------------------------------------------------------------------

class TestClearAttemptCounters:
    def test_clears_all_counters(self):
        from bot.flow import (
            _slot_selection_attempts,
            _confirmation_attempts,
            _meeting_type_attempts,
            _data_collection_attempts,
        )
        phone = "573001234567"
        _slot_selection_attempts[phone] = 3
        _confirmation_attempts[phone] = 2
        _meeting_type_attempts[phone] = 1
        _data_collection_attempts[phone] = 1

        _clear_attempt_counters(phone)

        assert phone not in _slot_selection_attempts
        assert phone not in _confirmation_attempts
        assert phone not in _meeting_type_attempts
        assert phone not in _data_collection_attempts

    def test_clears_nonexistent_phone_no_error(self):
        _clear_attempt_counters("nonexistent_phone_999")
