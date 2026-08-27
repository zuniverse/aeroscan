from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, read from the environment.

    Every value has a development default so the stack boots with
    `docker compose up` and no manual setup. Production overrides
    them through the environment; see README, Production notes.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://aeroscan:aeroscan@db:5432/aeroscan"

    # S3 / MinIO
    s3_endpoint_url: str = "http://minio:9000"
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "inspection-runs"
    presigned_url_ttl_seconds: int = 3600

    # Request caps. Both are documented in DESIGN.md; exceeding them
    # returns 413 rather than silently truncating the batch.
    max_manifest_files: int = 50_000
    max_confirmations_per_batch: int = 1_000
    max_upload_urls_per_batch: int = 500

    # Sweep thresholds for abandoned runs. The first measures silence,
    # not elapsed time: 40 GB over a 5 Mbps site uplink is ~18 h of
    # legitimate uploading, so a duration-based cut would kill healthy
    # runs. A drone that is uploading at all confirms a batch well
    # inside two hours on any link; two hours of nothing means broken.
    uploading_idle_timeout_hours: int = 2
    incomplete_idle_timeout_days: int = 7

    # Sample of missing keys returned by completion. Enough for the
    # drone to act on, bounded so a run missing 18 000 files does not
    # produce an 18 000-entry error body.
    max_missing_keys_reported: int = 100

    # Verify completion against the bucket rather than trusting the
    # drone's confirmations. Switchable so tests and local runs can
    # skip the S3 round trip.
    verify_uploads_against_s3: bool = True

    # Backoffice / web app credential, distinct from drone keys: the
    # web app reads every site, a drone writes only its own runs.
    backoffice_api_key: str = "dev-backoffice-key"

    default_page_size: int = 20
    max_page_size: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()
