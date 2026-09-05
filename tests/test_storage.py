from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from pdf_to_anki.config import S3Settings
from pdf_to_anki.storage import (
    DECK_CONTENT_TYPE,
    PDF_CONTENT_TYPE,
    Storage,
    StorageError,
    build_client,
    deck_key,
    pdf_key,
)

BUCKET = "pdf-to-anki"
USER_ID = 4242
JOB_ID = "0f9c1b2a"


@pytest.fixture
def settings() -> S3Settings:
    return S3Settings(
        endpoint_url="http://minio:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
        bucket=BUCKET,
    )


@pytest.fixture
def storage(settings: S3Settings) -> Storage:
    return Storage(settings, client=build_client(settings))


@pytest.fixture
def stub(storage: Storage) -> Iterator[Stubber]:
    stubber = Stubber(storage.client)
    stubber.activate()
    yield stubber
    stubber.deactivate()


def body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


def test_key_layout_is_stable() -> None:
    assert pdf_key(USER_ID, JOB_ID) == "pdfs/4242/0f9c1b2a.pdf"
    assert deck_key(USER_ID, JOB_ID) == "decks/4242/0f9c1b2a.apkg"


def test_key_segments_reject_path_traversal() -> None:
    with pytest.raises(ValueError):
        pdf_key(USER_ID, "../../etc/passwd")


def test_ensure_bucket_creates_when_missing(storage: Storage, stub: Stubber) -> None:
    stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    stub.add_response("create_bucket", {}, {"Bucket": BUCKET})

    storage.ensure_bucket()
    stub.assert_no_pending_responses()


def test_ensure_bucket_is_a_noop_when_head_succeeds(storage: Storage, stub: Stubber) -> None:
    stub.add_response("head_bucket", {}, {"Bucket": BUCKET})

    storage.ensure_bucket()
    stub.assert_no_pending_responses()


# MinIO and AWS disagree on which of these a concurrent create returns, so both must pass.
@pytest.mark.parametrize("code", ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"])
def test_ensure_bucket_tolerates_existing_bucket(
    storage: Storage, stub: Stubber, code: str
) -> None:
    stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    stub.add_client_error("create_bucket", service_error_code=code, http_status_code=409)

    storage.ensure_bucket()
    stub.assert_no_pending_responses()


def test_ensure_bucket_reraises_other_create_errors(storage: Storage, stub: Stubber) -> None:
    stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
    stub.add_client_error("create_bucket", service_error_code="AccessDenied", http_status_code=403)

    with pytest.raises(StorageError, match="AccessDenied"):
        storage.ensure_bucket()


def test_put_pdf_sends_key_and_content_type(storage: Storage, stub: Stubber) -> None:
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": BUCKET,
            "Key": "pdfs/4242/0f9c1b2a.pdf",
            "Body": b"%PDF-1.7",
            "ContentType": PDF_CONTENT_TYPE,
        },
    )

    assert storage.put_pdf(USER_ID, JOB_ID, b"%PDF-1.7") == "pdfs/4242/0f9c1b2a.pdf"
    stub.assert_no_pending_responses()


def test_put_deck_bytes_uses_put_object(storage: Storage, stub: Stubber) -> None:
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": BUCKET,
            "Key": "decks/4242/0f9c1b2a.apkg",
            "Body": b"PK\x03\x04",
            "ContentType": DECK_CONTENT_TYPE,
        },
    )

    assert storage.put_deck(USER_ID, JOB_ID, b"PK\x03\x04") == "decks/4242/0f9c1b2a.apkg"
    stub.assert_no_pending_responses()


def test_put_deck_path_streams_instead_of_buffering(
    settings: S3Settings, tmp_path: Path
) -> None:
    apkg = tmp_path / "deck.apkg"
    apkg.write_bytes(b"PK\x03\x04" + b"\x00" * 1024)
    client = MagicMock()
    storage = Storage(settings, client=client)

    key = storage.put_deck(USER_ID, JOB_ID, apkg)

    assert key == "decks/4242/0f9c1b2a.apkg"
    client.upload_file.assert_called_once_with(
        str(apkg), BUCKET, key, ExtraArgs={"ContentType": DECK_CONTENT_TYPE}
    )
    client.put_object.assert_not_called()


def test_get_bytes_round_trips(storage: Storage, stub: Stubber) -> None:
    key = deck_key(USER_ID, JOB_ID)
    stub.add_response("get_object", {"Body": body(b"deck-bytes")}, {"Bucket": BUCKET, "Key": key})

    assert storage.get_bytes(key) == b"deck-bytes"


def test_exists_true(storage: Storage, stub: Stubber) -> None:
    key = pdf_key(USER_ID, JOB_ID)
    stub.add_response("head_object", {}, {"Bucket": BUCKET, "Key": key})

    assert storage.exists(key) is True


def test_exists_returns_false_on_404(storage: Storage, stub: Stubber) -> None:
    stub.add_client_error("head_object", service_error_code="404", http_status_code=404)

    assert storage.exists("decks/4242/missing.apkg") is False


def test_exists_raises_on_other_errors(storage: Storage, stub: Stubber) -> None:
    stub.add_client_error("head_object", service_error_code="AccessDenied", http_status_code=403)

    with pytest.raises(StorageError, match="AccessDenied"):
        storage.exists("decks/4242/forbidden.apkg")


def test_client_error_names_operation_and_key(storage: Storage, stub: Stubber) -> None:
    key = deck_key(USER_ID, JOB_ID)
    stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

    with pytest.raises(StorageError) as excinfo:
        storage.get_bytes(key)

    message = str(excinfo.value)
    assert "get_bytes" in message
    assert key in message
    assert "NoSuchKey" in message


def test_presigned_url_contains_key(storage: Storage) -> None:
    key = deck_key(USER_ID, JOB_ID)

    url = storage.presigned_url(key, expires_in=60)

    assert url.startswith("http://minio:9000/")
    assert f"{BUCKET}/{key}" in url  # path-style addressing, not bucket-as-subdomain
    assert "X-Amz-Expires=60" in url


def test_list_user_decks_follows_pagination(storage: Storage, stub: Stubber) -> None:
    prefix = "decks/4242/"
    stub.add_response(
        "list_objects_v2",
        {
            "Contents": [{"Key": f"{prefix}a.apkg"}, {"Key": f"{prefix}b.apkg"}],
            "IsTruncated": True,
            "NextContinuationToken": "page-2",
        },
        {"Bucket": BUCKET, "Prefix": prefix},
    )
    stub.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": f"{prefix}c.apkg"}], "IsTruncated": False},
        {"Bucket": BUCKET, "Prefix": prefix, "ContinuationToken": "page-2"},
    )

    assert storage.list_user_decks(USER_ID) == [
        f"{prefix}a.apkg",
        f"{prefix}b.apkg",
        f"{prefix}c.apkg",
    ]
    stub.assert_no_pending_responses()


def test_list_user_decks_handles_empty_prefix(storage: Storage, stub: Stubber) -> None:
    stub.add_response(
        "list_objects_v2", {"IsTruncated": False}, {"Bucket": BUCKET, "Prefix": "decks/4242/"}
    )

    assert storage.list_user_decks(USER_ID) == []
