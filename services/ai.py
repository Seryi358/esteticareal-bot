import json
import logging
from openai import AsyncOpenAI
from config import get_settings
from bot.prompts import SYSTEM_PROMPT, PAYMENT_VERIFICATION_PROMPT, DATA_EXTRACTION_PROMPT

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=get_settings().openai_api_key)
    return _client


async def chat(messages: list[dict]) -> str:
    """
    Generate a human-like response using GPT-4o.
    messages: list of {role, content} — already includes system prompt as first message.
    """
    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.85,       # Slightly creative for natural conversation
            max_tokens=400,         # Keep responses short for WhatsApp
            presence_penalty=0.3,   # Avoid repetitive responses
            frequency_penalty=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI chat error: {e}")
        return "Disculpa, tuve un problema tecnico. Un momento y te respondo!"


async def verify_payment_image(base64_image: str) -> dict:
    """
    Analyze a payment screenshot using GPT-4o Vision.
    Returns dict with: is_valid, amount_detected, recipient_matches, appears_authentic, notes
    """
    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": PAYMENT_VERIFICATION_PROMPT,
                        },
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
            temperature=0.1,  # Low temperature for consistent verification
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Payment verification response was not valid JSON")
        return {
            "is_valid": False,
            "amount_detected": None,
            "recipient_matches": False,
            "appears_authentic": False,
            "notes": "No se pudo analizar la imagen correctamente",
        }
    except Exception as e:
        logger.error(f"Payment verification error: {e}")
        return {
            "is_valid": False,
            "amount_detected": None,
            "recipient_matches": False,
            "appears_authentic": False,
            "notes": "Error al analizar la imagen",
        }


async def extract_user_data(messages: list[dict]) -> dict:
    """
    Extract structured user data (name, phone, email) from recent conversation messages.
    """
    # Build a readable conversation excerpt from the last 10 messages
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
        raw = response.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Data extraction error: {e}")
        return {"name": None, "phone": None, "email": None}
