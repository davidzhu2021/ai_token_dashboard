"""Team-membership attribution backfill for unattributed personal usage rows."""

from backend.usage_sync import UsageSynchronizer, team_rename_map


def make_synchronizer():
    # The backfill helpers never touch client/store; lightweight doubles are fine.
    return UsageSynchronizer(client=None, store=None)


def memberships(*records):
    return [
        {
            "backendId": backend,
            "userId": user,
            "teamId": team,
            "teamName": name,
            "snapshotDate": date,
            "employeeEmail": email,
            "employeeName": name,
            "teamRole": role,
        }
        for backend, user, team, name, date, email, role in records
    ]


def test_latest_team_by_user_single_team() -> None:
    rows = memberships(
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-10", "a@x.com", "user"),
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "admin"),
    )
    assert UsageSynchronizer._latest_team_by_user(rows) == {
        ("primary", "cursor-a"): ("team-new", "AI Infra部")
    }


def test_latest_team_by_user_ambiguous_skipped() -> None:
    # 同一最新快照日期横跨两个团队（部门改名后旧团队未清理）→ 歧义，不回填。
    rows = memberships(
        ("primary", "cursor-a", "team-old", "AI技术院", "2026-08-18", "a@x.com", "admin"),
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "admin"),
    )
    assert UsageSynchronizer._latest_team_by_user(rows) == {}


def test_latest_team_by_user_rename_map_collapses(monkeypatch) -> None:
    monkeypatch.setenv(
        "USAGE_TEAM_RENAME_MAP", "team-old=team-new,team-old2=team-new2"
    )
    rows = memberships(
        ("primary", "cursor-a", "team-old", "AI技术院", "2026-08-18", "a@x.com", "admin"),
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "admin"),
    )
    assert UsageSynchronizer._latest_team_by_user(rows) == {
        ("primary", "cursor-a"): ("team-new", "AI Infra部")
    }


def test_team_rename_map_parsing(monkeypatch) -> None:
    monkeypatch.setenv(
        "USAGE_TEAM_RENAME_MAP",
        " team-old = team-new , team-old2=team-new2 ,bad-entry",
    )
    assert team_rename_map() == {"team-old": "team-new", "team-old2": "team-new2"}


def test_backfill_attaches_single_latest_team() -> None:
    sync = make_synchronizer()
    rows = [
        {"_userId": "cursor-a", "source": "其他", "totalTokens": 100},
        {"_userId": "cursor-b", "source": "Cursor", "totalTokens": 200},
    ]
    ms = memberships(
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "user"),
    )
    assert sync._backfill_team_from_membership("primary", rows, ms) == 1
    assert rows[0]["teamId"] == "team-new"
    assert rows[0]["attributionSource"] == "team_membership_backfill"
    assert rows[0]["billingEligible"] is False
    # 不在快照里的用户保持原样。
    assert "teamId" not in rows[1]


def test_backfill_skips_rows_with_tenant_evidence() -> None:
    sync = make_synchronizer()
    rows = [
        {"_userId": "cursor-a", "organizationId": "org-customer", "teamId": ""},
        {"_userId": "cursor-a", "teamId": "team-explicit", "totalTokens": 10},
    ]
    ms = memberships(
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "user"),
    )
    assert sync._backfill_team_from_membership("primary", rows, ms) == 0
    assert rows[0].get("teamId") == ""
    assert rows[1]["teamId"] == "team-explicit"


def test_backfill_skips_ambiguous_user() -> None:
    sync = make_synchronizer()
    rows = [{"_userId": "cursor-a", "source": "其他", "totalTokens": 100}]
    ms = memberships(
        ("primary", "cursor-a", "team-old", "AI技术院", "2026-08-18", "a@x.com", "admin"),
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "admin"),
    )
    # 没有重命名映射时保持未归因，绝不猜测。
    assert sync._backfill_team_from_membership("primary", rows, ms) == 0
    assert "teamId" not in rows[0]


def test_backfill_applies_rename_map(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_TEAM_RENAME_MAP", "team-old=team-new")
    sync = make_synchronizer()
    rows = [{"_userId": "cursor-a", "source": "Cursor", "totalTokens": 300}]
    ms = memberships(
        ("primary", "cursor-a", "team-old", "AI技术院", "2026-08-18", "a@x.com", "admin"),
        ("primary", "cursor-a", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "admin"),
    )
    assert sync._backfill_team_from_membership("primary", rows, ms) == 1
    assert rows[0]["teamId"] == "team-new"
    assert rows[0]["billingEligible"] is False


def test_backfill_respects_backend_scope() -> None:
    sync = make_synchronizer()
    rows = [{"_userId": "carher-271", "source": "Her", "totalTokens": 50}]
    ms = memberships(
        ("her", "carher-271", "team-new", "AI Infra部", "2026-08-18", "a@x.com", "user"),
    )
    # 同一 user 在另一个后端没有归属 → 按 backend 维度判定。
    assert sync._backfill_team_from_membership("primary", rows, ms) == 0
    assert sync._backfill_team_from_membership("her", rows, ms) == 1
    assert rows[0]["teamId"] == "team-new"


def test_backfill_empty_inputs() -> None:
    sync = make_synchronizer()
    assert sync._backfill_team_from_membership("primary", [], []) == 0
    assert sync._backfill_team_from_membership("primary", [{"_userId": "x"}], []) == 0
