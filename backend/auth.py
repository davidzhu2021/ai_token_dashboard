import os
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request


SESSION_USER_KEY = "user"
SERVER_SESSION_KEY = "server_session_id"
CSRF_SESSION_KEY = "csrf_token"
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def validate_password(password: str) -> str:
    value = str(password or "")
    if len(value) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码至少需要 {PASSWORD_MIN_LENGTH} 个字符")
    if len(value) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"密码不能超过 {PASSWORD_MAX_LENGTH} 个字符")
    return value


def hash_password(password: str) -> str:
    """Hash with Argon2id when installed, otherwise use PBKDF2-SHA256."""
    import base64
    import hashlib
    import os
    try:
        from argon2 import PasswordHasher

        return PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).hash(validate_password(password))
    except ImportError:
        value = validate_password(password)
        salt = os.urandom(16)
        rounds = 600_000
        digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, rounds)
        encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
        encoded_digest = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"pbkdf2_sha256${rounds}${encoded_salt}${encoded_digest}"


def verify_password(password: str, password_hash: str) -> bool:
    import base64
    import hashlib
    import hmac

    value = str(password or "")
    encoded = str(password_hash or "")
    if not value or not encoded:
        return False
    if encoded.startswith("$argon2"):
        try:
            from argon2 import PasswordHasher
            from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

            return bool(PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).verify(encoded, value))
        except (ImportError, InvalidHashError, VerificationError, VerifyMismatchError, ValueError):
            return False
    if encoded.startswith("pbkdf2_sha256$"):
        try:
            _, rounds_text, salt_text, digest_text = encoded.split("$", 3)
            salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
            expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
            actual = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, int(rounds_text))
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False
    return False


def password_needs_rehash(password_hash: str) -> bool:
    if not str(password_hash or "").startswith("$argon2"):
        return True
    try:
        from argon2 import PasswordHasher

        return bool(PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2).check_needs_rehash(password_hash))
    except (ImportError, ValueError):
        return False


def generate_numeric_code(length: int = 6) -> str:
    import secrets

    size = max(4, min(int(length), 12))
    return f"{secrets.randbelow(10**size):0{size}d}"


def generate_auth_token(byte_length: int = 32) -> str:
    import secrets

    return secrets.token_urlsafe(max(16, int(byte_length)))


def hash_auth_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def get_server_session_token(request: Request) -> str | None:
    value = request.session.get(SERVER_SESSION_KEY)
    return str(value) if value else None


def set_server_session(request: Request, token: str, csrf_value: str | None = None) -> str:
    request.session.clear()
    request.session[SERVER_SESSION_KEY] = str(token)
    value = csrf_value or generate_auth_token(24)
    request.session[CSRF_SESSION_KEY] = value
    return value


def clear_server_session(request: Request) -> None:
    request.session.clear()


def csrf_token(request: Request) -> str:
    value = request.session.get(CSRF_SESSION_KEY)
    if value:
        return str(value)
    value = generate_auth_token(24)
    request.session[CSRF_SESSION_KEY] = value
    return value


def verify_csrf_token(request: Request, provided: str | None = None) -> bool:
    import hmac

    expected = request.session.get(CSRF_SESSION_KEY)
    candidate = provided if provided is not None else request.headers.get("X-CSRF-Token")
    return bool(expected and candidate and hmac.compare_digest(str(expected), str(candidate)))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_admin_email(email: str) -> bool:
    return email.strip().lower() in admin_emails()


def is_platform_admin_email(email: str) -> bool:
    """Return whether an enterprise identity is a seller-side platform admin.

    ``ADMIN_EMAILS`` predates the customer-organization console and remains its
    configuration source.  Keeping the naming distinction here prevents
    callers from accidentally treating a customer organization administrator
    as a platform operator.
    """

    return is_admin_email(email)


def initials(email: str, name: str | None = None) -> str:
    source = (name or email or "员工").strip()
    return source[:1].upper()


def display_name(email: str, name: str | None = None) -> str:
    if name:
        return name
    prefix = (email or "employee").split("@", 1)[0]
    parts = [part for part in prefix.replace("-", ".").replace("_", ".").split(".") if part]
    if not parts:
        return "员工"
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def normalize_user(email: str, name: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_email = email.strip().lower()
    normalized_name = display_name(normalized_email, name)
    is_platform_admin = is_platform_admin_email(normalized_email)
    return {
        "email": normalized_email,
        "name": normalized_name,
        "avatar": initials(normalized_email, normalized_name),
        "department": (extra or {}).get("department", "研发中心"),
        # ``isAdmin`` is retained for the legacy seller-side /api/admin/*
        # operations.  Customer organization roles are added by backend.main
        # and must never be inferred from this field.
        "isAdmin": is_platform_admin,
        "isPlatformAdmin": is_platform_admin,
    }


def allowed_email_domain() -> str:
    return os.getenv("ALLOWED_EMAIL_DOMAIN", "auto-link.com.cn").strip().lower()


def validate_company_email(email: str) -> str:
    normalized = email.strip().lower()
    domain = allowed_email_domain()
    if "@" not in normalized:
        raise HTTPException(status_code=400, detail="企业认证未返回有效邮箱")
    if domain and not normalized.endswith(f"@{domain}"):
        raise HTTPException(status_code=403, detail="当前账号不属于公司邮箱域，无法访问")
    return normalized


def claim_value(claims: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and value:
            return str(value)
    return None


def require_user(request: Request) -> dict[str, Any]:
    user = request.session.get(SESSION_USER_KEY)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录后再查看个人数据")
    # A password account with the same email as an enterprise account is a
    # separate identity. It must not inherit enterprise administrator access.
    is_platform_admin = False if user.get("authType") == "password" else is_platform_admin_email(str(user.get("email", "")))
    user["isAdmin"] = is_platform_admin
    user["isPlatformAdmin"] = is_platform_admin
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if not user.get("isAdmin"):
        raise HTTPException(status_code=403, detail="当前账号没有管理员看板权限")
    return user


def require_platform_admin(request: Request) -> dict[str, Any]:
    """Require the seller-side administrator role.

    This is intentionally separate from customer organization ``owner`` and
    ``admin`` roles, which are resolved by the organization store.
    """

    user = require_user(request)
    if not user.get("isPlatformAdmin"):
        raise HTTPException(status_code=403, detail="当前账号没有平台客户管理权限")
    return user


def build_oauth() -> OAuth:
    oauth = OAuth()
    issuer = os.getenv("OIDC_ISSUER_URL", "").strip()
    client_id = os.getenv("OIDC_CLIENT_ID", "").strip()
    client_secret = os.getenv("OIDC_CLIENT_SECRET", "").strip()
    if issuer and client_id and client_secret:
        metadata_url = issuer if issuer.endswith("/.well-known/openid-configuration") else issuer.rstrip("/") + "/.well-known/openid-configuration"
        oauth.register(
            name="company",
            server_metadata_url=metadata_url,
            client_id=client_id,
            client_secret=client_secret,
            client_kwargs={"scope": "openid email profile"},
        )
    return oauth


def oidc_configured() -> bool:
    return bool(
        os.getenv("OIDC_ISSUER_URL", "").strip()
        and os.getenv("OIDC_CLIENT_ID", "").strip()
        and os.getenv("OIDC_CLIENT_SECRET", "").strip()
    )
