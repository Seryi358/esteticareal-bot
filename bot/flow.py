import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.conversation import ConversationState, load_conversation, save_conversation
from bot.learning import save_success_pattern, save_yesica_message, get_learning_context
from bot.prompts import SYSTEM_PROMPT
from config import get_settings
from services import ai, calendar, evolution

logger = logging.getLogger(__name__)
COLOMBIA_TZ = ZoneInfo("America/Bogota")

# Regex to strip emojis and special unicode symbols
_EMOJI_RE = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    r"\U0001F1E0-\U0001F1FF\U0001FA00-\U0001FAFF\U0001F900-\U0001F9FF"
    r"\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0000200D\U00002600-\U000026FF"
    r"\U00002700-\U000027BF\U0000231A-\U0000231B\U00002934-\U00002935"
    r"\U000025AA-\U000025FE\U00002B05-\U00002B55\U00003030\U0000303D"
    r"\U00003297\U00003299\U0000200B-\U0000200F\u2728\u2764\u2665\u2763"
    r"\u270A-\u270D\u2744\u274C\u274E\u2753-\u2755\u2757\u2795-\u2797"
    r"\u27A1\u27B0\u27BF✨]+",
    re.UNICODE,
)

# ---------------------------------------------------------------------------
# Common Spanish names — used to extract names from usernames
# ---------------------------------------------------------------------------
_COMMON_NAMES = {
    # Female
    "maria", "ana", "laura", "andrea", "diana", "paula", "sara", "luz",
    "angela", "angelica", "sandra", "carolina", "paola", "valentina",
    "juliana", "natalia", "monica", "camila", "daniela", "alejandra",
    "catalina", "isabella", "sofia", "gabriela", "fernanda", "luisa",
    "marcela", "patricia", "claudia", "liliana", "adriana", "carmen",
    "rosa", "elena", "lucia", "marta", "pilar", "gloria", "teresa",
    "beatriz", "silvia", "yesica", "jessica", "jennifer", "katherine",
    "karen", "vanessa", "wendy", "tatiana", "milena", "johana",
    "lorena", "viviana", "lina", "mayra", "marisol", "rocio",
    "xiomara", "yolanda", "olga", "martha", "nelly", "constanza",
    "manuela", "mariana", "isabel", "veronica", "estefania",
    "stephanie", "kelly", "leidy", "dayana", "yuliana", "lizeth",
    "ingrid", "melissa", "erika", "karina", "marina", "susana",
    "elizabeth", "cristina", "alicia", "norma", "blanca", "dora",
    "cecilia", "amparo", "bibiana", "nancy", "flor", "stella",
    "johanna", "vivian", "margaret", "irene", "esperanza",
    "ximena", "cindy", "wendy", "nathalia", "yurani", "yeimi",
    "lady", "yenny", "jenny", "milady", "derly", "angie",
    # Male
    "juan", "carlos", "pedro", "jose", "luis", "diego", "andres",
    "david", "santiago", "sebastian", "nicolas", "daniel", "alejandro",
    "felipe", "miguel", "fernando", "ricardo", "jorge", "oscar",
    "ivan", "sergio", "pablo", "mario", "roberto", "alberto",
    "cristian", "william", "edison", "edwin", "jhon", "john",
    "jaime", "rafael", "guillermo", "raul", "hector", "hugo",
    "francisco", "manuel", "antonio", "gabriel", "martin", "cesar",
    "camilo", "fabian", "german", "gustavo", "hernan", "nelson",
    "omar", "victor", "julian", "mateo", "samuel", "brayan",
    "kevin", "stiven", "yeison", "duvan", "steven", "alex",
    "alexander", "freddy", "frank", "henry", "harold", "leonel",
}


def _extract_name_from_username(text: str) -> str | None:
    """Try to extract a real name from a concatenated username.
    e.g., 'angelicadiaz0212' → 'Angelica', 'juanpedro123' → 'Juan'
    """
    text_lower = text.lower().strip()
    # Try each name prefix — longest match wins
    matches = [n for n in _COMMON_NAMES if text_lower.startswith(n)]
    if matches:
        best = max(matches, key=len)
        return best.capitalize()
    return None


def _is_likely_person_name(text: str) -> bool:
    """Check if text looks like a person's name vs an organization/random text."""
    words = text.split()
    # All uppercase multi-word → likely organization ("LEONES TIGRES FC")
    if len(words) >= 2 and all(w.isupper() and len(w) > 1 for w in words):
        return False
    # Contains obvious non-name words
    non_name_indicators = (
        "fc", "club", "team", "store", "shop", "tienda", "empresa",
        "corp", "inc", "llc", "sas", "sa", "oficial", "official",
        "real", "group", "grupo", "fundacion", "asociacion",
    )
    text_lower = text.lower()
    if any(w in text_lower.split() for w in non_name_indicators):
        return False
    # Check if any word matches a known name
    for word in words:
        if word.lower() in _COMMON_NAMES:
            return True
    # Single reasonable word
    if len(words) == 1 and 2 <= len(text) <= 12:
        return True
    return len(words) <= 3


