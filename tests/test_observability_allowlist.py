from __future__ import annotations

import base64
import json
import os
from datetime import date, datetime, timezone
from typing import Any

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import SESSION_USER_KEY


class MinimalStabilityStore:
    async def stability_events(self, start_date: str, end_date: str, model: str = ""):
        return [
            {
                "backend_id": "primary",
                "request_id": "req-1",
                "event_time": datetime(2026, 8, 12, tzinfo=timezone.utc),
                "usage_date": date(2026, 8, 12),
                "model": "model-a",
                "status": "success",
                "scenario": "overload",
                "attempted_retries": 1,
                "ttft_ms": 800,
                "user_visible_failure": False,
                "error_code": "429",
                "collected_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
            }
        ]

    async def stability_sync_states(self):
        return [
            {
                "backend_id": "primary",
                "window_start": date(2026, 8, 6),
                "window_end": date(2026, 8, 12),
                "partial": False,
            }
        ]


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")


def _capabilities(me: dict[str, Any]) -> dict[str, bool]:
    caps = me.get("observabilityCapabilities") or {}
    return {
        "stabilityView": bool(caps.get("stabilityView")),
        "stabilityManage": bool(caps.get("stabilityManage")),
        "costView": bool(caps.get("costView")),
        "costManage": bool(caps.get("costManage")),
        "costReconcile": bool(caps.get("costReconcile")),
    }


def _enable_observability(monkeypatch, *, allowlist: str | None) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "zhuyida@auto-link.com.cn,leader@auto-link.com.cn")
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    if allowlist is None:
        monkeypatch.delenv("ADMIN_OBSERVABILITY_ALLOWED_EMAILS", raising=False)
    else:
        monkeypatch.setenv("ADMIN_OBSERVABILITY_ALLOWED_EMAILS", allowlist)


def test_observability_allowed_emails_parses_and_normalizes(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_OBSERVABILITY_ALLOWED_EMAILS", " Zhuyida@Auto-Link.com.cn , leader@auto-link.com.cn ")
    assert main.observability_allowed_emails() == {
        "zhuyida@auto-link.com.cn",
        "leader@auto-link.com.cn",
    }
    monkeypatch.delenv("ADMIN_OBSERVABILITY_ALLOWED_EMAILS", raising=False)
    assert main.observability_allowed_emails() == set()


def test_observability_dashboards_enabled_for_flag_and_allowlist(monkeypatch) -> None:
    zhuyida = {"email": "zhuyida@auto-link.com.cn", "isPlatformAdmin": True}
    leader = {"email": "leader@auto-link.com.cn", "isPlatformAdmin": True}
    member = {"email": "zhuyida@auto-link.com.cn", "isPlatformAdmin": False}

    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "false")
    monkeypatch.setenv("ADMIN_OBSERVABILITY_ALLOWED_EMAILS", "zhuyida@auto-link.com.cn")
    assert main.observability_dashboards_enabled_for(zhuyida) is False

    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    monkeypatch.setenv("ADMIN_OBSERVABILITY_ALLOWED_EMAILS", "zhuyida@auto-link.com.cn")
    assert main.observability_dashboards_enabled_for(zhuyida) is True
    assert main.observability_dashboards_enabled_for(leader) is False
    # A customer member must never inherit the seller preview boards.
    assert main.observability_dashboards_enabled_for(member) is False

    # Empty allowlist keeps the historical behavior: every platform admin.
    monkeypatch.delenv("ADMIN_OBSERVABILITY_ALLOWED_EMAILS", raising=False)
    assert main.observability_dashboards_enabled_for(leader) is True


def test_stability_overview_respects_preview_allowlist(monkeypatch) -> None:
    _enable_observability(monkeypatch, allowlist="zhuyida@auto-link.com.cn")
    monkeypatch.setattr(main, "usage_store", lambda: MinimalStabilityStore())
    fake_client = type("FakeClient", (), {"backends": [type("B", (), {"id": "primary"})()]})()
    monkeypatch.setattr(main, "client", lambda: fake_client)
    # Only the platform-admin gate is faked; the allowlist check runs for real.
    monkeypatch.setattr(
        main,
        "require_platform_admin",
        lambda request: {"email": "leader@auto-link.com.cn", "isPlatformAdmin": True},
    )
    client = TestClient(main.app)
    assert client.get("/api/admin/stability/overview").status_code == 404
    assert client.get("/api/admin/costs/overview").status_code == 404
    assert client.get("/api/admin/stability/actions").status_code == 404
    assert client.get("/api/admin/stability/regressions").status_code == 404

    monkeypatch.setattr(
        main,
        "require_platform_admin",
        lambda request: {"email": "zhuyida@auto-link.com.cn", "isPlatformAdmin": True},
    )
    assert client.get("/api/admin/stability/overview").status_code == 200
    assert client.get("/api/admin/stability/actions").status_code == 404
    assert client.get("/api/admin/stability/regressions").status_code == 404


