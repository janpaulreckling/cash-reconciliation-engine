"""Runtime settings, read from environment variables / .env (prefix RECON_)."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RECON_", env_file=".env", extra="ignore")

    duckdb_path: str = "./recon.duckdb"

    odoo_url: str = "http://localhost:8069"
    odoo_db: str = "northwind"
    odoo_username: str = "admin"
    odoo_password: SecretStr = SecretStr("admin")

    stripe_api_key: SecretStr | None = None
    stripe_account: str | None = None  # connected test merchant (acct_...)
    plaid_client_id: str | None = None
    plaid_secret: SecretStr | None = None
    plaid_access_token: SecretStr | None = None  # sandbox Item, written by `recon seed plaid`
