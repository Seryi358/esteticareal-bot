import asyncio
import json
import os
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from config import get_settings

COLOMBIA_TZ = ZoneInfo("America/Bogota")


@dataclass
class ConversationState:
    phone: str
    phase: str = "chatting"  # chatting | awaiting_slot_selection | awaiting_confirmation | awaiting_meeting_type | collecting_data | appointment_confirmed | escalated_to_yesica
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
    meeting_type: Optional[str] = None  # "whatsapp" | "meet"
    meet_link: Optional[str] = None  # Google Meet URL when meeting_type == "meet"
    offered_pay_at_clinic: bool = False  # True after offering same-day payment
    pay_at_clinic: bool = False  # Legacy field — kept for backward compat
    human_takeover: bool = False  # Legacy — kept for backward compat with saved JSONs
    human_takeover_until: Optional[str] = None  # ISO timestamp — bot silent until this time
    last_user_message_at: Optional[str] = None  # ISO timestamp — for 24h follow-up
    follow_up_sent: bool = False  # True after automatic 24h follow-up sent
    reminder_sent: bool = False  # True after appointment reminder sent (2h before)
    reminder_day_before_sent: bool = False  # True after day-before reminder sent (24h before)
    messages: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationState":
        # Filter to only known fields — allows old/new JSON files to load safely
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        # Keep last 30 messages to avoid token limit issues
        if len(self.messages) > 30:
            self.messages = self.messages[-30:]

    def inject_system_event(self, event: str) -> None:
        """Inject a system event that the AI can read and react to."""
        self.messages.append({"role": "system", "content": event})

    def is_human_takeover_active(self) -> bool:
        """Check if Yesica's takeover window is still active."""
        if not self.human_takeover_until:
            return False
        try:
            until = datetime.fromisoformat(self.human_takeover_until).replace(tzinfo=COLOMBIA_TZ)
            if datetime.now(COLOMBIA_TZ) < until:
                return True
            # Window expired — clear until but keep human_takeover=True
            # so flow.py can detect the transition and inject context
            self.human_takeover_until = None
            return False
        except Exception:
            self.human_takeover_until = None
            return False


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

    # Sync key fields to Google Sheets
    try:
        from services.sheets import sync_conversation
        nombre = conv.collected_name or conv.user_display_name
        is_booked = conv.phase == "appointment_confirmed"
        appointment_dt = ""
        if conv.appointment_datetime:
            try:
                from datetime import datetime as _dt
                apt = _dt.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
                h = apt.hour
                period = "a.m." if h < 12 else "p.m."
                h12 = 12 if h == 0 else (h - 12 if h > 12 else h)
                appointment_dt = f"{apt.strftime('%Y-%m-%d')} {h12}:{apt.strftime('%M')} {period}"
            except Exception:
                pass
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                sync_conversation(
                    conv.phone,
                    nombre=nombre,
                    ciudad=conv.city,
                    servicio=conv.service_interest,
                    is_booked=is_booked,
                    appointment_dt=appointment_dt,
                )
            )
        except RuntimeError:
            pass  # No running event loop — skip sheets sync
    except Exception:
        pass  # Don't let sheets sync failure break conversation saves
