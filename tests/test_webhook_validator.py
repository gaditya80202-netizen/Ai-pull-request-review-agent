import hashlib
import hmac

from backend.webhook_receiver.validator import valid_signature


SECRET = "unit-test-secret"


def sign(body: bytes) -> str:
    digest = hmac.new(
        SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes():
    body = b'{"action":"opened"}'
    assert valid_signature(SECRET, body, sign(body))


def test_modified_body_fails():
    body = b'{"action":"opened"}'
    assert not valid_signature(
        SECRET,
        b'{"action":"closed"}',
        sign(body),
    )


def test_missing_signature_fails():
    assert not valid_signature(SECRET, b"payload", None)
