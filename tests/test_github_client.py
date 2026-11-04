import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.core.settings import settings
from backend.integrations.github_client import GitHubClient


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
async def test_post_review_rejects_non_comment_events(event):
    async with GitHubClient() as client:
        with pytest.raises(ValueError, match="Only COMMENT reviews"):
            await client.post_review(
                "owner/repo",
                1,
                "abc123",
                "Review body",
                event=event,
            )


@pytest.mark.asyncio
async def test_app_jwt_is_signed_with_rs256_and_has_valid_claims(
    tmp_path, monkeypatch
):
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    key_path = tmp_path / "test-private-key.pem"
    key_path.write_bytes(private_key_pem)

    monkeypatch.setattr(settings, "github_app_id", "12345")
    monkeypatch.setattr(
        settings, "github_private_key_path", str(key_path)
    )

    async with GitHubClient() as client:
        token = client._create_app_jwt()

    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["RS256"],
    )

    now = int(time.time())

    assert header["alg"] == "RS256"
    assert claims["iss"] == "12345"
    assert claims["iat"] <= now
    assert claims["exp"] > now
    assert claims["exp"] - claims["iat"] <= 600
