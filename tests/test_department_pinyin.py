from backend.main import department_search_indexes


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
