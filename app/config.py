from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import default_sqlite_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = ""

    @field_validator("database_url", mode="before")
    @classmethod
    def _default_db(cls, v: str) -> str:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return default_sqlite_url()
        url = str(v).strip()
        # Liara app root is read-only; ./data in sqlite URL crashes startup → 502/503.
        if url.startswith("sqlite:") and ("./data" in url or "///./data" in url):
            return default_sqlite_url()
        return url
    workspace_api_key: str = "dev-local-key"
    liara_api_token: str = ""
    liara_api_base: str = "https://api.liara.ir"
    liara_team_id: str = ""
    liara_network_id: str = ""
    liara_network_name: str = "my-network"
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

    cursor_api_key: str = ""
    cursor_sdk_model: str = "composer-2.5"
    cursor_sdk_runtime: str = "local"  # local (default) | cloud
    cursor_cloud_branch: str = "main"
    cursor_cloud_auto_pr: bool = False
    cursor_sdk_auto_fix: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
