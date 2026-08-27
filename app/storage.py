import uuid
from functools import lru_cache

import boto3
from botocore.client import Config

from app.config import get_settings


@lru_cache
def _signing_client():
    """S3 client used only to sign URLs, never to transfer bytes.

    It is built against the *public* endpoint, because a presigned URL
    is signed for the host that will ultimately be called: the drone
    resolves `localhost:9000`, while the API container resolves
    `minio:9000`. Signing against the internal host would produce URLs
    that fail signature validation from outside the compose network.

    Signing is a local HMAC computation, so no request leaves the
    process here and the client never needs to reach MinIO.
    """
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_public_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        # MinIO serves path-style buckets; virtual-host style would
        # sign for a hostname that does not resolve locally.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name="us-east-1",
    )


def run_prefix(run_id: uuid.UUID) -> str:
    """All objects of a run live under one prefix.

    Keeps a run's data addressable as a unit, which is what makes a
    lifecycle rule or a bulk delete possible later on.
    """
    return f"runs/{run_id}/"


def presign_put(run_id: uuid.UUID, file_key: str) -> str:
    """Presigned PUT URL for a single file.

    Plain PUT, not multipart: files average ~2.2 MB here, far below the
    5 GB single-PUT ceiling and below the size where splitting starts
    to pay for itself.
    """
    settings = get_settings()
    return _signing_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.s3_bucket, "Key": f"{run_prefix(run_id)}{file_key}"},
        ExpiresIn=settings.presigned_url_ttl_seconds,
    )


def list_uploaded_keys(run_id: uuid.UUID) -> set[str]:
    """File keys actually present in the bucket for this run.

    The API never sees the image bytes, so a confirmation is only a
    claim. This is the independent check: at ~18 000 files a run, the
    listing is 18 paginated calls and a few seconds, against hours of
    upload, and it needs no queue, worker or redelivery handling.

    Called once per completion rather than continuously. Mid-run bucket
    state is nobody's concern; what has to be true is that a run marked
    COMPLETED really is complete.

    Keys come back without the run prefix, so they line up with the
    manifest the drone submitted.
    """
    settings = get_settings()
    prefix = run_prefix(run_id)
    found: set[str] = set()

    paginator = _signing_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            found.add(obj["Key"].removeprefix(prefix))
    return found
