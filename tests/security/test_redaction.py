from __future__ import annotations

import pytest

from backend.core.redaction import REDACTED, is_sensitive_path, redact_text

# Fake credentials assembled at runtime so secret scanners don't flag this file.
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
OPENAI = "sk-" + "proj-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2"


@pytest.mark.parametrize(
    "text",
    [
        f"key = '{AWS}'",
        f"token: {GH}",
        f"OPENAI_API_KEY={OPENAI}",
        "DATABASE_URL=postgresql://admin:S3cr3tPass@db.internal:5432/app",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        "client_secret = 'q8F!2xLm9Pz7Rk3T'",
        "conn = 'Endpoint=sb://x/;SharedAccessKey=abcDEF123456ghiJKL=='",
    ],
)
def test_secrets_are_redacted(text: str) -> None:
    result = redact_text(text)
    assert result.count >= 1
    assert REDACTED in result.text
    for secret in (
        AWS,
        GH,
        OPENAI,
        "S3cr3tPass",
        "q8F!2xLm9Pz7Rk3T",
        "abcDEF123456ghiJKL",
        "MIIEow",
    ):
        assert secret not in result.text


@pytest.mark.parametrize(
    "text",
    [
        "password = os.environ['DB_PASSWORD']",
        "api_key = settings.openai_api_key",
        "token = 'your-token-here'",
        "def get_token(user): return user.token",
        "for item in items: total += item.price",
        "SECRET_KEY = '<changeme>'",
    ],
)
def test_benign_code_is_untouched(text: str) -> None:
    result = redact_text(text)
    assert result.count == 0
    assert result.text == text


def test_url_credentials_keep_scheme_and_host() -> None:
    result = redact_text("git clone https://user:tok3n@github.com/o/r.git")
    assert result.text == f"git clone https://{REDACTED}@github.com/o/r.git"


@pytest.mark.parametrize(
    ("path", "sensitive"),
    [
        (".env", True),
        ("backend/.env.production", True),
        ("certs/server.pem", True),
        ("config/secrets.yaml", True),
        (".env.example", True),
        ("app/environment.py", False),
        ("README.md", False),
    ],
)
def test_sensitive_paths(path: str, sensitive: bool) -> None:
    assert is_sensitive_path(path) is sensitive
