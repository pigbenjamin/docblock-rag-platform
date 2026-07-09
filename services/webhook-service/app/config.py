import os
from dataclasses import dataclass, field


def _parse_verify(value: str):
    """KEYCLOAK_VERIFY_SSL: true/false 或 CA bundle 檔案路徑（自簽憑證用）。"""
    s = (value or "").strip()
    if s.lower() in ("", "1", "true", "yes", "on"):
        return True
    if s.lower() in ("0", "false", "no", "off"):
        return False
    return s


@dataclass
class DBSettings:
    pg_dsn: str = os.getenv("PG_DSN", "dbname=acl_FIRDI user=ai-x password=changeme host=localhost port=5435")
    tenant_id: str = os.getenv("TENANT_ID", "firdi")


@dataclass
class KeycloakSettings:
    KEYCLOAK_URL: str = os.getenv("KEYCLOAK_URL", "https://host.docker.internal:8446")
    KEYCLOAK_REALM: str = os.getenv("KEYCLOAK_REALM", "FIRDI-AI-Platform")
    CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "user-sync-service")
    CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    VERIFY = _parse_verify(os.getenv("KEYCLOAK_VERIFY_SSL", "true"))


@dataclass
class AppSettings:
    keycloak: KeycloakSettings = field(default_factory=KeycloakSettings)
    db: DBSettings = field(default_factory=DBSettings)


settings = AppSettings()