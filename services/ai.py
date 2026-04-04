import base64
import io
import json
import logging
from openai import AsyncOpenAI
from config import get_settings
from bot.prompts import SYSTEM_PROMPT, IMAGE_ANALYSIS_PROMPT, DATA_EXTRACTION_PROMPT

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=get_settings().openai_api_key,
            timeout=30.0,  # 30s hard timeout — prevents hanging booking flow
        )
    return _client


async def chat(messages: list[dict]) -> str:
    """Generate a response using GPT-4o."""
    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.4,
            max_tokens=250,
            presence_penalty=0.3,
            frequency_penalty=0.3,
        )
        content = response.choices[0].message.content
        if not content:
            return "Disculpa, tuve un problema tecnico. Un momento y te respondo!"
        return content.strip()
    except Exception as e:
        logger.error(f"OpenAI chat error: {e}")
        return "Disculpa, tuve un problema tecnico. Un momento y te respondo!"


async def analyze_image(base64_image: str) -> dict:
    """
    Analyze any image sent by the user.
    Returns structured info: image_type, description, payment fields, body_zone, response_suggestion.
    """
    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_ANALYSIS_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            temperature=0.1,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        if not raw:
            raise ValueError("Empty content from OpenAI")
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        logger.error("Image analysis response was not valid JSON")
        return {
            "image_type": "OTHER",
            "description": "No se pudo analizar la imagen",
            "response_suggestion": "Responde de forma amigable que recibiste la imagen pero no pudiste verla bien",
        }
    except Exception as e:
        logger.error(f"Image analysis error: {e}")
        return {
            "image_type": "OTHER",
            "description": "Error al procesar imagen",
            "response_suggestion": "Pide al usuario que reenvie la imagen",
        }


async def transcribe_audio(base64_audio: str) -> str | None:
    """
    Transcribe a WhatsApp voice/audio message using OpenAI Whisper.
    base64_audio: base64-encoded OGG/MP4/MP3 audio data.
    Returns transcribed text in Spanish, or None on failure.
    """
    try:
        audio_bytes = base64.b64decode(base64_audio)
        # WhatsApp audios are typically OGG/Opus — Whisper handles this
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "audio.ogg"

        transcription = await get_client().audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="es",
        )
        text = transcription.text.strip()
        logger.info(f"Audio transcribed: {text[:80]}...")
        return text
    except Exception as e:
        logger.error(f"Audio transcription error: {e}")
        return None


async def extract_name_from_pushname(push_name: str) -> str | None:
    """Use GPT to extract a real first name from a WhatsApp push name.
    Returns the name capitalized, or None if it's not a person's name."""
    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": (
                f"Analiza este nombre de perfil de WhatsApp: \"{push_name}\"\n\n"
                "Determina si contiene un nombre real de persona. Ejemplos:\n"
                "- 'angelicadiaz0212' → 'Angelica'\n"
                "- 'Maria Jose Lopez' → 'Maria'\n"
                "- 'LEONES TIGRES FC' → null (no es persona)\n"
                "- 'juanpedro123' → 'Juan'\n"
                "- 'tienda_online' → null (no es persona)\n"
                "- 'laura 💕✨' → 'Laura'\n"
                "- 'solo llamadas de emergenc' → null (no es nombre)\n"
                "- '.' → null\n"
                "- 'Dr. Martinez' → 'Martinez'\n\n"
                "Responde SOLO con JSON: {\"name\": \"Nombre\" } o {\"name\": null}"
            )}],
            temperature=0.0,
            max_tokens=50,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        if not raw:
            return None
        result = json.loads(raw.strip())
        name = result.get("name")
        return name if name else None
    except Exception as e:
        logger.error(f"Push name extraction error: {e}")
        return None


