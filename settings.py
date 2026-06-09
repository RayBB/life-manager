from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    grist_api_key: str
    grist_doc_id: str
    todoist_api_token: str
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()  # type: ignore
