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
        _client = AsyncOpenAI(api_key=get_settings().openai_api_key)
    return _client


async def chat(messages: list[dict]) -> str:
    """Generate a response using GPT-4o."""
    try:
        response = await get_client().chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.85,
            max_tokens=500,
            presence_penalty=0.3,
            frequency_penalty=0.2,
        )
        return response.choices[0].message.content.strip()
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
        raw = response.choices[0].message.content.strip()
        return json.loads(raw)
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
        raw = response.choices[0].message.content.strip()
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Data extraction error: {e}")
        return {"name": None, "phone": None, "email": None}
