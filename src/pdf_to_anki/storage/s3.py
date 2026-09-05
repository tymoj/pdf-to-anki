from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

from ..config import S3Settings

logger = logging.getLogger(__name__)

# Object key layout. job_id is supplied by the caller (the worker mints it);
# storage never invents identifiers.
#     pdfs/{user_id}/{job_id}.pdf
#     decks/{user_id}/{job_id}.apkg
PDF_KEY_TEMPLATE = "pdfs/{user_id}/{job_id}.pdf"
DECK_KEY_TEMPLATE = "decks/{user_id}/{job_id}.apkg"
PDF_PREFIX_TEMPLATE = "pdfs/{user_id}/"
DECK_PREFIX_TEMPLATE = "decks/{user_id}/"

PDF_CONTENT_TYPE = "application/pdf"
DECK_CONTENT_TYPE = "application/octet-stream"

# head_object/head_bucket report a missing object as a bare HTTP status.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


class StorageError(RuntimeError):
    """Object storage failed. Carries the operation and key so callers can report it."""


def build_client(settings: S3Settings) -> BaseClient:
    # MinIO serves buckets as path segments, not as subdomains of the endpoint;
    # the default virtual-host addressing produces URLs it cannot route.
    config = Config(
        s3={"addressing_style": "path"},
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
    )
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name=settings.region,
        config=config,
    )


def pdf_key(user_id: int | str, job_id: str) -> str:
    return PDF_KEY_TEMPLATE.format(user_id=_segment(user_id), job_id=_segment(job_id))


def deck_key(user_id: int | str, job_id: str) -> str:
    return DECK_KEY_TEMPLATE.format(user_id=_segment(user_id), job_id=_segment(job_id))


class Storage:
    """Synchronous S3/MinIO wrapper. Pass `client` to inject a stub in tests."""

    def __init__(self, settings: S3Settings, client: BaseClient | None = None) -> None:
        self.settings = settings
        self.bucket = settings.bucket
        self.client = client if client is not None else build_client(settings)

    def ensure_bucket(self) -> None:
        """Create the bucket unless it is already there. Idempotent and race-safe."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return
        except ClientError as exc:
            logger.debug("head_bucket on %r failed (%s); creating", self.bucket, _code(exc))
        except (EndpointConnectionError, BotoCoreError) as exc:
            raise StorageError(
                f"ensure_bucket failed for bucket {self.bucket!r}: "
                f"cannot reach {self.settings.endpoint_url} ({exc})"
            ) from exc

        try:
            self.client.create_bucket(**self._create_bucket_kwargs())
            logger.info("Created bucket %r", self.bucket)
        except ClientError as exc:
            # AWS returns BucketAlreadyOwnedByYou, MinIO usually BucketAlreadyExists;
            # both mean another process won the race, which is fine.
            if _code(exc) in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                logger.debug("Bucket %r already exists", self.bucket)
                return
            raise _storage_error("ensure_bucket", f"bucket {self.bucket!r}", exc) from exc
        except (EndpointConnectionError, BotoCoreError) as exc:
            raise _storage_error("ensure_bucket", f"bucket {self.bucket!r}", exc) from exc

    def _create_bucket_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {"Bucket": self.bucket}
        # us-east-1 is the only region AWS rejects a LocationConstraint for.
        if self.settings.region and self.settings.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.settings.region}
        return kwargs

    def put_pdf(self, user_id: int | str, job_id: str, data: bytes) -> str:
        key = pdf_key(user_id, job_id)
        with _translate("put_pdf", key):
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=PDF_CONTENT_TYPE
            )
        logger.info("Stored PDF at %s (%d bytes)", key, len(data))
        return key

    def put_deck(self, user_id: int | str, job_id: str, path_or_bytes: Path | str | bytes) -> str:
        key = deck_key(user_id, job_id)
        with _translate("put_deck", key):
            if isinstance(path_or_bytes, (bytes, bytearray)):
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=bytes(path_or_bytes),
                    ContentType=DECK_CONTENT_TYPE,
                )
            else:
                # Streamed by the transfer manager: a deck with images can be large
                # and must never be slurped into memory just to hand it to put_object.
                self.client.upload_file(
                    str(path_or_bytes),
                    self.bucket,
                    key,
                    ExtraArgs={"ContentType": DECK_CONTENT_TYPE},
                )
        logger.info("Stored deck at %s", key)
        return key

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/json") -> str:
        with _translate("put_bytes", key):
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
            )
        logger.info("Stored object at %s (%d bytes)", key, len(data))
        return key

    def get_bytes(self, key: str) -> bytes:
        with _translate("get_bytes", key):
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES:
                return False
            raise _storage_error("exists", key, exc) from exc
        except (EndpointConnectionError, BotoCoreError) as exc:
            raise _storage_error("exists", key, exc) from exc

    def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        # Signed against settings.endpoint_url, so the link only resolves for clients
        # that can reach MinIO directly - inside a private compose network it cannot
        # be handed to a Telegram user unless the endpoint is published.
        with _translate("presigned_url", key):
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )

    def list_user_decks(self, user_id: int | str) -> list[str]:
        prefix = DECK_PREFIX_TEMPLATE.format(user_id=_segment(user_id))
        keys: list[str] = []
        with _translate("list_user_decks", prefix):
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys


def _segment(value: int | str) -> str:
    text = str(value).strip()
    if not text or "/" in text:
        raise ValueError(f"invalid key segment: {value!r}")
    return text


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "Unknown"))


def _storage_error(operation: str, target: str, exc: Exception) -> StorageError:
    if isinstance(exc, ClientError):
        return StorageError(f"{operation} failed for {target}: {_code(exc)} ({exc})")
    if isinstance(exc, EndpointConnectionError):
        return StorageError(f"{operation} failed for {target}: S3 endpoint unreachable ({exc})")
    return StorageError(f"{operation} failed for {target}: {exc}")


@contextmanager
def _translate(operation: str, target: str) -> Iterator[None]:
    try:
        yield
    except (ClientError, EndpointConnectionError, BotoCoreError) as exc:
        raise _storage_error(operation, target, exc) from exc
