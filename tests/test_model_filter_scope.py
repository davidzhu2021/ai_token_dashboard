from pathlib import Path

from backend.main import apply_usage_model_filter, reaggregate_team_employees_after_model_filter


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_model_filter_isolated_by_board_scope() -> None:
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")

    assert "function updateDashboardModelFilterOptions(rows, optionNames = null, scopeKey = \"\", dataKey = \"\")" in source
    assert "const contextChanged = Boolean(dataKey && state.dataKey && state.dataKey !== dataKey);" in source
    assert "const dashboardModelFilterStates = new Map();" in source
    assert "function activateDashboardModelFilterScope(scopeKey = dashboardModelFilterScope())" in source
    assert "incoming.every((name) => state.options.includes(name))" in source
    assert 'if (["dashboard", "admin", "team", "department"].includes(view))' in source
    assert "closeDashboardFilterPanels();" in source
    assert 'updateDashboardModelFilterOptions(payload.rows || [], payload.modelOptions, "personal",' in source
    assert 'updateDashboardModelFilterOptions(payload.summaryRows || payload.rows || [], payload.modelOptions, "admin",' in source
    assert 'updateDashboardModelFilterOptions(payload.summaryRows || payload.rows || [], payload.modelOptions, "department",' in source
    assert 'updateDashboardModelFilterOptions(payload.summaryRows || payload.rows || [], payload.modelOptions, "team",' in source
    assert 'const hasMemberRoster = Array.isArray(payload.employees);' in source


def test_team_model_filter_keeps_full_member_roster() -> None:
    employees = [
        {"employeeId": "alice", "employeeName": "Alice", "employeeEmail": "alice@example.com", "userIds": ["primary:alice"]},
        {"employeeId": "bob", "employeeName": "Bob", "employeeEmail": "bob@example.com", "userIds": ["primary:bob"]},
        {"employeeId": "carol", "employeeName": "Carol", "employeeEmail": "carol@example.com", "userIds": ["her:carol"]},
    ]
    rows = [{"employeeId": "alice", "employeeName": "Alice", "employeeEmail": "alice@example.com", "totalTokens": 123, "promptTokens": 100, "completionTokens": 23, "requestCount": 2, "successCount": 2, "failureCount": 0, "spend": 1.5}]

    result = reaggregate_team_employees_after_model_filter(employees, rows)

    assert [item["employeeEmail"] for item in result] == ["alice@example.com", "bob@example.com", "carol@example.com"]
    assert result[0]["totalTokens"] == 123
    assert result[1]["totalTokens"] == 0
    assert result[2]["totalTokens"] == 0


def test_team_model_filter_does_not_merge_same_name_or_cross_backend_accounts() -> None:
    employees = [
        {"employeeId": "alice-primary", "employeeName": "Alice", "employeeEmail": "alice.primary@example.com", "userIds": ["primary:alice"]},
        {"employeeId": "alice-her", "employeeName": "Alice", "employeeEmail": "alice.her@example.com", "userIds": ["her:alice"]},
    ]
    rows = [
        {"employeeId": "alice-primary", "employeeName": "Alice", "employeeEmail": "ALICE.PRIMARY@example.com", "totalTokens": 50, "requestCount": 1, "spend": 0.5},
    ]

    result = reaggregate_team_employees_after_model_filter(employees, rows)

    assert len(result) == 2
    assert result[0]["employeeEmail"] == "alice.primary@example.com"
    assert result[0]["totalTokens"] == 50
    assert result[1]["employeeEmail"] == "alice.her@example.com"
    assert result[1]["totalTokens"] == 0


