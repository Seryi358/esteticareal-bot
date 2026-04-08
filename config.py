from pydantic_settings import BaseSettings
from pydantic import ConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8")

    # OpenAI
    openai_api_key: str

    # Evolution API
    evolution_api_url: str = ""
    evolution_api_key: str = ""
    evolution_instance: str = ""

    # Negocio
    yesica_phone: str = "573006278237"
    bot_phone: str = ""

    # Google Calendar
    google_calendar_id: str = "estetica.real.ai@gmail.com"
    google_token_json: str = ""  # base64-encoded token.json for production

    # Google Sheets
    google_sheet_id: str = ""

    # Base URL of this service (for constructing static file URLs)
    base_url: str = ""  # e.g. https://esteticareal-bot.easypanel.host

    # Media assets — public URLs for before/after photos and videos
    media_ficha_gluteos_url: str = ""   # Promo flyer — Plan Glúteos $350k (auto-built from base_url if empty)
    media_gluteos_url: str = ""         # Before/after photo — glúteos
    media_reduccion_url: str = ""       # Before/after photo — reducción de medidas
    media_facial_url: str = ""          # Before/after photo — limpieza facial
    media_consultorio_url: str = ""     # Photo of the consultorio
    media_video_yesica_url: str = ""    # Yésica presentation video (30-45 sec)
    media_video_proceso_url: str = ""   # Treatment process video (20-30 sec)

    # Paths
    conversations_dir: str = "data/conversations"
    credentials_dir: str = "credentials"


@lru_cache
def get_settings() -> Settings:
    return Settings()
