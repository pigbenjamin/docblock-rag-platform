import os
from dataclasses import dataclass, field


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
class AppSettings:
    acl: AclSettings = field(default_factory=AclSettings)
    db: DBSettings = field(default_factory=DBSettings)
    ingest_worker: IngestWorkerSettings = field(default_factory=IngestWorkerSettings)


settings = AppSettings()
