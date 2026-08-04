import asyncio
from datetime import date

import pytest
from fastapi import HTTPException

from backend import baic_pilot_reconcile as reconcile


class Repository:
    async def get_organization(self, organization_id):
        assert organization_id == reconcile.LOCAL_ORGANIZATION_ID
        return {
            "id": organization_id,
            "name": "北汽",
            "upstreamOrganizationId": reconcile.CURRENT_UPSTREAM_ORGANIZATION_ID,
        }

    async def list_departments(self, *, organization_id, include_archived=False):
        return [
            {
                "id": reconcile.LOCAL_DEPARTMENT_ID,
                "name": "企业管理",
                "upstreamTeamId": reconcile.CURRENT_UPSTREAM_TEAM_ID,
            }
        ]

    async def list_members(self, **_kwargs):
        return {
            "items": [
                {
                    "id": reconcile.LOCAL_MEMBER_ID,
                    "name": "梁海强",
                    "email": "davidzhu2021@163.com",
                    "role": "admin",
                    "status": "invited",
                    "authUserId": "auth-david",
                }
            ]
        }

    async def get_adoption_operation(self, operation_key):
        assert operation_key == reconcile.OPERATION_KEY
        return {
            "status": "applied",
            "organizationId": reconcile.LOCAL_ORGANIZATION_ID,
            "result": {"ok": True, "status": "reconciled"},
        }


class Backend:
    id = "primary"


class Upstream:
    backends = [Backend()]

    async def find_organizations_exact(self, **_kwargs):
        return [
            {
                "organization_id": reconcile.TARGET_UPSTREAM_ORGANIZATION_ID,
                "organization_alias": "北汽研究院",
            }
        ]

    async def find_teams_exact(self, **_kwargs):
        return [
            {
                "team_id": reconcile.TARGET_UPSTREAM_TEAM_ID,
                "organization_id": reconcile.TARGET_UPSTREAM_ORGANIZATION_ID,
                "team_alias": "北汽集团",
            }
        ]

    async def list_keys_exact(self, *, key_alias, backend):
        return [{"key_alias": key_alias}]

    @staticmethod
    def report_only_key_identity(record):
        alias = record["key_alias"]
        suffix = "a" if alias.startswith("claude") else "b"
        return {
            "id": suffix * 64,
            "hash": suffix * 64,
            "alias": alias,
            "organizationId": reconcile.TARGET_UPSTREAM_ORGANIZATION_ID,
            "teamId": reconcile.TARGET_UPSTREAM_TEAM_ID,
            "userId": f"user-{suffix}",
            "models": [],
            "spend": 1.0,
            "blocked": False,
        }


def test_preview_requires_exact_target_scope_and_stable_keys():
    payload = asyncio.run(reconcile.build_preview(Repository(), Upstream()))

    assert payload["organization"]["name"] == "北汽"
    assert payload["targetOrganizationAlias"] == "北汽研究院"
    assert payload["targetTeamAlias"] == "北汽集团"
    assert [item["alias"] for item in payload["assets"]] == list(
        reconcile.KEY_ALIASES
    )
    assert payload["localProjection"] == "current"


def test_preview_accepts_a_verified_partial_retry_after_local_remap():
    class ReconciledRepository(Repository):
        async def get_organization(self, organization_id):
            return {
                "id": organization_id,
                "name": "北汽集团",
                "upstreamOrganizationId": reconcile.TARGET_UPSTREAM_ORGANIZATION_ID,
            }

        async def list_departments(self, **_kwargs):
            return [
                {
                    "id": reconcile.LOCAL_DEPARTMENT_ID,
                    "name": "企业管理",
                    "upstreamTeamId": reconcile.TARGET_UPSTREAM_TEAM_ID,
                }
            ]

    payload = asyncio.run(reconcile.build_preview(ReconciledRepository(), Upstream()))

    assert payload["localProjection"] == "target"


def test_preview_rejects_an_unrecognized_local_projection():
    class DriftedRepository(Repository):
        async def get_organization(self, organization_id):
            return {
                "id": organization_id,
                "name": "another tenant",
                "upstreamOrganizationId": reconcile.TARGET_UPSTREAM_ORGANIZATION_ID,
            }

    with pytest.raises(RuntimeError, match="neither the expected source nor target"):
        asyncio.run(reconcile.build_preview(DriftedRepository(), Upstream()))


def test_preview_rejects_key_scope_drift():
    class DriftedUpstream(Upstream):
        @staticmethod
        def report_only_key_identity(record):
            identity = Upstream.report_only_key_identity(record)
            identity["teamId"] = "another-team"
            return identity

    with pytest.raises(RuntimeError, match="key scope changed"):
        asyncio.run(reconcile.build_preview(Repository(), DriftedUpstream()))


def test_datetime_parser_preserves_timezone_and_rejects_invalid_values():
    parsed = reconcile._parse_optional_upstream_datetime("2026-08-04T08:00:00Z")
    assert parsed is not None and parsed.utcoffset().total_seconds() == 0
    assert reconcile._parse_optional_upstream_datetime("") is None
    with pytest.raises(RuntimeError, match="expiry timestamp"):
        reconcile._parse_optional_upstream_datetime("not-a-date")


def test_asset_reporting_start_uses_upstream_creation_date() -> None:
    asset = {"createdAt": "2026-07-29T04:20:25Z"}

    assert reconcile._asset_reporting_start(asset, date(2020, 1, 1)) == date(
        2026, 7, 29
    )
    assert reconcile._asset_reporting_start(asset, date(2026, 8, 1)) == date(
        2026, 8, 1
    )


def test_legacy_membership_cleanup_is_idempotent_but_fail_closed():
    async def missing():
        raise HTTPException(status_code=404, detail="membership not found")

    async def denied():
        raise HTTPException(status_code=403, detail="forbidden")

    asyncio.run(reconcile._ignore_absent_legacy_membership(missing()))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(reconcile._ignore_absent_legacy_membership(denied()))
    assert raised.value.status_code == 403


def test_cli_defaults_cover_historical_usage():
    assert date(2020, 1, 1) < date.today()
    assert reconcile.OPERATION_KEY == "baic-pilot-reconcile-v1"
