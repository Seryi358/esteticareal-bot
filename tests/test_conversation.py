"""Tests for bot/conversation.py — ConversationState, load/save, persistence."""

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

from bot.conversation import (
    ConversationState,
    _conversation_path,
    load_conversation,
    save_conversation,
    COLOMBIA_TZ,
)


# ---------------------------------------------------------------------------
# ConversationState — dataclass basics
# ---------------------------------------------------------------------------

class TestConversationStateDefaults:
    def test_defaults(self):
        cs = ConversationState(phone="573001234567")
        assert cs.phone == "573001234567"
        assert cs.phase == "chatting"
        assert cs.user_display_name is None
        assert cs.service_interest is None
        assert cs.city is None
        assert cs.payment_verified is False
        assert cs.notification_sent is False
        assert cs.collected_name is None
        assert cs.collected_phone is None
        assert cs.collected_email is None
        assert cs.calendar_slots_json is None
        assert cs.slots_fetched_at is None
        assert cs.appointment_datetime is None
        assert cs.calendar_event_id is None
        assert cs.meeting_type is None
        assert cs.meet_link is None
        assert cs.offered_pay_at_clinic is False
        assert cs.pay_at_clinic is False
        assert cs.human_takeover is False
        assert cs.human_takeover_until is None
        assert cs.last_user_message_at is None
        assert cs.follow_up_sent is False
        assert cs.reminder_sent is False
        assert cs.reminder_day_before_sent is False
        assert cs.reminder_confirmation_pending is False
        assert cs.reminder_confirmed is False
        assert cs.appointment_cancelled is False
        assert cs.escalated_at is None
        assert cs.messages == []

    def test_messages_not_shared(self):
        """Each instance should have its own messages list."""
        a = ConversationState(phone="111")
        b = ConversationState(phone="222")
        a.add_message("user", "hello")
        assert len(b.messages) == 0


# ---------------------------------------------------------------------------
# to_dict / from_dict round-trip
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_roundtrip(self):
        cs = ConversationState(
            phone="573001234567",
            phase="awaiting_slot_selection",
            user_display_name="María",
            city="Medellín",
            collected_name="María García",
            meeting_type="whatsapp",
        )
        cs.add_message("user", "Hola")
        cs.add_message("assistant", "Hola María")

        d = cs.to_dict()
        restored = ConversationState.from_dict(d)
        assert restored.phone == cs.phone
        assert restored.phase == cs.phase
        assert restored.user_display_name == cs.user_display_name
        assert restored.city == cs.city
        assert restored.meeting_type == cs.meeting_type
        assert len(restored.messages) == 2

    def test_backward_compat_extra_fields(self):
        """Unknown keys in JSON should be silently ignored."""
        data = {"phone": "111", "phase": "chatting", "unknown_field": True, "messages": []}
        cs = ConversationState.from_dict(data)
        assert cs.phone == "111"
        assert not hasattr(cs, "unknown_field")

    def test_backward_compat_missing_fields(self):
        """Missing keys should use defaults."""
        data = {"phone": "111"}
        cs = ConversationState.from_dict(data)
        assert cs.phase == "chatting"
        assert cs.messages == []
        assert cs.reminder_sent is False


# ---------------------------------------------------------------------------
# add_message — with trimming
# ---------------------------------------------------------------------------

class TestAddMessage:
    def test_basic_add(self):
        cs = ConversationState(phone="111")
        cs.add_message("user", "Hello")
        assert len(cs.messages) == 1
        assert cs.messages[0] == {"role": "user", "content": "Hello"}

    def test_trims_to_30(self):
        cs = ConversationState(phone="111")
        for i in range(35):
            cs.add_message("user", f"msg {i}")
        assert len(cs.messages) == 30
        # Should keep the last 30 (msg 5..34)
        assert cs.messages[0]["content"] == "msg 5"
        assert cs.messages[-1]["content"] == "msg 34"

    def test_multiple_roles(self):
        cs = ConversationState(phone="111")
        cs.add_message("user", "hi")
        cs.add_message("assistant", "hello")
        cs.add_message("system", "event")
        assert len(cs.messages) == 3


# ---------------------------------------------------------------------------
# inject_system_event
# ---------------------------------------------------------------------------

class TestInjectSystemEvent:
    def test_adds_system_message(self):
        cs = ConversationState(phone="111")
        cs.inject_system_event("CALENDAR_SLOTS: ...")
        assert cs.messages[-1]["role"] == "system"
        assert "CALENDAR_SLOTS" in cs.messages[-1]["content"]