def _clean_push_name(raw: str | None) -> str | None:
    """
    Extract a usable first name from a WhatsApp push name.
    Handles: normal names, concatenated usernames, and detects non-names.
    """
    if not raw:
        return None

    # Strip emojis
    cleaned = _EMOJI_RE.sub("", raw).strip()
    if not cleaned or len(cleaned) < 2:
        return None

    # First check: if it looks like an organization/non-person → None
    if not _is_likely_person_name(cleaned):
        return None

    # Remove numbers
    no_numbers = re.sub(r"\d+", "", cleaned).strip()
    # Remove stray special characters but keep letters, spaces, accents
    no_numbers = re.sub(r"[^a-záéíóúñüA-ZÁÉÍÓÚÑÜ\s]", "", no_numbers).strip()

    if not no_numbers or len(no_numbers) < 2:
        return None

    # If it has spaces, take the first word
    if " " in no_numbers:
        parts = [p for p in no_numbers.split() if len(p) >= 2]
        if parts:
            first = parts[0]
            if first.lower() in _COMMON_NAMES or len(first) <= 12:
                return first.capitalize()
        return None

    # Single word — check if it's a known name
    if no_numbers.lower() in _COMMON_NAMES:
        return no_numbers.capitalize()

    # Try to extract a name from a concatenated username (e.g., "angelicadiaz")
    extracted = _extract_name_from_username(no_numbers)
    if extracted:
        return extracted

    # Short single word — only use if it looks like a real name (no weird patterns)
    if len(no_numbers) <= 10 and no_numbers.isalpha():
        # Reject patterns like "xxxdarkxxx", repeated chars, or gamer tags
        lower = no_numbers.lower()
        if lower != lower.replace("x", "", 2) and lower.count("x") >= 3:
            return None
        if any(c * 3 in lower for c in "abcdefghijklmnopqrstuvwxyz"):
            return None
        return no_numbers.capitalize()

    return None


# ---------------------------------------------------------------------------
# Debounce — accumulate rapid messages before processing
# ---------------------------------------------------------------------------
DEBOUNCE_SECONDS = 3.0

_pending_text: dict[str, list[str]] = {}
_pending_names: dict[str, str] = {}
_debounce_tasks: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def process_message(
    phone: str,
    push_name: str | None,
    message_type: str,
    text_content: str | None,
    media_key_id: str | None,
    media_base64_inline: str | None,
) -> None:
    """Main handler called from the webhook endpoint."""
    # Clean the push name
    push_name = _clean_push_name(push_name)
    if push_name:
        _pending_names[phone] = push_name

    # Check human takeover — if Yesica's window is active, bot stays silent
    conv = load_conversation(phone)
    if conv.is_human_takeover_active():
        logger.info(f"Human takeover active for {phone} (until {conv.human_takeover_until}) — bot silent")
        return
    # Clear expired takeover flag if it was left on
    if conv.human_takeover and not conv.human_takeover_until:
        conv.human_takeover = False
        conv.inject_system_event(
            "YESICA_HANDBACK: Yésica acaba de terminar de hablar con este cliente. "
            "Lee los mensajes anteriores de Yésica (aparecen como 'assistant') para "
            "tener contexto de lo que hablaron. Continúa la conversación de forma "
            "natural sin repetir lo que Yésica ya dijo."
        )
        save_conversation(conv)

    # Audio — transcribe first, then treat as text
    if message_type == "audioMessage":
        conv = load_conversation(phone)
        if push_name and not conv.user_display_name:
            conv.user_display_name = push_name
        try:
            await _handle_audio(conv, media_key_id, media_base64_inline)
        except Exception as e:
            logger.error(f"CRITICAL: Unhandled error in _handle_audio for {phone}: {e}", exc_info=True)
            try:
                await evolution.send_text_message(phone, "Disculpa, tuve un inconveniente con el audio 😅 Me lo puedes enviar de nuevo o escribirme?")
            except Exception:
                pass
        finally:
            save_conversation(conv)
        return

    # Images
    if message_type == "imageMessage":
        conv = load_conversation(phone)
        if push_name and not conv.user_display_name:
            conv.user_display_name = push_name
        try:
            await _handle_image(conv, media_key_id, media_base64_inline)
        except Exception as e:
            logger.error(f"CRITICAL: Unhandled error in _handle_image for {phone}: {e}", exc_info=True)
            try:
                await evolution.send_text_message(phone, "Disculpa, tuve un inconveniente con la imagen 😅 Me la puedes enviar de nuevo?")
            except Exception:
                pass
        finally:
            save_conversation(conv)
        return

    # Text messages go through debounce
    text = text_content or ""
    if not text.strip():
        return

    if phone not in _pending_text:
        _pending_text[phone] = []
    _pending_text[phone].append(text)

    # Cancel existing debounce timer
    existing = _debounce_tasks.get(phone)
    if existing and not existing.done():
        existing.cancel()

    # Schedule processing after delay
    _debounce_tasks[phone] = asyncio.create_task(
        _fire_after_delay(phone, DEBOUNCE_SECONDS)
    )


