import os
import secrets
import bcrypt as _bcrypt
from itsdangerous import URLSafeSerializer, BadSignature

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")

_csrf_serializer = URLSafeSerializer(SECRET_KEY, salt="csrf")


def verify_admin(username: str, password: str) -> bool:
    if username != ADMIN_USERNAME:
        return False
    if not ADMIN_PASSWORD_HASH:
        return False
    try:
        return _bcrypt.checkpw(
            password.encode("utf-8"),
            ADMIN_PASSWORD_HASH.encode("utf-8"),
        )
    except Exception:
        return False


def generate_csrf_token(session_token: str) -> str:
    return _csrf_serializer.dumps(session_token)


def validate_csrf_token(token: str, session_token: str) -> bool:
    try:
        data = _csrf_serializer.loads(token)
        return data == session_token
    except (BadSignature, Exception):
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)
