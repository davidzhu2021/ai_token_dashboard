import json
import re
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "assets" / "app.js"
INDEX_HTML = ROOT / "index.html"


def _run_identity_script(body: str) -> object:
    source = APP_JS.read_text(encoding="utf-8")
    blocks = []
    for name in ("normalizedEmployeeIdentity", "employeeIdentityKeys", "employeeMatchesIdentity"):
        match = re.search(r"function " + name + r"\([\s\S]*?\n\}", source)
        assert match, f"未找到 {name}"
        blocks.append(match.group(0))
    script = f"""
{chr(10).join(blocks)}
{body}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        result = subprocess.run(
            ["node", script_path], capture_output=True, text=True, encoding="utf-8", check=True
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    return json.loads(result.stdout)


def test_department_employee_drilldown_controls_are_wired() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")

    for control_id in (
        "departmentDetailBackLabel",
        "departmentEmployeeDetailCard",
        "departmentEmployeeUsageDetailDateFilter",
        "departmentEmployeeUsageDetailModelFilter",
        "departmentEmployeeUsageDetailStatusFilter",
        "departmentEmployeeUsageDetailSearch",
        "departmentEmployeeUsageDetailReset",
        "departmentEmployeeUsageTable",
    ):
        assert f'id="{control_id}"' in markup

    assert 'selectDepartmentEmployee(employeeRow.dataset.employee);' in source
    assert 'backLabel.textContent = "返回部门总览";' in source
    assert 'backLabel.textContent = "返回全部部门";' in source
    assert 'renderDepartmentMemberMetrics(chartData);' in source
    assert 'renderDepartmentEmployeeTable();' in source
    assert 'el("departmentEmployeeUsageDetailSearch").addEventListener("input", updateDepartmentEmployeeUsageFilters);' in source


def test_department_employee_ranking_stays_visible_and_highlights_selection() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'tableId === "departmentUserTable" && employeeMatchesIdentity(item, selectedDepartmentEmployeeInfo())' in source
    assert 'renderEmployeeRanking("departmentUserTable", "departmentUserCount", departmentEmployees' in source
    assert "点击员工查看个人用量详情" in source


def test_employee_identity_prefers_email_and_merges_account_ids() -> None:
    result = _run_identity_script(
        """
const selected = { employeeId: "primary-alice", employeeEmail: "Alice@Example.com", userIds: ["primary-alice", "her-alice"] };
const sameEmail = { employeeId: "other-id", employeeEmail: "alice@example.com" };
const accountWithoutEmail = { employeeId: "her-alice", employeeEmail: "" };
const conflictingEmail = { employeeId: "her-alice", employeeEmail: "bob@example.com" };
console.log(JSON.stringify([
  employeeMatchesIdentity(sameEmail, selected),
  employeeMatchesIdentity(accountWithoutEmail, selected),
  employeeMatchesIdentity(conflictingEmail, selected),
]));
"""
    )
    assert result == [True, True, False]


def test_same_name_does_not_merge_distinct_employees() -> None:
    result = _run_identity_script(
        """
const first = { employeeId: "account-a", employeeEmail: "a@example.com", employeeName: "同名员工" };
const second = { employeeId: "account-b", employeeEmail: "b@example.com", employeeName: "同名员工" };
console.log(JSON.stringify(employeeMatchesIdentity(first, second)));
"""
    )
    assert result is False


def test_selected_employee_snapshot_survives_empty_filtered_response() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    load_block = source[source.index("function loadDepartmentData(") : source.index("async function loadTeamRankingData(")]

    assert "if (selectedDepartmentEmployee)" in load_block
    assert "if (matchedEmployee)" in load_block
    assert "resetDepartmentEmployeeSelection()" not in load_block
    assert "当前员工在所选范围内暂无用量记录" in source


def test_department_employee_detail_is_not_sortable() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    tbody_index = markup.index('<tbody id="departmentEmployeeUsageTable">')
    head_start = markup.rindex("<thead>", 0, tbody_index)
    head_end = markup.index("</thead>", head_start)
    head = markup[head_start:head_end]

    assert "data-sort-key" not in head
    assert "sortable" not in head
