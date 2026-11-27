from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)
    bot_token: str = ""
    admin_ids: str = ""
    database_url: str = "postgresql+asyncpg://goat:goat@localhost:5432/goat"
    redis_url: str = ""
    portals_api_key: str = ""
    portals_data_licensed: bool = False
    mrkt_api_key: str = ""
    app_url: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    port: int = 8000
    default_subscription_price_xtr: int | None = None
    purchase_approval_required: bool = True
    news_channel_url: str = ""
    environment: str = "development"
    run_background: bool = True

    @property
    def admins(self) -> frozenset[int]:
        return frozenset(int(value.strip()) for value in self.admin_ids.split(",") if value.strip())

    @property
    def dsn(self) -> str:
        return self.database_url.replace("postgres://", "postgresql+asyncpg://", 1).replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )
