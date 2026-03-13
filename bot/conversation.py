import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional
from config import get_settings


@dataclass
class ConversationState:
    phone: str
    phase: str = "chatting"  # chatting | awaiting_screenshot | collecting_data | awaiting_slot_selection | appointment_confirmed
    user_display_name: Optional[str] = None
    service_interest: Optional[str] = None
    city: Optional[str] = None
    payment_verified: bool = False
    notification_sent: bool = False
    collected_name: Optional[str] = None
    collected_phone: Optional[str] = None
    collected_email: Optional[str] = None
    calendar_slots_json: Optional[str] = None  # JSON string of available slots
    appointment_datetime: Optional[str] = None
    messages: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationState":
        return cls(**data)

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        # Keep last 30 messages to avoid token limit issues
        if len(self.messages) > 30:
            self.messages = self.messages[-30:]

    def inject_system_event(self, event: str) -> None:
        """Inject a system event that the AI can read and react to."""
        self.messages.append({"role": "system", "content": event})


def _conversation_path(phone: str) -> str:
    settings = get_settings()
    os.makedirs(settings.conversations_dir, exist_ok=True)
    safe_phone = phone.replace("+", "").replace("@", "_").replace(":", "_")
    return os.path.join(settings.conversations_dir, f"{safe_phone}.json")


def load_conversation(phone: str) -> ConversationState:
    path = _conversation_path(phone)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ConversationState.from_dict(data)
        except Exception:
            pass
    return ConversationState(phone=phone)


def save_conversation(conv: ConversationState) -> None:
    path = _conversation_path(conv.phone)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(conv.to_dict(), f, ensure_ascii=False, indent=2)