async def parse_slot_selection(user_message: str, available_slots: list[str], current_datetime: str, conversation_context: str = "") -> str | None:
    """Use GPT to understand which slot the user wants from their natural language.
    Returns the ISO datetime string of the selected slot, or None."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    COLOMBIA_TZ = ZoneInfo("America/Bogota")

    def _fmt_time_es(dt: datetime) -> str:
        """Format time in Spanish: '4:30 p.m.', '9:00 a.m.'"""
        hour = dt.hour
        period = "a.m." if hour < 12 else "p.m."
        h = 12 if hour == 0 else (hour - 12 if hour > 12 else hour)
        minute = dt.strftime("%M")
        return f"{h}:{minute} {period}"

    days_es = {0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves", 4: "viernes", 5: "sábado", 6: "domingo"}

    # Build human-readable list — pick first 3 slots per day to keep it compact but cover all days
    from collections import defaultdict
    slots_by_day = defaultdict(list)
    for iso in available_slots:
        day_key = iso[:10]  # "2026-03-27"
        slots_by_day[day_key].append(iso)

    slot_lines = []
    for day_key in sorted(slots_by_day.keys()):
        day_slots = slots_by_day[day_key]
        # Include first, middle, and last slot of each day
        indices = [0]
        if len(day_slots) > 2:
            indices.append(len(day_slots) // 2)
        if len(day_slots) > 1:
            indices.append(len(day_slots) - 1)
        for idx in indices:
            iso = day_slots[idx]
            dt = datetime.fromisoformat(iso).replace(tzinfo=COLOMBIA_TZ)
            day_name = days_es[dt.weekday()]
            human = f"{day_name} {dt.day}/{dt.month} a las {_fmt_time_es(dt)}"
            slot_lines.append(f"- {human} → ISO: {iso}")
        # Also note total available
        first_dt = datetime.fromisoformat(day_slots[0]).replace(tzinfo=COLOMBIA_TZ)
        last_dt = datetime.fromisoformat(day_slots[-1]).replace(tzinfo=COLOMBIA_TZ)
        day_name = days_es[first_dt.weekday()]
        slot_lines.append(f"  ({day_name}: {len(day_slots)} slots de {_fmt_time_es(first_dt)} a {_fmt_time_es(last_dt)})")

    context_block = ""
    if conversation_context:
        context_block = f"""
Contexto de la conversación reciente (para entender restricciones del usuario):
{conversation_context}

"""

    prompt = f"""El usuario está eligiendo un horario para una cita. Analiza su mensaje y selecciona el horario más apropiado.

Fecha y hora actual: {current_datetime}
{context_block}Horarios disponibles:
{chr(10).join(slot_lines)}

Mensaje del usuario: "{user_message}"

Responde SOLO con JSON. El valor de "selected" debe ser el ISO EXACTO de la lista:
{{"selected": "2026-03-25T09:00:00-05:00"}}

Si el usuario no está eligiendo un horario o dice que NO puede en cierto horario, responde:
{{"selected": null}}

REGLAS:
- Solo puedes elegir un ISO que esté EN LA LISTA
- Si dice "en la mañana" elige el primer slot antes de 12 p.m. de ese día
- Si dice "en la tarde" elige el primer slot de 12 p.m. en adelante
- Si dice solo un día sin hora, elige el primer slot disponible de ese día
- Si el usuario dice que NO puede en cierto horario/día, responde null — NO elijas ese horario
- Si el usuario dice que solo puede DESPUÉS de cierta hora (ej: "después de las 5", "puedo a partir de las 4"), y NO hay slots disponibles después de esa hora, responde null
- Si el usuario pide un horario fuera de 9 a.m. a 5 p.m. lunes a viernes (ej: noches, fines de semana), responde null
- Presta atención al contexto: si el usuario previamente dijo que no puede a cierta hora, respétalo
- NUNCA elijas un horario que contradiga las restricciones del usuario. Es preferible responder null que elegir un horario incorrecto"""

    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=100,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        if not raw:
            return None
        result = json.loads(raw.strip())
        selected = result.get("selected")
        if not selected:
            return None

        # Exact match
        if selected in available_slots:
            logger.info(f"GPT slot selection: '{user_message}' → {selected}")
            return selected

        # Fuzzy match — find closest slot to what GPT returned
        try:
            target_raw = datetime.fromisoformat(selected)
            # Use astimezone for tz-aware (safe if GPT changes offset), replace for naive
            target = target_raw.astimezone(COLOMBIA_TZ) if target_raw.tzinfo else target_raw.replace(tzinfo=COLOMBIA_TZ)
            best = None
            best_diff = float("inf")
            for iso in available_slots:
                slot_raw = datetime.fromisoformat(iso)
                slot_dt = slot_raw.astimezone(COLOMBIA_TZ) if slot_raw.tzinfo else slot_raw.replace(tzinfo=COLOMBIA_TZ)
                diff = abs((slot_dt - target).total_seconds())
                if diff < best_diff:
                    best_diff = diff
                    best = iso
            # Accept only within 30 minutes (one slot duration) to avoid
            # booking a significantly different time than what the user asked
            if best and best_diff <= 1800:
                logger.info(f"GPT slot fuzzy match: '{user_message}' → {best} (diff={best_diff}s)")
                return best
        except Exception:
            pass

        logger.warning(f"GPT returned slot not in list: {selected}")
        return None
    except Exception as e:
        logger.error(f"Slot parsing error: {e}")
        return None


async def interpret_confirmation(user_message: str, proposed_slot: str, conversation_context: str = "") -> str:
    """Use GPT to interpret if the user confirms, rejects, or is ambiguous about a proposed slot.
    Returns: 'yes', 'no', or 'ambiguous'."""
    context_block = ""
    if conversation_context:
        context_block = f"\nConversación reciente:\n{conversation_context}\n"

    prompt = f"""Eres asistente de una clínica estética en Colombia. El bot le propuso al usuario el horario: {proposed_slot}.