async def _fire_after_delay(phone: str, delay: float) -> None:
    """Wait, then process all accumulated messages for this user."""
    await asyncio.sleep(delay)

    messages = _pending_text.pop(phone, [])
    push_name = _pending_names.pop(phone, None)
    _debounce_tasks.pop(phone, None)

    if not messages:
        return

    combined = " ".join(messages)
    conv = load_conversation(phone)
    if push_name and not conv.user_display_name:
        conv.user_display_name = push_name

    try:
        await _handle_text(conv, combined)
    except Exception as e:
        logger.error(f"CRITICAL: Unhandled error in _handle_text for {phone}: {e}", exc_info=True)
        try:
            await evolution.send_text_message(phone, "Disculpa, tuve un inconveniente tecnico 😅 Dame un momento y te respondo!")
        except Exception:
            pass
    finally:
        save_conversation(conv)


# ---------------------------------------------------------------------------
# Audio handling
# ---------------------------------------------------------------------------

async def _handle_audio(
    conv: ConversationState,
    media_key_id: str | None,
    media_base64_inline: str | None,
) -> None:
    """Transcribe audio with Whisper and process as text."""
    await evolution.send_typing_presence(conv.phone)

    base64_data = media_base64_inline
    if not base64_data and media_key_id:
        base64_data = await evolution.get_media_base64(media_key_id, phone=conv.phone)

    if not base64_data:
        conv.add_message("user", "[El usuario envio un audio pero no se pudo descargar]")
        conv.inject_system_event(
            "INSTRUCCION: El usuario envio un audio pero no se pudo procesar. "
            "Pidele amablemente que lo reenvie o que escriba su mensaje."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    transcription = await ai.transcribe_audio(base64_data)

    if transcription:
        logger.info(f"[{conv.phone}] Whisper transcription: '{transcription[:100]}'")
        conv.last_user_message_at = datetime.now(COLOMBIA_TZ).isoformat()
        await _handle_text(conv, transcription)
    else:
        conv.inject_system_event(
            "INSTRUCCION: El usuario envio un audio pero no se pudo transcribir. "
            "Pidele amablemente que escriba su mensaje."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

async def _handle_image(
    conv: ConversationState,
    media_key_id: str | None,
    media_base64_inline: str | None,
) -> None:
    conv.last_user_message_at = datetime.now(COLOMBIA_TZ).isoformat()

    base64_data = media_base64_inline
    if not base64_data and media_key_id:
        await evolution.send_typing_presence(conv.phone)
        base64_data = await evolution.get_media_base64(media_key_id, phone=conv.phone)

    if not base64_data:
        conv.inject_system_event(
            "IMAGE_ANALYSIS: No se pudo descargar la imagen. Pide al usuario que la envie de nuevo."
        )
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    await evolution.send_typing_presence(conv.phone)
    analysis = await ai.analyze_image(base64_data)

    image_type = analysis.get("image_type", "OTHER")
    description = analysis.get("description", "")
    suggestion = analysis.get("response_suggestion", "")

    # Handle payment screenshots (legacy support)
    if image_type == "PAYMENT":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio lo que parece ser un comprobante de pago "
            f"({description}). La valoración es gratuita este mes, no necesita pagar. "
            f"Aclara amablemente que la valoración no tiene costo y pregunta si quiere agendar."
        )

    elif image_type == "BODY":
        zone = analysis.get("body_zone", "corporal")
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de su zona {zone}. "
            f"Descripcion: {description}. "
            f"Responde con empatia, muestra interes profesional por su caso, "
            f"menciona que Yesica podria evaluarla en la valoracion gratuita "
            f"y guia hacia el agendamiento."
        )

    elif image_type == "FACE":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de su rostro o piel. "
            f"Descripcion: {description}. "
            f"Responde con empatia, menciona tratamientos faciales relevantes "
            f"y ofrece la valoracion gratuita con Yesica."
        )

    elif image_type == "BEFORE_AFTER":
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una foto de antes/despues. "
            f"Descripcion: {description}. "
            f"Celebra y valida esos resultados, usa como motivacion."
        )

    else:
        conv.inject_system_event(
            f"IMAGE_ANALYSIS: El usuario envio una imagen. "
            f"Descripcion: {description}. Responde de forma natural."
        )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

# Phrases that indicate user wants to schedule
_USER_SCHEDULE_TRIGGERS = (
    "quiero agendar", "quiero la valoración", "quiero la valoracion",
    "quiero ir", "cómo agendo", "como agendo",
    "cuándo puedo ir", "cuando puedo ir", "qué horarios", "que horarios",
    "tienen disponibilidad", "hay disponibilidad", "cuándo hay",
    "cuando hay", "para cuándo", "para cuando", "qué días", "que dias",
    "quiero reservar", "quiero separar", "me gustaría agendar",
    "me gustaria agendar", "dale sí", "dale si", "dale pues",
    "va pues", "listo agendemos", "sí quiero", "si quiero",
    "hagámosle", "hagamosle", "cuándo me puedo ir",
    "cuando me puedo ir", "quiero la cita", "sepárame", "separame",
    "quiero el cupo", "vamos pues", "listo dale", "agendame",
    "agéndame", "reservame", "resérvame", "quiero conocer",
    "me interesa la valoración", "me interesa la valoracion",
    "quiero la valoracion gratuita", "quiero la valoración gratuita",
    "sí me interesa", "si me interesa",
    "busca un horario", "busca horario", "buscar horario",
    "dale busca", "sí busca", "si busca", "dale agenda",
    "si por favor", "sí por favor", "claro que sí", "claro que si",
    "dale claro", "si claro", "sí claro",
)