def test_team_model_filter_keeps_identity_fields_and_handles_empty_rows() -> None:
    employees = [
        {"employeeId": "member-1", "employeeName": "Member", "employeeEmail": " member@example.com ", "teamRole": "leader", "userIds": ["primary:member"], "bindStatus": "已绑定"},
    ]

    result = reaggregate_team_employees_after_model_filter(employees, [])

    assert result == [{
        "employeeId": "member-1",
        "employeeName": "Member",
        "employeeEmail": " member@example.com ",
        "teamRole": "leader",
        "userIds": ["primary:member"],
        "bindStatus": "已绑定",
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "requestCount": 0,
        "successCount": 0,
        "failureCount": 0,
        "spend": 0.0,
    }]


def test_team_model_filter_matches_emailless_members_by_exact_backend_account() -> None:
    employees = [
        {"employeeId": "primary-user", "employeeName": "Same", "employeeEmail": "", "userIds": ["primary:primary-user"]},
        {"employeeId": "her-user", "employeeName": "Same", "employeeEmail": "", "userIds": ["her:her-user"]},
    ]
    rows = [{"backend": "her", "employeeId": "her-user", "employeeName": "Same", "employeeEmail": "", "totalTokens": 70, "requestCount": 1, "spend": 0.7}]

    result = reaggregate_team_employees_after_model_filter(employees, rows)

    assert result[0]["employeeId"] == "her-user"
    assert result[0]["totalTokens"] == 70
    assert result[1]["employeeId"] == "primary-user"
    assert result[1]["totalTokens"] == 0


def test_team_model_filter_aggregates_multiple_source_accounts_for_one_member() -> None:
    employees = [{
        "employeeId": "alice@example.com",
        "employeeName": "Alice",
        "employeeEmail": " Alice@Example.com ",
        "userIds": ["primary:alice", "her:alice"],
    }]
    rows = [
        {"backend": "primary", "employeeId": "alice", "employeeEmail": "alice@example.com", "totalTokens": 20, "spend": 0.2},
        {"backend": "her", "employeeId": "alice", "employeeEmail": "ALICE@example.com", "totalTokens": 30, "spend": 0.3},
    ]

    result = reaggregate_team_employees_after_model_filter(employees, rows)

    assert len(result) == 1
    assert result[0]["totalTokens"] == 50
    assert result[0]["spend"] == 0.5


def test_team_model_filter_handles_malformed_payload_without_cross_member_contamination() -> None:
    employees = [
        {"employeeId": "primary:alice", "employeeName": "Alice", "employeeEmail": "", "backend": "primary"},
        None,
        {"employeeId": "bob", "employeeName": "Bob", "employeeEmail": "bob@example.com"},
    ]
    rows = [
        {"backend": "primary", "employeeId": "alice", "employeeEmail": "", "totalTokens": 9},
        {"employeeName": "Alice", "totalTokens": 999},
    ]

    result = reaggregate_team_employees_after_model_filter(employees, rows)

    assert [item["employeeName"] for item in result] == ["Alice", "Bob"]
    assert result[0]["totalTokens"] == 9
    assert result[1]["totalTokens"] == 0


def test_apply_team_model_filter_keeps_roster_when_all_rows_are_filtered_out() -> None:
    payload = {
        "rows": [{"model": "visible", "employeeEmail": "alice@example.com", "totalTokens": 10}],
        "summaryRows": [{"model": "visible", "totalTokens": 10}],
        "employees": [
            {"employeeId": "alice", "employeeName": "Alice", "employeeEmail": "alice@example.com", "userIds": ["primary:alice"], "totalTokens": 10},
            {"employeeId": "bob", "employeeName": "Bob", "employeeEmail": "bob@example.com", "userIds": ["primary:bob"], "totalTokens": 20},
        ],
        "dataQuality": {"rankingScope": "selected_team"},
    }

    result = apply_usage_model_filter(payload, ["missing"])

    assert [item["employeeEmail"] for item in result["employees"]] == ["alice@example.com", "bob@example.com"]
    assert all(item["totalTokens"] == 0 for item in result["employees"])
    assert result["rows"] == []
    assert result["summaryRows"] == []