def test_stability_overview_open_to_all_admins_when_allowlist_empty(monkeypatch) -> None:
    _enable_observability(monkeypatch, allowlist=None)
    monkeypatch.setattr(main, "usage_store", lambda: MinimalStabilityStore())
    fake_client = type("FakeClient", (), {"backends": [type("B", (), {"id": "primary"})()]})()
    monkeypatch.setattr(main, "client", lambda: fake_client)
    monkeypatch.setattr(
        main,
        "require_platform_admin",
        lambda request: {"email": "leader@auto-link.com.cn", "isPlatformAdmin": True},
    )
    client = TestClient(main.app)
    assert client.get("/api/admin/stability/overview").status_code == 200
    assert client.get("/api/admin/stability/actions").status_code == 404
    assert client.get("/api/admin/stability/regressions").status_code == 404


def test_stability_overview_stays_closed_when_master_flag_off(monkeypatch) -> None:
    _enable_observability(monkeypatch, allowlist="zhuyida@auto-link.com.cn")
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "false")
    monkeypatch.setattr(main, "usage_store", lambda: MinimalStabilityStore())
    fake_client = type("FakeClient", (), {"backends": [type("B", (), {"id": "primary"})()]})()
    monkeypatch.setattr(main, "client", lambda: fake_client)
    monkeypatch.setattr(
        main,
        "require_platform_admin",
        lambda request: {"email": "zhuyida@auto-link.com.cn", "isPlatformAdmin": True},
    )
    client = TestClient(main.app)
    assert client.get("/api/admin/stability/overview").status_code == 404


def _me_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "usage_store", lambda: MinimalStabilityStore())
    return TestClient(main.app)


def _logged_in_me(monkeypatch, email: str, name: str) -> dict[str, Any]:
    # 每个身份用独立的 TestClient：会话 cookie 一旦被 /api/auth/me 回写
    # （csrfToken），复用同一 client 会让下一个身份带着上一个身份的 cookie。
    client = _me_client(monkeypatch)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": email,
                    "name": name,
                    "avatar": name[:1],
                    "department": "研发中心",
                }
            }
        ),
    )
    return client.get("/api/auth/me").json()


def test_auth_me_capabilities_follow_allowlist(monkeypatch) -> None:
    _enable_observability(monkeypatch, allowlist="zhuyida@auto-link.com.cn")

    zhuyida = _logged_in_me(monkeypatch, "zhuyida@auto-link.com.cn", "朱奕达")
    assert zhuyida["isPlatformAdmin"] is True
    assert zhuyida["observabilityDashboardsEnabled"] is True
    assert _capabilities(zhuyida) == {
        "stabilityView": True,
        "stabilityManage": True,
        "costView": True,
        "costManage": True,
        "costReconcile": True,
    }

    # 其他全局管理员暂时看不到三个治理入口，导航与接口同时收口。
    leader = _logged_in_me(monkeypatch, "leader@auto-link.com.cn", "Leader")
    assert leader["isPlatformAdmin"] is True
    assert leader["observabilityDashboardsEnabled"] is False
    assert _capabilities(leader) == {
        "stabilityView": False,
        "stabilityManage": False,
        "costView": False,
        "costManage": False,
        "costReconcile": False,
    }


def test_auth_me_capabilities_open_to_all_admins_when_allowlist_cleared(monkeypatch) -> None:
    _enable_observability(monkeypatch, allowlist=None)
    leader = _logged_in_me(monkeypatch, "leader@auto-link.com.cn", "Leader")
    assert leader["observabilityDashboardsEnabled"] is True
    assert _capabilities(leader) == {
        "stabilityView": True,
        "stabilityManage": True,
        "costView": True,
        "costManage": True,
        "costReconcile": True,
    }
