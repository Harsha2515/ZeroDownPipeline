"""Runtime configuration, read once from the environment.

Every value has a default that works with docker-compose on the EC2 host, so a
missing variable degrades to "local dev" rather than crashing at import time.
"""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# Identity of this running container - set by deploy/deploy.py at docker run time.
APP_VERSION = os.getenv("APP_VERSION", "dev")
APP_COLOR = os.getenv("APP_COLOR", "blue")
APP_PORT = _int("APP_PORT", 8000)

# MySQL: writes go to the primary, reads can be served by the replica.
DB_HOST = os.getenv("DB_HOST", "mysql-primary")
DB_PORT = _int("DB_PORT", 3306)
DB_REPLICA_HOST = os.getenv("DB_REPLICA_HOST", "mysql-replica")
DB_REPLICA_PORT = _int("DB_REPLICA_PORT", 3306)
DB_NAME = os.getenv("MYSQL_DATABASE", "zerodown")
DB_USER = os.getenv("MYSQL_USER", "zerodown")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "zerodown")

# Redis cache in front of the redirect hot path.
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = _int("REDIS_PORT", 6379)
CACHE_TTL_SECONDS = _int("CACHE_TTL_SECONDS", 300)

# S3 holds the versioned audit copy of each record (not the source of truth).
S3_BUCKET = os.getenv("S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

BASE_URL = os.getenv("BASE_URL", "")
