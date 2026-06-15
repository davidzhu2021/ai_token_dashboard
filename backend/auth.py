import os
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request


SESSION_USER_KEY = "user"


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
    return {
        "email": normalized_email,
        "name": normalized_name,
        "avatar": initials(normalized_email, normalized_name),
        "department": (extra or {}).get("department", "研发中心"),
        "isAdmin": is_admin_email(normalized_email),
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
    user["isAdmin"] = is_admin_email(str(user.get("email", "")))
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if not user.get("isAdmin"):
        raise HTTPException(status_code=403, detail="当前账号没有管理员看板权限")
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