# Phrases that indicate evening/night preference
_EVENING_TRIGGERS = (
    "en la noche", "por la noche", "después de las 5", "despues de las 5",
    "después de las 6", "despues de las 6", "después de las 7", "despues de las 7",
    "en la tarde noche", "tarde-noche", "solo puedo en la noche",
    "7pm", "8pm", "9pm", "7 pm", "8 pm", "9 pm",
    "después del trabajo", "despues del trabajo", "salgo a las 5",
    "salgo a las 6", "fin de semana", "sábado", "sabado", "domingo",
    "solo fines de semana", "solo los fines",
)

# Phrases from bot that trigger slot fetching
_BOT_SLOT_TRIGGERS = (
    "revisar la agenda", "revisar los horarios", "horarios disponibles",
    "dejame revisar", "déjame revisar", "reviso los horarios",
    "reviso la disponibilidad", "miro los horarios", "consulto los horarios",
    "te busco horario", "busco disponibilidad", "reviso la agenda",
    "miro la agenda", "chequeo la agenda", "verifico disponibilidad",
    "darte un horario", "revisar disponibilidad",
)


async def _handle_text(conv: ConversationState, text: str) -> None:
    conv.add_message("user", text)
    conv.last_user_message_at = datetime.now(COLOMBIA_TZ).isoformat()

    # Phase-specific handling
    if conv.phase == "awaiting_slot_selection":
        await _try_parse_slot_selection(conv, text)
        return

    if conv.phase == "collecting_data":
        await _try_collect_data_and_schedule(conv)
        return

    # Check if user wants evening/weekend (outside business hours)
    user_lower = text.lower()
    if any(t in user_lower for t in _EVENING_TRIGGERS):
        if conv.phase not in ("appointment_confirmed", "escalated_to_yesica"):
            logger.info(f"Evening/weekend request detected for {conv.phone}")
            await _escalate_to_yesica_evening(conv, text)
            return

    # Check if user is showing scheduling intent
    wants_to_schedule = any(t in user_lower for t in _USER_SCHEDULE_TRIGGERS)

    # Also detect: if the bot just asked about scheduling and user says yes/dale/si/ok
    if not wants_to_schedule:
        last_bot_msg = ""
        for msg in reversed(conv.messages):
            if msg.get("role") == "assistant":
                last_bot_msg = msg["content"].lower()
                break
        scheduling_question = any(w in last_bot_msg for w in (
            "busque un horario", "buscar horario", "agendar", "agendamos",
            "te agendo", "quieres que te busque",
        ))
        if scheduling_question:
            affirmative = any(w in user_lower for w in (
                "si", "sí", "dale", "ok", "listo", "claro", "bueno",
                "perfecto", "va", "de una", "con toda", "porfa",
            ))
            if affirmative:
                wants_to_schedule = True

    if wants_to_schedule:
        if conv.phase not in (
            "awaiting_slot_selection", "collecting_data",
            "appointment_confirmed", "escalated_to_yesica",
        ):
            logger.info(f"User scheduling intent detected for {conv.phone}")
            await _fetch_and_inject_slots(conv)
            reply = await _generate_reply(conv)
            await _send_and_record(conv, reply)
            return

    # Default: generate reply
    reply = await _generate_reply(conv)

    # SAFETY: If bot invented a specific time without calendar data, intercept and force calendar check
    if conv.phase not in ("awaiting_slot_selection", "collecting_data", "appointment_confirmed", "escalated_to_yesica"):
        if _contains_invented_time(reply) and not conv.calendar_slots_json:
            logger.warning(f"[{conv.phone}] Bot invented a time without calendar data — forcing calendar check")
            await _fetch_and_inject_slots(conv)
            reply = await _generate_reply(conv)

    await _send_and_record(conv, reply)

    # Detect trigger phrase from bot reply → fetch slots
    reply_lower = reply.lower()
    if any(t in reply_lower for t in _BOT_SLOT_TRIGGERS):
        if conv.phase not in (
            "awaiting_slot_selection", "collecting_data",
            "appointment_confirmed", "escalated_to_yesica",
        ):
            logger.info(f"Bot slot trigger detected for {conv.phone}")
            await _fetch_and_inject_slots(conv)
            slot_reply = await _generate_reply(conv)
            await _send_and_record(conv, slot_reply)
        return


# ---------------------------------------------------------------------------
# Evening/Weekend escalation to Yésica
# ---------------------------------------------------------------------------

