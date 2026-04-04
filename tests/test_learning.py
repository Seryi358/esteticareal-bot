"""Tests for bot/learning.py — success patterns, Yésica style, learning context."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from bot.learning import (
    save_success_pattern,
    save_yesica_message,
    get_learning_context,
    _load_json,
    _save_json,
    MAX_SUCCESS_PATTERNS,
    MAX_YESICA_MESSAGES,
    SUCCESS_PATTERNS_FILE,
    YESICA_STYLE_FILE,
)


@pytest.fixture(autouse=True)
def _use_tmp_learning_dir(tmp_path):
    """Redirect learning files to temp directory for all tests."""
    patterns_path = str(tmp_path / "success_patterns.json")
    style_path = str(tmp_path / "yesica_style.json")
    learning_dir = str(tmp_path)
    with patch("bot.learning.SUCCESS_PATTERNS_FILE", patterns_path), \
         patch("bot.learning.YESICA_STYLE_FILE", style_path), \
         patch("bot.learning.LEARNING_DIR", learning_dir):
        yield


# ---------------------------------------------------------------------------
# _load_json / _save_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_load_nonexistent_returns_default(self, tmp_path):
        result = _load_json(str(tmp_path / "nope.json"), [])
        assert result == []

    def test_load_nonexistent_returns_dict_default(self, tmp_path):
        result = _load_json(str(tmp_path / "nope.json"), {"key": "val"})
        assert result == {"key": "val"}

    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "test.json")
        _save_json(path, [1, 2, 3])
        result = _load_json(path, [])
        assert result == [1, 2, 3]

    def test_load_corrupt_returns_default(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("{corrupt")
        result = _load_json(path, "default")
        assert result == "default"


# ---------------------------------------------------------------------------
# save_success_pattern
# ---------------------------------------------------------------------------

class TestSaveSuccessPattern:
    def test_saves_pattern(self, tmp_path):
        messages = [
            {"role": "user", "content": "Hola, quiero agendar una cita"},
            {"role": "assistant", "content": "Claro, déjame revisar la agenda"},
            {"role": "system", "content": "CALENDAR_SLOTS: ..."},
            {"role": "user", "content": "El martes me sirve"},
            {"role": "assistant", "content": "Listo, agendada tu cita"},
        ]
        save_success_pattern("573001234567", messages)

        patterns_path = str(tmp_path / "success_patterns.json")
        with open(patterns_path) as f:
            patterns = json.load(f)
        assert len(patterns) == 1
        assert patterns[0]["phone_hash"] == "4567"
        assert patterns[0]["message_count"] > 0
        # System messages should be filtered out
        for msg in patterns[0]["messages"]:
            assert msg["role"] in ("user", "assistant")

    def test_skips_short_messages(self, tmp_path):
        messages = [
            {"role": "user", "content": "Sí"},  # 2 chars — too short
            {"role": "assistant", "content": "Ok"},  # 2 chars — too short
        ]
        save_success_pattern("573001234567", messages)

        patterns_path = str(tmp_path / "success_patterns.json")
        if os.path.exists(patterns_path):
            with open(patterns_path) as f:
                patterns = json.load(f)
            # Pattern should not be saved since no relevant messages
            assert len(patterns) == 0
        # File might not exist if nothing was saved — that's fine too

    def test_skips_system_messages(self, tmp_path):
        messages = [
            {"role": "system", "content": "You are a helpful assistant that helps a lot"},
            {"role": "user", "content": "Quiero agendar una cita por favor"},
        ]
        save_success_pattern("573001234567", messages)

        patterns_path = str(tmp_path / "success_patterns.json")
        with open(patterns_path) as f:
            patterns = json.load(f)
        assert len(patterns) == 1
        assert all(m["role"] != "system" for m in patterns[0]["messages"])

    def test_truncates_long_messages(self, tmp_path):
        messages = [
            {"role": "user", "content": "x" * 500},
        ]
        save_success_pattern("573001234567", messages)

        patterns_path = str(tmp_path / "success_patterns.json")
        with open(patterns_path) as f:
            patterns = json.load(f)
        assert len(patterns[0]["messages"][0]["content"]) <= 300

    def test_limits_to_max_patterns(self, tmp_path):
        for i in range(MAX_SUCCESS_PATTERNS + 5):
            messages = [
                {"role": "user", "content": f"Message number {i} from test user"},
            ]
            save_success_pattern(f"57300{i:07d}", messages)

        patterns_path = str(tmp_path / "success_patterns.json")
        with open(patterns_path) as f:
            patterns = json.load(f)
        assert len(patterns) == MAX_SUCCESS_PATTERNS

    def test_empty_messages_no_save(self, tmp_path):
        save_success_pattern("573001234567", [])
        patterns_path = str(tmp_path / "success_patterns.json")
        # File either doesn't exist or has empty array
        if os.path.exists(patterns_path):
            with open(patterns_path) as f:
                patterns = json.load(f)
            assert len(patterns) == 0


# ---------------------------------------------------------------------------
# save_yesica_message
# ---------------------------------------------------------------------------

class TestSaveYesicaMessage:
    def test_saves_text_message(self, tmp_path):
        save_yesica_message("573001234567", "Hola, agenda tu cita para mañana")

        style_path = str(tmp_path / "yesica_style.json")
        with open(style_path) as f:
            entries = json.load(f)
        assert len(entries) == 1
        assert entries[0]["phone_hash"] == "4567"
        assert entries[0]["source"] == "manual_message"
        assert "mañana" in entries[0]["text"]

    def test_saves_audio_message(self, tmp_path):
        save_yesica_message("573001234567", "Hola esto es un audio de prueba", is_audio=True)

        style_path = str(tmp_path / "yesica_style.json")
        with open(style_path) as f:
            entries = json.load(f)
        assert entries[0]["source"] == "audio_transcription"

    def test_skips_empty_text(self, tmp_path):
        save_yesica_message("573001234567", "")
        save_yesica_message("573001234567", "   ")
        save_yesica_message("573001234567", "ab")  # < 5 chars stripped

        style_path = str(tmp_path / "yesica_style.json")
        assert not os.path.exists(style_path)

    def test_truncates_long_text(self, tmp_path):
        save_yesica_message("573001234567", "x" * 700)

        style_path = str(tmp_path / "yesica_style.json")
        with open(style_path) as f:
            entries = json.load(f)
        assert len(entries[0]["text"]) <= 500

    def test_limits_to_max_messages(self, tmp_path):
        for i in range(MAX_YESICA_MESSAGES + 5):
            save_yesica_message("573001234567", f"Yésica message number {i} for testing")

        style_path = str(tmp_path / "yesica_style.json")
        with open(style_path) as f:
            entries = json.load(f)
        assert len(entries) == MAX_YESICA_MESSAGES


# ---------------------------------------------------------------------------
# get_learning_context
# ---------------------------------------------------------------------------

class TestGetLearningContext:
    def test_empty_returns_empty_string(self, tmp_path):
        ctx = get_learning_context()
        assert ctx == ""

    def test_with_yesica_style(self, tmp_path):
        save_yesica_message("573001234567", "Hola, te confirmo tu cita para mañana a las 10")
        save_yesica_message("573001234567", "Dale, nos vemos en la valoración virtual")

        ctx = get_learning_context()
        assert "YESICA_STYLE_CONTEXT" in ctx
        assert "confirmo tu cita" in ctx

    def test_with_success_patterns(self, tmp_path):
        messages = [
            {"role": "user", "content": "Quiero agendar una cita por favor"},
            {"role": "assistant", "content": "Dale, déjame revisar la agenda de Yésica"},
            {"role": "user", "content": "El martes en la mañana me sirve"},
            {"role": "assistant", "content": "Listo, te agendé para el martes a las 10am"},
        ]
        save_success_pattern("573001234567", messages)

        ctx = get_learning_context()
        assert "SUCCESS_PATTERNS" in ctx
        assert "agenda" in ctx.lower()

    def test_yesica_style_deduplication(self, tmp_path):
        # Same message repeated — should only appear once in context
        for _ in range(5):
            save_yesica_message("573001234567", "Confirmo tu cita para mañana a las diez")

        ctx = get_learning_context()
        assert ctx.count("Confirmo tu cita") == 1

    def test_yesica_style_skips_short(self, tmp_path):
        save_yesica_message("573001234567", "ok sí claro dale listo")  # > 15 chars
        save_yesica_message("573001234568", "sí ok dale de una pues")  # > 15 chars

        ctx = get_learning_context()
        assert "YESICA_STYLE_CONTEXT" in ctx
