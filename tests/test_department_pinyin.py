from backend.main import department_search_indexes
from backend.organization_store import InMemoryOrganizationStore


def test_department_search_indexes_include_full_pinyin_and_initials():
    assert department_search_indexes("技术部") == {
        "fullPinyin": "jishubu",
        "pinyinInitials": "jsb",
    }


def test_department_search_indexes_preserve_ascii_and_mixed_names():
    assert department_search_indexes("AI平台部") == {
        "fullPinyin": "aipingtaibu",
        "pinyinInitials": "aptb",
    }


def test_mock_department_usage_options_include_pinyin_indexes():
    payload = InMemoryOrganizationStore().mock_department_usage(
        "org-demo",
        start_date="2026-08-31", end_date="2026-08-31", source="all"
    )
    options = payload.get("departmentOptions")
    assert options
    engineering = next(item for item in options if item["departmentName"] == "Engineering")
    assert engineering["fullPinyin"] == "engineering"
    assert engineering["pinyinInitials"] == "e"
