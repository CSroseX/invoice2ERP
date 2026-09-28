from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices
from functools import lru_cache

class AppSettings(BaseSettings):
    primary_provider: str = Field(default="OpenRouter", exclude=True)
    
    # Model configs
    groq_api_key: str = Field(default="", validation_alias="GROQ_API_KEY")
    groq_model: str = Field(default="llama-3.3-70b-versatile", validation_alias="GROQ_MODEL")
    
    open_router_api_key: str = Field(default="", validation_alias=AliasChoices("OPEN_ROUTER_API", "OPENROUTER_API_KEY"))
    open_router_model: str = Field(default="meta-llama/llama-3.2-3b-instruct", validation_alias=AliasChoices("OPEN_ROUTER_MODEL", "OPENROUTER_MODEL"))
    
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.5-flash", validation_alias="GEMINI_MODEL")
    
    cloudflare_workers_ai_key: str = Field(default="", validation_alias=AliasChoices("CLOUDFLARE_WORKERS_AI", "CLOUDFLARE_API_KEY"))
    cloudflare_account_id: str = Field(default="", validation_alias="CLOUDFLARE_ACCOUNT_ID")
    cloudflare_model: str = Field(default="@cf/meta/llama-3.1-8b-instruct", validation_alias="CLOUDFLARE_MODEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

@lru_cache()
def get_settings() -> AppSettings:
    """Return a cached instance of the settings."""
    return AppSettings()

settings = get_settings()