# ---------------------------------------------------------------------------
# is_human_takeover_active
# ---------------------------------------------------------------------------

class TestIsHumanTakeoverActive:
    def test_no_until_returns_false(self):
        cs = ConversationState(phone="111")
        assert cs.is_human_takeover_active() is False

    def test_future_until_returns_true(self):
        future = (datetime.now(COLOMBIA_TZ) + timedelta(hours=1)).isoformat()
        cs = ConversationState(phone="111", human_takeover_until=future)
        assert cs.is_human_takeover_active() is True

    def test_past_until_returns_false_and_clears(self):
        past = (datetime.now(COLOMBIA_TZ) - timedelta(hours=1)).isoformat()
        cs = ConversationState(phone="111", human_takeover_until=past)
        assert cs.is_human_takeover_active() is False
        assert cs.human_takeover_until is None

    def test_invalid_until_returns_false_and_clears(self):
        cs = ConversationState(phone="111", human_takeover_until="not-a-date")
        assert cs.is_human_takeover_active() is False
        assert cs.human_takeover_until is None

    def test_naive_datetime_treated_as_colombia(self):
        """Naive datetime (no tz info) should be treated as Colombia TZ."""
        future = (datetime.now(COLOMBIA_TZ) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        cs = ConversationState(phone="111", human_takeover_until=future)
        assert cs.is_human_takeover_active() is True


# ---------------------------------------------------------------------------
# _conversation_path
# ---------------------------------------------------------------------------

class TestConversationPath:
    @patch("bot.conversation.get_settings")
    def test_path_sanitization(self, mock_settings):
        settings = MagicMock()
        settings.conversations_dir = "/tmp/test_convs"
        mock_settings.return_value = settings
        path = _conversation_path("+573001234567")
        assert path == "/tmp/test_convs/573001234567.json"

    @patch("bot.conversation.get_settings")
    def test_path_at_colon_sanitization(self, mock_settings):
        settings = MagicMock()
        settings.conversations_dir = "/tmp/test_convs"
        mock_settings.return_value = settings
        path = _conversation_path("user@example:test")
        assert path == "/tmp/test_convs/user_example_test.json"


# ---------------------------------------------------------------------------
# load_conversation / save_conversation
# ---------------------------------------------------------------------------

class TestLoadSave:
    @patch("bot.conversation.get_settings")
    def test_load_nonexistent_returns_default(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = MagicMock()
            settings.conversations_dir = tmpdir
            mock_settings.return_value = settings
            cs = load_conversation("573009999999")
            assert cs.phone == "573009999999"
            assert cs.phase == "chatting"

    @patch("bot.conversation.get_settings")
    def test_save_and_load_roundtrip(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = MagicMock()
            settings.conversations_dir = tmpdir
            mock_settings.return_value = settings

            cs = ConversationState(phone="573001111111", phase="awaiting_confirmation")
            cs.user_display_name = "Test User"
            cs.add_message("user", "Hola")

            # Patch asyncio to skip sheets sync (no running loop)
            with patch("bot.conversation.asyncio") as mock_asyncio:
                mock_asyncio.get_running_loop.side_effect = RuntimeError("no loop")
                save_conversation(cs)

            loaded = load_conversation("573001111111")
            assert loaded.phone == "573001111111"
            assert loaded.phase == "awaiting_confirmation"
            assert loaded.user_display_name == "Test User"
            assert len(loaded.messages) == 1

    @patch("bot.conversation.get_settings")
    def test_load_corrupt_json_returns_default(self, mock_settings):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = MagicMock()
            settings.conversations_dir = tmpdir
            mock_settings.return_value = settings

            # Write a corrupt file
            path = os.path.join(tmpdir, "573001111111.json")
            with open(path, "w") as f:
                f.write("{not valid json")

            cs = load_conversation("573001111111")
            assert cs.phone == "573001111111"
            assert cs.phase == "chatting"

    @patch("bot.conversation.get_settings")
    def test_save_atomic_write(self, mock_settings):
        """save_conversation uses atomic write (tempfile + os.replace)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = MagicMock()
            settings.conversations_dir = tmpdir
            mock_settings.return_value = settings

            cs = ConversationState(phone="573002222222")

            with patch("bot.conversation.asyncio") as mock_asyncio:
                mock_asyncio.get_running_loop.side_effect = RuntimeError("no loop")
                save_conversation(cs)

            path = os.path.join(tmpdir, "573002222222.json")
            assert os.path.exists(path)
            with open(path) as f:
                data = json.load(f)
            assert data["phone"] == "573002222222"