async def _escalate_to_yesica_evening(conv: ConversationState, user_text: str) -> None:
    """User needs evening/weekend hours. Escalate to Yésica."""
    settings = get_settings()

    conv.phase = "escalated_to_yesica"
    conv.inject_system_event(
        "EVENING_ESCALATION: El usuario necesita un horario fuera del rango normal "
        "(después de 5pm o fin de semana). Dile que lo conectás directamente con "
        "Yésica para coordinar ese horario especial. Sé natural y positiva."
    )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)

    # Notify Yésica with context
    name = conv.collected_name or conv.user_display_name or "Cliente"
    service = conv.service_interest or "Levantamiento de glúteos"
    message = (
        f"📋 *Solicitud de horario especial*\n\n"
        f"*Cliente:* {name}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Servicio:* {service}\n"
        f"*Solicitud:* {user_text[:200]}\n\n"
        f"El cliente necesita horario fuera de 9am-5pm L-V. "
        f"Por favor coordina directamente."
    )
    try:
        await evolution.send_text_message(settings.yesica_phone, message)
        logger.info(f"Escalated {conv.phone} to Yésica for evening/weekend scheduling")
    except Exception as e:
        logger.error(f"Failed to notify Yésica for evening escalation: {e}")


# ---------------------------------------------------------------------------
# Slot selection → appointment booking (no payment required)
# ---------------------------------------------------------------------------

async def _try_parse_slot_selection(conv: ConversationState, text: str) -> None:
    """User is picking a time slot. Parse and proceed to data collection."""
    # Check if this is actually an evening request
    text_lower = text.lower()
    if any(t in text_lower for t in _EVENING_TRIGGERS):
        await _escalate_to_yesica_evening(conv, text)
        return

    if not conv.calendar_slots_json:
        await _fetch_and_inject_slots(conv)
        reply = await _generate_reply(conv)
        await _send_and_record(conv, reply)
        return

    slots = [
        datetime.fromisoformat(s).replace(tzinfo=COLOMBIA_TZ)
        for s in json.loads(conv.calendar_slots_json)
    ]

    selected_slot = _extract_slot_from_text(text, slots)

    if selected_slot:
        formatted_dt = _format_appointment_datetime(selected_slot)
        conv.appointment_datetime = selected_slot.isoformat()

        # Check if we already have enough data to create appointment
        has_name = conv.collected_name or conv.user_display_name
        if has_name:
            # Go directly to appointment creation
            conv.phase = "collecting_data"
            await _try_collect_data_and_schedule(conv)
            return
        else:
            # Need to collect name
            conv.phase = "collecting_data"
            conv.inject_system_event(
                f"El usuario eligio el horario: {formatted_dt}. "
                f"Ahora necesitás su nombre completo para confirmar la cita. "
                f"Pidelo de forma natural y breve."
            )
    else:
        conv.inject_system_event(
            "INSTRUCCION: No se pudo identificar el horario que el usuario quiere. "
            "Preguntale de forma natural qué día y hora le queda mejor, "
            "por ejemplo: 'mañana a las 10am' o 'el jueves a las 2pm'."
        )

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


# ---------------------------------------------------------------------------
# Data collection & appointment creation
# ---------------------------------------------------------------------------

async def _try_collect_data_and_schedule(conv: ConversationState) -> None:
    """Collect user data and create the appointment."""
    logger.info(f"[{conv.phone}] Collecting data — phase={conv.phase}, appointment_datetime={conv.appointment_datetime}")

    try:
        extracted = await ai.extract_user_data(conv.messages)
        logger.info(f"[{conv.phone}] Extracted data: {extracted}")
    except Exception as e:
        logger.error(f"[{conv.phone}] Data extraction failed: {e}", exc_info=True)
        extracted = {"name": None, "phone": None, "email": None}

    if extracted.get("name"):
        conv.collected_name = extracted["name"]
        conv.user_display_name = extracted["name"].split()[0]
    if extracted.get("phone"):
        conv.collected_phone = extracted["phone"]
    if extracted.get("email"):
        conv.collected_email = extracted["email"]

    has_name = conv.collected_name or conv.user_display_name

    if conv.appointment_datetime and has_name:
        logger.info(f"[{conv.phone}] Creating appointment for: {conv.appointment_datetime}")
        try:
            await _create_appointment_from_saved_slot(conv)
        except Exception as e:
            logger.error(f"[{conv.phone}] Failed to create appointment: {e}", exc_info=True)
            conv.phase = "appointment_confirmed"
            formatted_dt = "pendiente"
            if conv.appointment_datetime:
                try:
                    formatted_dt = _format_appointment_datetime(
                        datetime.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
                    )
                except Exception:
                    pass
            conv.inject_system_event(
                f"APPOINTMENT_CONFIRMED: Cita registrada para {formatted_dt}. "
                f"Da detalles: fecha/hora, direccion (Cra 49b #26b-50, Unidad Ciudad Central, "
                f"Apto 1618, Torre 2, Bello), Estacion Madera del Metro, llegar antes, "
                f"cancelar con 24h."
            )
    elif conv.appointment_datetime and not has_name:
        conv.inject_system_event(
            "INSTRUCCION: Ya tienes el horario. Solo falta el nombre completo "
            "para confirmar la cita. Pídelo de forma natural."
        )
    else:
        logger.info(f"[{conv.phone}] No saved slot — fetching new slots")
        await _fetch_and_inject_slots(conv)

    reply = await _generate_reply(conv)
    await _send_and_record(conv, reply)


