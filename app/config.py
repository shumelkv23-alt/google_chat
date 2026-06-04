from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # БД
    database_url: str

    # OpenAI (embeddings)
    openai_api_key: str
    openai_embedding_model: str = "text-embedding-3-small"

    # OpenRouter (все LLM-задачи)
    openrouter_api_key: str = ""
    openrouter_model_prefilter: str = "deepseek/deepseek-v4-flash"
    openrouter_model_extract: str = "deepseek/deepseek-v4-flash"
    openrouter_model_resolve: str = "deepseek/deepseek-v4-flash"
    openrouter_model_answer: str = "deepseek/deepseek-v4-flash"
    openrouter_model_summarize: str = "deepseek/deepseek-v4-flash"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"

    # GCP
    gcp_project_id: str = ""
    gcp_project_number: str = ""
    google_pubsub_topic: str = ""

    # Google OAuth (для WE API)
    google_refresh_token: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""

    # Chat
    chat_a_space_id: str = ""
    chat_app_audience: str = ""  # = gcp_project_number

    # Email сервис-аккаунта, которым Pub/Sub подписывает push-OIDC токен.
    # Если задан — проверяем, что токен пришёл именно от него (defense-in-depth).
    pubsub_oidc_email: str = ""

    # FastAPI / ngrok
    app_base_url: str = ""

    # Отключить JWT-валидацию в локальной разработке
    skip_jwt_validation: bool = False


settings = Settings()
