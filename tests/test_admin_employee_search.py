from pathlib import Path

from backend.litellm_client import employee_search_indexes
from backend.organization_store import InMemoryOrganizationStore


ROOT = Path(__file__).parents[1]


def test_employee_search_indexes_match_department_pinyin_rules():
    assert employee_search_indexes("张三") == {"fullPinyin": "zhangsan", "pinyinInitials": "zs"}
    assert employee_search_indexes("AI平台部") == {"fullPinyin": "aipingtaibu", "pinyinInitials": "aptb"}


def test_mock_admin_employee_summaries_include_search_indexes():
    payload = InMemoryOrganizationStore().usage_payload("org-demo", "2026-07-01", "2026-07-03")
    assert payload["employees"]
    for employee in payload["employees"]:
        assert "fullPinyin" in employee
        assert "pinyinInitials" in employee


def test_admin_picker_contract_supports_identity_and_pinyin_matching():
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="adminEmployeeOptions"' in markup
    assert 'aria-controls="adminEmployeeOptions"' in markup
    assert "item.fullPinyin" in source
    assert "item.pinyinInitials" in source
    assert "全部员工" in source
    assert "暂无匹配员工" in source
    assert 'event.key === "Escape"' in source
