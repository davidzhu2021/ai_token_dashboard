import base64
import json
import os

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY
from backend.organization_store import InMemoryOrganizationStore


def signed_session(payload: dict) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")


def organization_client(monkeypatch, *, email: str, is_platform_admin: bool = False) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", email if is_platform_admin else "platform-admin@example.test")
    monkeypatch.setattr(main, "_organization_store", InMemoryOrganizationStore())
    client = TestClient(main.app)
    session = {
        SESSION_USER_KEY: {
            "email": email,
            "name": "Test User",
            "avatar": "T",
            "department": "Engineering",
            "isAdmin": is_platform_admin,
        },
        CSRF_SESSION_KEY: "organization-test-csrf",
    }
    client.cookies.set(main.SESSION_COOKIE_NAME, signed_session(session))
    return client


def write_headers() -> dict[str, str]:
    return {"X-CSRF-Token": "organization-test-csrf"}


def test_organization_demo_routes_are_hidden_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "false")
    client = TestClient(main.app)

    response = client.get("/api/organization/current")

    assert response.status_code == 404


def test_platform_admin_gets_synthetic_owner_and_can_manage_demo(monkeypatch) -> None:
    client = organization_client(monkeypatch, email="platform-admin@example.test", is_platform_admin=True)

    me = client.get("/api/auth/me")
    response = client.get("/api/organization/current")

    assert me.status_code == 200
    assert me.json()["organizationRole"] == "owner"
    assert me.json()["canManageOrganization"] is True
    assert me.json()["organizationDemoEnabled"] is True
    assert response.status_code == 200
    payload = response.json()
    assert payload["organization"]["isDemo"] is True
    assert payload["stats"]["departmentCount"] == 3
    assert payload["stats"]["memberCount"] == 12
    assert payload["currentMember"]["email"] == "platform-admin@example.test"
    assert payload["currentMember"]["role"] == "owner"


def test_seed_member_role_controls_read_and_write_access(monkeypatch) -> None:
    client = organization_client(monkeypatch, email="avery.chen@demo.example")

    me = client.get("/api/auth/me")
    current = client.get("/api/organization/current")
    members = client.get("/api/organization/current/members?search=Avery&page=1&pageSize=10")
    write = client.post(
        "/api/organization/current/departments",
        json={"name": "Customer Success"},
        headers=write_headers(),
    )

    assert me.json()["organizationRole"] == "member"
    assert me.json()["canManageOrganization"] is False
    assert current.status_code == 200
    assert current.json()["currentMember"]["id"] == "member-001"
    assert members.status_code == 200
    assert members.json()["total"] == 1
    assert write.status_code == 403
    assert write.json()["detail"]["code"] == "ORGANIZATION_MANAGE_FORBIDDEN"


def test_organization_mutations_require_csrf_and_return_public_records(monkeypatch) -> None:
    client = organization_client(monkeypatch, email="admin@demo.example")

    missing_csrf = client.post("/api/organization/current/departments", json={"name": "Customer Success"})
    created_department = client.post(
        "/api/organization/current/departments",
        json={"name": "Customer Success"},
        headers=write_headers(),
    )
    department = created_department.json()["department"]
    created_member = client.post(
        "/api/organization/current/members",
        json={
            "name": "New Administrator",
            "email": "new.admin@demo.example",
            "departmentId": department["id"],
            "role": "admin",
        },
        headers=write_headers(),
    )

    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "AUTH_CSRF_INVALID"
    assert created_department.status_code == 200
    assert department["name"] == "Customer Success"
    assert created_member.status_code == 200
    member = created_member.json()["member"]
    assert member["status"] == "invited"
    assert member["role"] == "admin"
    assert member["departmentId"] == department["id"]


def test_organization_validates_duplicates_and_protects_last_owner(monkeypatch) -> None:
    client = organization_client(monkeypatch, email="owner@demo.example")

    duplicate = client.post(
        "/api/organization/current/members",
        json={
            "name": "Duplicate Owner",
            "email": "OWNER@demo.example",
            "departmentId": "dept-engineering",
            "role": "member",
        },
        headers=write_headers(),
    )
    remove_last_owner = client.patch(
        "/api/organization/current/members/member-owner",
        json={"role": "member"},
        headers=write_headers(),
    )
    archive_live_department = client.post(
        "/api/organization/current/departments/dept-engineering/archive",
        json={},
        headers=write_headers(),
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "ORGANIZATION_MEMBER_EXISTS"
    assert remove_last_owner.status_code == 409
    assert remove_last_owner.json()["detail"]["code"] == "ORGANIZATION_CONFLICT"
    assert archive_live_department.status_code == 409
    assert archive_live_department.json()["detail"]["code"] == "ORGANIZATION_CONFLICT"


def test_demo_reset_restores_seed_data_and_member_filters(monkeypatch) -> None:
    client = organization_client(monkeypatch, email="owner@demo.example")

    added = client.post(
        "/api/organization/current/departments",
        json={"name": "Temporary Department"},
        headers=write_headers(),
    )
    listed = client.get("/api/organization/current/members?status=pending&role=admin&page=1&pageSize=10")
    reset = client.post("/api/organization/current/demo/reset", json={}, headers=write_headers())
    current = client.get("/api/organization/current")

    assert added.status_code == 200
    assert listed.status_code == 200
    assert all(member["status"] == "invited" for member in listed.json()["items"])
    assert all(member["role"] == "admin" for member in listed.json()["items"])
    assert reset.status_code == 200
    assert reset.json()["stats"]["departmentCount"] == 3
    assert current.json()["stats"]["departmentCount"] == 3