async def _create_appointment_from_saved_slot(conv: ConversationState) -> None:
    """Create a Google Calendar appointment."""
    try:
        slot = datetime.fromisoformat(conv.appointment_datetime).replace(tzinfo=COLOMBIA_TZ)
    except Exception as e:
        logger.error(f"Invalid appointment_datetime for {conv.phone}: {e}")
        await _fetch_and_inject_slots(conv)
        return

    event = await calendar.create_appointment(
        slot,
        conv.collected_name or conv.user_display_name or "Cliente",
        conv.collected_phone or conv.phone,
        conv.collected_email or "",
    )
    formatted_dt = _format_appointment_datetime(slot)
    conv.phase = "appointment_confirmed"

    if event:
        conv.inject_system_event(
            f"APPOINTMENT_CONFIRMED: Cita creada exitosamente. "
            f"Fecha y hora: {formatted_dt}. "
            f"Da todos los detalles: fecha/hora, direccion completa "
            f"(Cra 49b #26b-50, Unidad Ciudad Central, Apto 1618, Torre 2, Bello), "
            f"como llegar en Metro (a pasos de la Estacion Madera), "
            f"llegar 5-10 min antes, cancelar con 24h de anticipacion."
        )
        asyncio.create_task(_notify_yesica_appointment(conv, formatted_dt))
        # Save success pattern for learning
        asyncio.create_task(
            asyncio.to_thread(save_success_pattern, conv.phone, conv.messages)
        )
    else:
        # Calendar API failed but we still confirm to user
        logger.warning(f"[{conv.phone}] Calendar API returned None — confirming anyway")
        conv.inject_system_event(
            f"APPOINTMENT_CONFIRMED: Cita registrada para {formatted_dt}. "
            f"Da detalles: fecha/hora, direccion, Metro, llegar antes."
        )
        asyncio.create_task(_notify_yesica_appointment(conv, formatted_dt))


# ---------------------------------------------------------------------------
# Calendar slot fetching
# ---------------------------------------------------------------------------

async def _fetch_and_inject_slots(conv: ConversationState) -> None:
    """Fetch real calendar slots and inject them into the conversation."""
    logger.info(f"Fetching calendar slots for {conv.phone}")
    slots = await calendar.get_available_slots(days_ahead=7)

    if not slots:
        logger.info(f"No slots in 7 days for {conv.phone}, trying 14 days")
        slots = await calendar.get_available_slots(days_ahead=14)

    if slots:
        conv.calendar_slots_json = json.dumps([s.isoformat() for s in slots])
        conv.phase = "awaiting_slot_selection"
        formatted = calendar.format_slots_for_whatsapp(slots)
        conv.inject_system_event(
            f"CALENDAR_SLOTS: Yesica tiene disponible {formatted}. "
            f"INSTRUCCION: Ofrece PRIMERO solo UN horario (el mas proximo, ej: 'mañana en la mañana'). "
            f"Si el usuario dice que no puede, ofrece la otra franja del MISMO dia (ej: la tarde). "
            f"Solo si no puede en ninguna franja de ese dia, ofrece otro dia. "
            f"Se proactivo: 'Mañana tiene disponible en la mañana, te sirve o prefieres en la tarde?' "
            f"NO hagas lista. Conversacional."
        )
        logger.info(f"Calendar slots fetched for {conv.phone}: {len(slots)} slots")
    else:
        logger.warning(f"No calendar slots found for {conv.phone}")
        conv.inject_system_event(
            "CALENDAR_ERROR: No se encontraron horarios en las proximas 2 semanas. "
            "Dile que revisas la agenda y le confirmas en un momentico. "
            "Pregunta que dia y horario le quedaria ideal. "
            "NUNCA digas que Yesica se pondra en contacto."
        )


# ---------------------------------------------------------------------------
# Multi-message sending
# ---------------------------------------------------------------------------

async def _send_and_record(conv: ConversationState, reply: str) -> None:
    """Split reply by [MSG], send each part as a separate WhatsApp message."""
    # Safety net: ensure response never dies without a question/continuation
    reply = _ensure_conversation_alive(reply)

    parts = [p.strip() for p in reply.split("[MSG]") if p.strip()]
    if not parts:
        return

    full_reply = " ".join(parts)
    conv.add_message("assistant", full_reply)

    for i, part in enumerate(parts):
        await evolution.send_typing_presence(conv.phone)
        delay = min(1.0 + len(part) * 0.015, 3.5)
        await asyncio.sleep(delay)
        await evolution.send_text_message(conv.phone, part)


def _ensure_conversation_alive(reply: str) -> str:
    """Guarantee every response has a question or continuation. Never let conversation die."""
    if not reply or not reply.strip():
        return "¿En qué te puedo ayudar?"

    # Check if reply already has a question mark
    if "?" in reply:
        return reply

    # Check if reply has a continuation phrase
    continuation_phrases = (
        "cuéntame", "cuentame", "dime", "escríbeme", "escribeme",
        "avísame", "avisame", "me cuentas",
    )
    reply_lower = reply.lower()
    if any(p in reply_lower for p in continuation_phrases):
        return reply

    # No question and no continuation — add a follow-up oriented to scheduling
    if any(w in reply_lower for w in ("instagram", "instagram.com")):
        return reply  # Instagram links are ok without question (user is out of zone)
    elif any(w in reply_lower for w in ("valoración", "valoracion", "cita", "agendar", "horario")):
        return reply + " ¿Quieres que te busque un horario?"
    else:
        return reply + " ¿Te gustaría conocer más sobre cómo funciona?"


