import hashlib
import hmac


def valid_signature(
    secret: str,
    body: bytes,
    header: str | None,
) -> bool:
    if not header:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, header)
