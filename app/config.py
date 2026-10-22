from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/deployer.db"
    workspace_api_key: str = "dev-local-key"
    liara_api_token: str = ""
    liara_api_base: str = "https://api.liara.ir"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    github_token: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/auth/github/callback"
    github_sample_repo: str = "vercel/next.js"
    github_sample_path: str = "examples/hello-world"

    docker_sandbox: bool = True
    docker_executor_image: str = "deployer-executor:latest"


@lru_cache
def get_settings() -> Settings:
    return Settings()