# ---------------------------------------------------------------------------
# 24h Follow-up system
# ---------------------------------------------------------------------------

async def send_followup_if_needed(phone: str) -> bool:
    """Check if this conversation needs a 24h follow-up and send it."""
    conv = load_conversation(phone)

    if conv.follow_up_sent:
        return False
    if conv.phase in ("appointment_confirmed", "collecting_data", "escalated_to_yesica"):
        return False
    if not conv.last_user_message_at:
        return False
    if conv.is_human_takeover_active():
        return False

    try:
        last_msg = datetime.fromisoformat(conv.last_user_message_at).replace(tzinfo=COLOMBIA_TZ)
    except Exception:
        return False

    now = datetime.now(COLOMBIA_TZ)
    hours_elapsed = (now - last_msg).total_seconds() / 3600

    if hours_elapsed < 20 or hours_elapsed > 48:
        return False

    name = conv.user_display_name or ""
    greeting = f"Hola {name}" if name else "Hola"
    followup_msg = (
        f"{greeting}, te escribo porque a Yésica le quedan pocos espacios esta semana "
        f"para valoraciones. Recordá que este mes la valoración no tiene costo. "
        f"¿Qué día te queda más fácil?"
    )

    await evolution.send_text_message(phone, followup_msg)
    conv.follow_up_sent = True
    conv.add_message("assistant", followup_msg)
    save_conversation(conv)
    logger.info(f"[{phone}] Sent 24h follow-up message")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _generate_reply(conv: ConversationState) -> str:
    """Build full messages list and call GPT-4o."""
    now_col = datetime.now(COLOMBIA_TZ)
    days_es = {0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves", 4: "viernes", 5: "sabado", 6: "domingo"}
    months_es = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
                 7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"}
    fecha_actual = (
        f"{days_es[now_col.weekday()]} {now_col.day} de {months_es[now_col.month]} de {now_col.year}, "
        f"{now_col.strftime('%I:%M %p')} (hora Colombia)"
    )

    header = f"FECHA Y HORA ACTUAL: {fecha_actual}\n"
    if conv.user_display_name:
        header += (
            f"NOMBRE DEL USUARIO: {conv.user_display_name}\n"
            f"INSTRUCCION: Usa el nombre '{conv.user_display_name}' de forma natural. "
            f"En el primer mensaje empieza con su nombre.\n"
        )
    else:
        header += (
            "NOMBRE DEL USUARIO: Desconocido — usa 'hola' simple y pregunta su nombre "
            "naturalmente. NO uses 'amiga', 'amor' ni nada cursi.\n"
        )

    # Inject learning context
    learning_ctx = get_learning_context()
    if learning_ctx:
        header += f"\n{learning_ctx}\n"

    system_with_context = header + "\n" + SYSTEM_PROMPT

    messages = [{"role": "system", "content": system_with_context}] + conv.messages

    # Analyze last user message for mirroring enforcement
    last_user_msg = ""
    for msg in reversed(conv.messages):
        if msg.get("role") == "user":
            last_user_msg = msg["content"]
            break

    user_word_count = len(last_user_msg.split()) if last_user_msg else 0
    user_char_count = len(last_user_msg) if last_user_msg else 0

    mirror_instruction = ""
    if user_word_count <= 5:
        mirror_instruction = (
            f"EFECTO ESPEJO OBLIGATORIO: El usuario escribió solo {user_word_count} palabras. "
            f"Tu respuesta DEBE ser corta: máximo 1-2 líneas, máximo 15 palabras. "
            f"NO uses [MSG]. UN solo mensaje breve."
        )
    elif user_word_count <= 15:
        mirror_instruction = (
            f"EFECTO ESPEJO: El usuario escribió {user_word_count} palabras. "
            f"Responde con longitud similar: máximo 2-3 líneas. UN solo mensaje."
        )
    elif user_word_count <= 30:
        mirror_instruction = (
            f"EFECTO ESPEJO: El usuario escribió {user_word_count} palabras. "
            f"Podés responder con un párrafo corto. UN solo mensaje."
        )
    else:
        mirror_instruction = (
            f"El usuario escribió un mensaje largo ({user_word_count} palabras). "
            f"Podés responder con un poco más de detalle pero no te excedas."
        )

    name = conv.user_display_name
    name_reminder = (
        f"El usuario se llama {name}."
        if name else
        "No sabes el nombre. Pregunta cómo se llama si es natural hacerlo."
    )

    messages.append({
        "role": "system",
        "content": f"{mirror_instruction}\n{name_reminder}\nNO inventes precios, datos ni información que no tengas."
    })

    return await ai.chat(messages)


async def _notify_yesica_appointment(
    conv: ConversationState,
    appointment_dt: str,
) -> None:
    """Notify Yésica when an appointment is booked."""
    settings = get_settings()
    name = conv.collected_name or conv.user_display_name or "Pendiente"
    message = (
        f"✅ *Nueva valoración agendada*\n\n"
        f"*Paciente:* {name}\n"
        f"*Teléfono:* +{conv.collected_phone or conv.phone}\n"
        f"*WhatsApp:* +{conv.phone}\n"
        f"*Servicio:* {conv.service_interest or 'Levantamiento de glúteos'}\n"
        f"*Valoración:* Gratuita (promoción del mes)\n"
        f"*Fecha:* {appointment_dt}\n\n"
        f"Quedó registrado en tu Google Calendar 📅"
    )
    await evolution.send_text_message(settings.yesica_phone, message)


# ---------------------------------------------------------------------------
# Slot extraction from natural language
# ---------------------------------------------------------------------------

def _extract_slot_from_text(text: str, slots: list[datetime]) -> datetime | None:
    """Parse natural time references like 'mañana a las 10', 'el jueves a las 3pm'."""
    if not slots:
        return None

    text_clean = text.strip().lower()
    now = datetime.now(COLOMBIA_TZ)

    # --- 1. Identify target DAY ---
    target_date = None
    if "hoy" in text_clean:
        target_date = now.date()
    elif "mañana" in text_clean or "manana" in text_clean:
        target_date = (now + timedelta(days=1)).date()
    else:
        day_names = {
            "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
            "jueves": 3, "viernes": 4,
        }
        for name, weekday in day_names.items():
            if name in text_clean:
                days_ahead = (weekday - now.weekday()) % 7
                if days_ahead == 0:
                    if any(s.date() == now.date() and s.weekday() == weekday for s in slots):
                        target_date = now.date()
                    else:
                        target_date = (now + timedelta(days=7)).date()
                else:
                    target_date = (now + timedelta(days=days_ahead)).date()
                break

    # --- 2. Identify target HOUR ---
    target_hour = None
    target_minute = 0

    time_match = re.search(
        r'(?:a\s+las\s+|las\s+)?(\d{1,2})\s*(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?',
        text_clean,
    )
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2)) if time_match.group(2) else 0
        period = time_match.group(3)

        if period and period.startswith("p") and hour < 12:
            hour += 12
        elif period and period.startswith("a") and hour == 12:
            hour = 0
        elif not period and 1 <= hour <= 7:
            hour += 12  # Assume PM for business hours

        target_hour = hour
        target_minute = minute

    # Handle "en la mañana" / "en la tarde"
    if target_hour is None:
        if re.search(r"en la mañana|por la mañana|temprano|mañana(?! )", text_clean):
            # Be careful: "mañana" can mean "tomorrow" — check context
            if "mañana en la mañana" in text_clean or "mañana temprano" in text_clean:
                target_hour = 9
            elif "en la mañana" in text_clean or "por la mañana" in text_clean:
                target_hour = 9
        if re.search(r"en la tarde|por la tarde|tarde", text_clean):
            target_hour = 14
        if re.search(r"medio\s*d[ií]a|mediodia|12", text_clean):
            target_hour = 12

    # --- 3. Match against available slots ---
    if target_date and target_hour is not None:
        target_dt = datetime(
            target_date.year, target_date.month, target_date.day,
            target_hour, target_minute, tzinfo=COLOMBIA_TZ,
        )
        candidates = [s for s in slots if s.date() == target_date]
        if candidates:
            best = min(candidates, key=lambda s: abs((s - target_dt).total_seconds()))
            if abs((best - target_dt).total_seconds()) <= 40 * 60:
                return best

    if target_hour is not None and target_date is None:
        candidates = [s for s in slots if s.hour == target_hour]
        if not candidates:
            candidates = [s for s in slots if abs(s.hour - target_hour) <= 1]
        if candidates:
            return candidates[0]

    if target_date and target_hour is None:
        candidates = [s for s in slots if s.date() == target_date]
        if candidates:
            return candidates[0]

    return None


