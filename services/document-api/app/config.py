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
    pg_dsn: str = os.getenv("PG_DSN", "dbname=acl_FIRDI user=ai-x password=changeme host=postgres port=5432")
    tenant_id: str = os.getenv("TENANT_ID", "firdi")


@dataclass
class AclSettings:
    admin_secret: str = os.getenv("ACL_ADMIN_SECRET", "")


@dataclass
class IngestWorkerSettings:
    url: str = os.getenv("INGEST_WORKER_URL", "http://ingest-worker:8762")


@dataclass
class KeycloakSettings:
    url: str = os.getenv("KEYCLOAK_URL", "https://host.docker.internal:8446")
    realm: str = os.getenv("KEYCLOAK_REALM", "FIRDI-AI-Platform")
    client_id: str = os.getenv("KEYCLOAK_CLIENT_ID", "user-sync-service")
    client_secret: str = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
    verify: object = field(default_factory=lambda: _parse_verify(os.getenv("KEYCLOAK_VERIFY_SSL", "true")))


@dataclass
class AppSettings:
    acl: AclSettings = field(default_factory=AclSettings)
    db: DBSettings = field(default_factory=DBSettings)
    ingest_worker: IngestWorkerSettings = field(default_factory=IngestWorkerSettings)
    keycloak: KeycloakSettings = field(default_factory=KeycloakSettings)


settings = AppSettings()
