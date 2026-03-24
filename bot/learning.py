"""
Learning system — saves patterns from successful conversations and Yésica's style.
Used to inject context into the system prompt so the bot improves over time.
"""

import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
COLOMBIA_TZ = ZoneInfo("America/Bogota")

LEARNING_DIR = "data/learning"
SUCCESS_PATTERNS_FILE = os.path.join(LEARNING_DIR, "success_patterns.json")
YESICA_STYLE_FILE = os.path.join(LEARNING_DIR, "yesica_style.json")

MAX_SUCCESS_PATTERNS = 20  # Keep the last 20 successful conversations
MAX_YESICA_MESSAGES = 30   # Keep the last 30 Yésica messages


def _ensure_dir():
    os.makedirs(LEARNING_DIR, exist_ok=True)


def save_success_pattern(phone: str, messages: list[dict]) -> None:
    """Save key messages from a conversation that ended in appointment_confirmed."""
    _ensure_dir()

    # Extract the most relevant messages (skip system events, keep user+assistant)
    relevant = []
    for msg in messages[-20:]:  # Last 20 messages
        if msg.get("role") in ("user", "assistant"):
            content = msg["content"]
            # Skip very short messages
            if len(content) > 10:
                relevant.append({
                    "role": msg["role"],
                    "content": content[:300],  # Truncate long messages
                })

    if not relevant:
        return

    pattern = {
        "phone_hash": phone[-4:],  # Just last 4 digits for privacy
        "timestamp": datetime.now(COLOMBIA_TZ).isoformat(),
        "message_count": len(relevant),
        "messages": relevant[-10:],  # Keep last 10 relevant messages
    }

    # Load existing patterns
    patterns = _load_json(SUCCESS_PATTERNS_FILE, [])

    # Add new pattern, keep only the most recent
    patterns.append(pattern)
    if len(patterns) > MAX_SUCCESS_PATTERNS:
        patterns = patterns[-MAX_SUCCESS_PATTERNS:]

    _save_json(SUCCESS_PATTERNS_FILE, patterns)
    logger.info(f"Saved success pattern from {phone[-4:]} ({len(relevant)} messages)")


def save_yesica_message(phone: str, text: str, is_audio: bool = False) -> None:
    """Save a message Yésica typed manually or an audio transcription."""
    if not text or len(text.strip()) < 5:
        return

    _ensure_dir()

    entry = {
        "timestamp": datetime.now(COLOMBIA_TZ).isoformat(),
        "phone_hash": phone[-4:],
        "text": text[:500],  # Truncate
        "source": "audio_transcription" if is_audio else "manual_message",
    }

    messages = _load_json(YESICA_STYLE_FILE, [])
    messages.append(entry)
    if len(messages) > MAX_YESICA_MESSAGES:
        messages = messages[-MAX_YESICA_MESSAGES:]

    _save_json(YESICA_STYLE_FILE, messages)
    logger.info(f"Saved Yésica {'audio' if is_audio else 'text'} message for learning")


def get_learning_context() -> str:
    """Build a learning context string to inject into the system prompt."""
    parts = []

    # Load Yésica's style examples
    yesica_messages = _load_json(YESICA_STYLE_FILE, [])
    if yesica_messages:
        # Take the 5 most recent unique messages
        seen = set()
        unique = []
        for msg in reversed(yesica_messages):
            text = msg["text"].strip()
            if text not in seen and len(text) > 15:
                seen.add(text)
                unique.append(text)
            if len(unique) >= 5:
                break

        if unique:
            examples = "\n".join(f"- \"{m}\"" for m in unique)
            parts.append(
                f"YESICA_STYLE_CONTEXT: Así responde Yésica cuando habla directamente con clientes. "
                f"Adapta tu estilo para ser consistente con ella:\n{examples}"
            )

    # Load success patterns
    patterns = _load_json(SUCCESS_PATTERNS_FILE, [])
    if patterns:
        # Take the 2 most recent successful conversations
        recent = patterns[-2:]
        for i, p in enumerate(recent):
            # Extract just the assistant messages as examples
            assistant_msgs = [
                m["content"] for m in p.get("messages", [])
                if m["role"] == "assistant"
            ][-3:]  # Last 3 assistant messages from the conversation

            if assistant_msgs:
                examples = "\n".join(f"  Valen: \"{m}\"" for m in assistant_msgs)
                parts.append(
                    f"SUCCESS_PATTERNS: Ejemplo de conversación exitosa #{i+1} "
                    f"(terminó en cita agendada):\n{examples}"
                )

    return "\n\n".join(parts) if parts else ""


def _load_json(path: str, default):
    """Load a JSON file or return default if it doesn't exist."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {path}: {e}")
        return default


def _save_json(path: str, data):
    """Save data to a JSON file."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving {path}: {e}")
