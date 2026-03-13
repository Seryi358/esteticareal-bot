from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
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
    google_calendar_id: str = "primary"
    google_token_json: str = ""  # base64-encoded token.json for production

    # Paths
    conversations_dir: str = "data/conversations"
    credentials_dir: str = "credentials"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
