"""Safely reconcile the partially-created BAIC pilot in real mode.

The command is read-only unless ``--apply`` is supplied. It deliberately uses
stable upstream ids and key hashes, never aliases alone, for the write phase.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import date, datetime, timezone
from typing import Any

from .litellm_client import LiteLLMClient
from .organization_provisioning import OrganizationProvisioningService
from .organization_repository import PostgreSQLOrganizationRepository


LOCAL_ORGANIZATION_ID = "4b13ec57df104522a59ee910824c7e70"
LOCAL_DEPARTMENT_ID = "2a74de46f4d64263b5986be9d9072e52"
LOCAL_MEMBER_ID = "5e81d7dfd44a41d99f428f400f81bc32"
CURRENT_UPSTREAM_ORGANIZATION_ID = "48dba1f2-d0e2-40d7-b76c-05f00b4cac1c"
CURRENT_UPSTREAM_TEAM_ID = "02c69e29-f1e8-4b86-b1d0-30aaf9f07ee6"
TARGET_UPSTREAM_ORGANIZATION_ID = "org-baic-research-institute"
TARGET_UPSTREAM_TEAM_ID = "team-8656ed00614014a1"
KEY_ALIASES = ("claude-code-lianghaiqiang", "cursor-lianghaiqiang")
OPERATION_KEY = "baic-pilot-reconcile-v1"
BACKEND_ID = "primary"


def _is_current_local_projection(
    organization: dict[str, Any],
    department: dict[str, Any],
) -> bool:
    return (
        str(organization.get("name") or "") == "北汽"
        and str(organization.get("upstreamOrganizationId") or "")
        == CURRENT_UPSTREAM_ORGANIZATION_ID
        and str(department.get("upstreamTeamId") or "")
        == CURRENT_UPSTREAM_TEAM_ID
    )


def _is_target_local_projection(
    organization: dict[str, Any],
    department: dict[str, Any],
) -> bool:
    return (
        str(organization.get("name") or "") == "北汽集团"
        and str(organization.get("upstreamOrganizationId") or "")
        == TARGET_UPSTREAM_ORGANIZATION_ID
        and str(department.get("name") or "") == "企业管理"
        and str(department.get("upstreamTeamId") or "")
        == TARGET_UPSTREAM_TEAM_ID
    )


def _parse_optional_upstream_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("BAIC key expiry timestamp is invalid") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _asset_reporting_start(asset: dict[str, Any], requested_from: date) -> date:
    created_at = _parse_optional_upstream_datetime(asset.get("createdAt"))
    if created_at is None:
        return requested_from
    return max(requested_from, created_at.astimezone(timezone.utc).date())


def _record_id(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _member_ids(payload: dict[str, Any] | None) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    values: list[Any] = []
    for key in ("members", "members_with_roles", "organization_memberships", "team_memberships"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            values.extend(candidate)
    result: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        user_id = _record_id(item, "user_id", "userId", "id") or _record_id(
            user, "user_id", "userId", "id"
        )
        if user_id:
            result.add(user_id)
    return result


async def _ignore_absent_legacy_membership(operation: Any) -> None:
    try:
        await operation
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        detail = getattr(exc, "detail", "")
        if isinstance(detail, dict):
            detail = detail.get("error") or detail.get("message") or ""
        message = str(detail or exc).casefold()
        if status_code == 404 or "not found" in message or "does not belong" in message:
            return
        raise


async def build_preview(
    repository: PostgreSQLOrganizationRepository,
    upstream: LiteLLMClient,
) -> dict[str, Any]:
    organization = await repository.get_organization(LOCAL_ORGANIZATION_ID)
    departments = await repository.list_departments(
        organization_id=LOCAL_ORGANIZATION_ID, include_archived=True
    )
    members = await repository.list_members(
        organization_id=LOCAL_ORGANIZATION_ID,
        keyword="davidzhu2021@163.com",
        page=1,
        page_size=10,
    )
    department = next(
        (item for item in departments if str(item.get("id")) == LOCAL_DEPARTMENT_ID), None
    )
    member = next(
        (item for item in members.get("items", []) if str(item.get("id")) == LOCAL_MEMBER_ID),
        None,
    )
    if not organization or not department or not member:
        raise RuntimeError("BAIC local pilot objects are not the expected unique records")
    if _is_current_local_projection(organization, department):
        local_projection = "current"
    elif _is_target_local_projection(organization, department):
        local_projection = "target"
    else:
        raise RuntimeError("BAIC local pilot projection is neither the expected source nor target state")
    if (
        str(member.get("email") or "").casefold() != "davidzhu2021@163.com"
        or str(member.get("role") or "") != "admin"
    ):
        raise RuntimeError("BAIC administrator is not the expected David account")
    if local_projection == "current" and (
        str(member.get("status") or "") != "invited"
        or not str(member.get("authUserId") or "")
    ):
        raise RuntimeError("BAIC source projection is missing the accepted David invitation")
    if local_projection == "target" and (
        str(member.get("status") or "") not in {"invited", "active"}
        or not str(member.get("authUserId") or "")
    ):
        raise RuntimeError("BAIC target projection has an unexpected David member state")

    backend = next((item for item in upstream.backends if item.id == BACKEND_ID), None)
    if backend is None:
        raise RuntimeError("configured BAIC backend is unavailable")
    target_orgs = await upstream.find_organizations_exact(
        organization_id=TARGET_UPSTREAM_ORGANIZATION_ID, backend=backend
    )
    target_teams = await upstream.find_teams_exact(
        organization_id=TARGET_UPSTREAM_ORGANIZATION_ID,
        team_id=TARGET_UPSTREAM_TEAM_ID,
        backend=backend,
    )
    if len(target_orgs) != 1 or len(target_teams) != 1:
        raise RuntimeError("BAIC target Organization/Team is not a unique exact match")

    assets: list[dict[str, Any]] = []
    for alias in KEY_ALIASES:
        records = await upstream.list_keys_exact(key_alias=alias, backend=backend)
        if len(records) != 1:
            raise RuntimeError(f"BAIC key alias is not unique: {alias}")
        identity = upstream.report_only_key_identity(records[0])
        key_hash = str(identity.get("hash") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            raise RuntimeError(f"BAIC key is missing a stable hash: {alias}")
        if (
            str(identity.get("organizationId") or "")
            != TARGET_UPSTREAM_ORGANIZATION_ID
            or str(identity.get("teamId") or "") != TARGET_UPSTREAM_TEAM_ID
            or not str(identity.get("userId") or "").strip()
        ):
            raise RuntimeError(f"BAIC key scope changed: {alias}")
        assets.append({"alias": alias, **identity})

    return {
        "organization": organization,
        "department": department,
        "member": member,
        "localProjection": local_projection,
        "targetOrganizationAlias": _record_id(
            target_orgs[0], "organization_alias", "organizationAlias", "name"
        ),
        "targetTeamAlias": _record_id(
            target_teams[0], "team_alias", "teamAlias", "name"
        ),
        "assets": assets,
    }


async def apply_reconciliation(
    repository: PostgreSQLOrganizationRepository,
    upstream: LiteLLMClient,
    preview: dict[str, Any],
    *,
    backfill_from: date,
    backfill_through: date,
) -> dict[str, Any]:
    member = preview["member"]
    if str(preview.get("localProjection") or "") == "current":
        result = await repository.reconcile_baic_pilot_state(
            organization_id=LOCAL_ORGANIZATION_ID,
            department_id=LOCAL_DEPARTMENT_ID,
            member_id=LOCAL_MEMBER_ID,
            expected_organization_name="北汽",
            expected_admin_email="davidzhu2021@163.com",
            expected_current_upstream_organization_id=CURRENT_UPSTREAM_ORGANIZATION_ID,
            expected_current_upstream_team_id=CURRENT_UPSTREAM_TEAM_ID,
            target_upstream_organization_id=TARGET_UPSTREAM_ORGANIZATION_ID,
            target_upstream_team_id=TARGET_UPSTREAM_TEAM_ID,
            organization_name="北汽集团",
            department_name="企业管理",
            member_name="David Zhu",
            operation_key=OPERATION_KEY,
            expected_report_only_keys=[
                {
                    "backendId": BACKEND_ID,
                    "upstreamKeyHash": str(item["hash"]),
                    "upstreamKeyId": str(item.get("id") or ""),
                }
                for item in preview["assets"]
            ],
            actor="baic-pilot-reconciler",
        )
    else:
        operation = await repository.get_adoption_operation(OPERATION_KEY)
        if (
            not operation
            or str(operation.get("status") or "") != "applied"
            or str(operation.get("organizationId") or "") != LOCAL_ORGANIZATION_ID
        ):
            raise RuntimeError("BAIC target projection has no matching reconciliation record")
        stored_result = operation.get("result") or {}
        result = (
            stored_result
            if isinstance(stored_result, dict)
            else {"ok": True, "status": "reconciled"}
        )

    backend = next(item for item in upstream.backends if item.id == BACKEND_ID)
    # Re-read all key fingerprints after the local transaction and before any
    # identity import. This closes the preview/apply race without changing keys.
    assets: list[dict[str, Any]] = []
    for expected in preview["assets"]:
        records = await upstream.list_keys_exact(
            key_alias=str(expected["alias"]), backend=backend
        )
        if len(records) != 1:
            raise RuntimeError("BAIC key match changed after local reconciliation")
        identity = upstream.report_only_key_identity(records[0])
        stable_fields = ("id", "hash", "organizationId", "teamId", "userId")
        if any(
            str(identity.get(field) or "") != str(expected.get(field) or "")
            for field in stable_fields
        ):
            raise RuntimeError("BAIC key fingerprint changed after preflight")
        assets.append({"alias": expected["alias"], **identity})

    principal = await repository.ensure_principal(LOCAL_ORGANIZATION_ID, "梁海强")
    imported: list[dict[str, Any]] = []
    for asset in assets:
        asset_from = _asset_reporting_start(asset, backfill_from)
        effective_from = datetime.combine(
            asset_from, datetime.min.time(), tzinfo=timezone.utc
        )
        await repository.attach_principal_upstream_identity(
            str(principal["id"]),
            organization_id=LOCAL_ORGANIZATION_ID,
            backend_id=BACKEND_ID,
            upstream_user_id=str(asset.get("userId") or ""),
        )
        imported_item = await repository.import_report_only_key_identity(
            LOCAL_ORGANIZATION_ID,
            backend_id=BACKEND_ID,
            upstream_key_hash=str(asset["hash"]),
            upstream_key_id=str(asset.get("id") or ""),
            key_alias=str(asset["alias"]),
            principal_id=str(principal["id"]),
            member_id="",
            department_id=LOCAL_DEPARTMENT_ID,
            effective_from=effective_from,
            effective_through=None,
            idempotency_key=f"{OPERATION_KEY}:{asset['hash']}",
            upstream_organization_id_snapshot=TARGET_UPSTREAM_ORGANIZATION_ID,
            upstream_team_id_snapshot=TARGET_UPSTREAM_TEAM_ID,
            upstream_user_id_snapshot=str(asset.get("userId") or ""),
            models_snapshot=list(asset.get("models") or []),
            max_budget_usd_snapshot=asset.get("maxBudget"),
            spend_usd_snapshot=asset.get("spend"),
            budget_duration_snapshot=str(asset.get("budgetDuration") or ""),
            expires_at_snapshot=_parse_optional_upstream_datetime(asset.get("expiresAt")),
            blocked_snapshot=bool(asset.get("blocked")),
            import_batch_id=OPERATION_KEY,
            reporting_requested_through=backfill_through,
            actor="baic-pilot-reconciler",
        )
        imported.append(imported_item)
        await repository.ensure_usage_backfill(
            LOCAL_ORGANIZATION_ID,
            principal_id=str(principal["id"]),
            usage_key_identity_id=str(imported_item["id"]),
            backend_id=BACKEND_ID,
            requested_from=asset_from,
            requested_through=backfill_through,
            import_batch_id=OPERATION_KEY,
        )

    service = OrganizationProvisioningService(
        repository, upstream, changed_by="baic-pilot-reconciler"
    )
    if str(member.get("status") or "") == "active" and str(
        member.get("upstreamUserId") or ""
    ):
        provisioned = member
    else:
        provisioned = await service.provision_member(
            LOCAL_ORGANIZATION_ID,
            LOCAL_MEMBER_ID,
            auth_user_id=str(member.get("authUserId") or ""),
        )
    upstream_user_id = str(provisioned.get("upstreamUserId") or "")
    if not upstream_user_id:
        raise RuntimeError("David upstream user was not provisioned")

    target_org_info = await upstream.organization_info(
        TARGET_UPSTREAM_ORGANIZATION_ID, backend=backend
    )
    target_team_info = await upstream.team_info(backend, TARGET_UPSTREAM_TEAM_ID)
    if (
        upstream_user_id not in _member_ids(target_org_info)
        or upstream_user_id not in _member_ids(target_team_info)
    ):
        raise RuntimeError("David target Organization/Team membership was not confirmed")

    # Remove only the stale memberships created for this same stable user.
    await _ignore_absent_legacy_membership(
        upstream.delete_team_member(
            CURRENT_UPSTREAM_TEAM_ID,
            user_id=upstream_user_id,
            changed_by="baic-pilot-reconciler",
            backend=backend,
        )
    )
    await _ignore_absent_legacy_membership(
        upstream.delete_organization_member(
            CURRENT_UPSTREAM_ORGANIZATION_ID,
            user_id=upstream_user_id,
            changed_by="baic-pilot-reconciler",
            backend=backend,
        )
    )
    if str(preview.get("localProjection") or "") == "current":
        await repository.record_audit(
            LOCAL_ORGANIZATION_ID,
            "organization.baic_pilot.completed",
            actor="baic-pilot-reconciler",
            target_type="adoption",
            target_id=OPERATION_KEY,
            details={"legacyAssetCount": len(imported), "principalId": principal["id"]},
        )
    return {**result, "member": provisioned, "legacyAssetCount": len(imported)}


async def run(*, apply: bool, backfill_from: date, backfill_through: date) -> dict[str, Any]:
    if os.getenv("ORGANIZATION_MODE", "").strip().lower() != "real":
        raise RuntimeError("ORGANIZATION_MODE=real is required")
    if backfill_from > backfill_through:
        raise RuntimeError("backfill range is invalid")
    repository = PostgreSQLOrganizationRepository(os.environ["USAGE_DATABASE_URL"])
    await repository.connect()
    try:
        # Import lazily so CLI help and unit tests do not initialize the app.
        from .main import client

        upstream = client()
        preview = await build_preview(repository, upstream)
        public_preview = {
            "organizationName": preview["organization"].get("name"),
            "memberStatus": preview["member"].get("status"),
            "targetOrganizationAlias": preview["targetOrganizationAlias"],
            "targetTeamAlias": preview["targetTeamAlias"],
            "legacyAssets": [
                {
                    "alias": item["alias"],
                    "hashSuffix": str(item["hash"])[-8:],
                    "spend": item.get("spend"),
                }
                for item in preview["assets"]
            ],
        }
        if not apply:
            return {"status": "ready", "apply": False, **public_preview}
        applied = await apply_reconciliation(
            repository,
            upstream,
            preview,
            backfill_from=backfill_from,
            backfill_through=backfill_through,
        )
        from .main import usage_store
        from .usage_sync import run_pending_usage_backfills

        store = usage_store()
        if store is None:
            raise RuntimeError("usage database is unavailable for BAIC historical import")
        await store.connect()
        backfill = await run_pending_usage_backfills(
            upstream, store, repository, max_windows=128
        )
        return {
            "status": "applied",
            "apply": True,
            **public_preview,
            **applied,
            "historicalBackfill": backfill,
        }
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--from-date", type=date.fromisoformat, default=date(2020, 1, 1))
    parser.add_argument("--through-date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run(
                    apply=args.apply,
                    backfill_from=args.from_date,
                    backfill_through=args.through_date,
                )
            ),
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