El usuario respondió: "{user_message}"
{context_block}
¿El usuario ACEPTA, RECHAZA, o su respuesta es AMBIGUA?

Ejemplos de ACEPTACIÓN: "sí", "dale", "listo", "perfecto", "de una", "ok", "claro", "bueno", "va", "eso", "me sirve", "está bien", "súper", "chevere", "hagale", "confirmame", "por favor", "seguro", "ese está bien", "a esa hora sí", "sí claro por favor"
Ejemplos de RECHAZO: "no", "no puedo", "ese día no", "mejor no", "no me sirve", "otro horario", "cambiar", "no me queda", "paso", "no gracias"
Ejemplos de AMBIGUO: "mmm", "déjame ver", "no sé", pregunta otra cosa sin relación, responde con información personal

Si el usuario dice "sí pero..." o acepta y agrega algo más, es ACEPTACIÓN.
Si el usuario pide un horario DIFERENTE al propuesto, es RECHAZO.

Responde SOLO con JSON: {{"decision": "yes"}} o {{"decision": "no"}} o {{"decision": "ambiguous"}}"""

    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=50,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        if not raw:
            return "ambiguous"
        result = json.loads(raw.strip())
        decision = result.get("decision", "ambiguous")
        if decision in ("yes", "no", "ambiguous"):
            logger.info(f"Confirmation interpreted: '{user_message}' → {decision}")
            return decision
        return "ambiguous"
    except Exception as e:
        logger.error(f"Confirmation interpretation error: {e}")
        return "ambiguous"


async def interpret_meeting_type(user_message: str) -> str | None:
    """Use GPT to interpret if the user prefers WhatsApp or Google Meet.
    Returns: 'whatsapp', 'meet', or None if ambiguous."""
    prompt = f"""El usuario debe elegir entre videollamada de WhatsApp o Google Meet para su cita.

El usuario respondió: "{user_message}"

¿Cuál prefiere?

- Si dice algo como "WhatsApp", "por whats", "por acá", "por aquí", "por este mismo", "videollamada normal" → whatsapp
- Si dice algo como "Meet", "Google Meet", "por meet", "enlace", "link", "por Google" → meet
- Si no queda claro → null

Responde SOLO con JSON: {{"choice": "whatsapp"}} o {{"choice": "meet"}} o {{"choice": null}}"""

    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=50,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        if not raw:
            return None
        result = json.loads(raw.strip())
        choice = result.get("choice")
        if choice:
            choice = str(choice).strip().lower()
        if choice in ("whatsapp", "meet"):
            logger.info(f"Meeting type interpreted: '{user_message}' → {choice}")
            return choice
        return None
    except Exception as e:
        logger.error(f"Meeting type interpretation error: {e}")
        return None


async def extract_user_data(messages: list[dict]) -> dict:
    """Extract structured user data (name, phone, email) from recent conversation messages."""
    recent = messages[-10:] if len(messages) > 10 else messages
    conversation_text = "\n".join(
        f"{'Usuario' if m['role'] == 'user' else 'Asistente'}: {m['content']}"
        for m in recent
        if m["role"] in ("user", "assistant")
    )

    prompt = DATA_EXTRACTION_PROMPT.format(conversation=conversation_text)
    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=150,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        if not raw:
            return {"name": None, "phone": None, "email": None}
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"Data extraction error: {e}")
        return {"name": None, "phone": None, "email": None}