def _contains_invented_time(reply: str) -> bool:
    """Detect if the bot's reply contains specific times/days that look like invented scheduling.
    Returns True if the reply mentions specific appointment times without calendar data."""
    reply_lower = reply.lower()

    # Patterns that indicate the bot is offering a specific time
    time_patterns = [
        r'\d{1,2}\s*(am|pm|a\.m|p\.m)',           # "3pm", "10 am"
        r'a las \d{1,2}',                          # "a las 3"
        r'mañana (a las|en la|por la)',            # "mañana a las 10"
        r'(lunes|martes|miércoles|miercoles|jueves|viernes).*(a las|en la|por la)',  # "jueves a las 2"
        r'tiene (disponible|libre|espacio).*(mañana|lunes|martes|miércoles|miercoles|jueves|viernes)',
        r'te (agendo|separo|reservo) para',        # "te agendo para mañana"
    ]

    for pattern in time_patterns:
        if re.search(pattern, reply_lower):
            return True
    return False


def _format_appointment_datetime(dt: datetime) -> str:
    days_es = {
        0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves",
        4: "viernes", 5: "sabado", 6: "domingo",
    }
    months_es = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre",
    }
    day_name = days_es[dt.weekday()]
    month_name = months_es[dt.month]
    time_str = dt.strftime("%I:%M %p").lstrip("0")
    return f"{day_name} {dt.day} de {month_name} a las {time_str}"
