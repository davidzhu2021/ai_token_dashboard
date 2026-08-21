from backend.usage_sync import resolve_display_identity


def test_user_alias_has_highest_priority_and_normalizes_email() -> None:
    result = resolve_display_identity(
        user_id="carher-zhangsan",
        user_record={
            "user_alias": "张三",
            "user_email": "ZhangSan@Example.com",
            "metadata": {"display_name": "备用姓名"},
        },
        log_record={"name": "日志姓名"},
        directory={},
    )

    assert result == {
        "name": "张三",
        "email": "zhangsan@example.com",
        "nameSource": "litellm_user_alias",
        "confidence": "high",
    }


def test_metadata_display_name_fills_missing_alias() -> None:
    result = resolve_display_identity(
        user_id="u-1",
        user_record={"user_email": "alice@example.com", "metadata": {"display_name": "Alice"}},
        log_record=None,
        directory={},
    )

    assert result["name"] == "Alice"
    assert result["nameSource"] == "litellm_metadata_display_name"
    assert result["confidence"] == "high"


def test_log_name_fills_missing_user_directory_name() -> None:
    result = resolve_display_identity(
        user_id="u-2",
        user_record={"user_email": "bob@example.com"},
        log_record={"user_alias": "Bob"},
        directory={},
    )

    assert result["name"] == "Bob"
    assert result["nameSource"] == "spendlog_user_alias"


def test_directory_identity_can_fill_name_and_email() -> None:
    result = resolve_display_identity(
        user_id="u-3",
        user_record=None,
        log_record=None,
        directory={("her", "u-3"): {"name": "Carol", "email": "carol@example.com"}},
        backend_id="her",
    )

    assert result["name"] == "Carol"
    assert result["email"] == "carol@example.com"
    assert result["nameSource"] == "identity_directory"
    assert result["confidence"] == "high"


def test_email_prefix_is_nonempty_inferred_fallback() -> None:
    result = resolve_display_identity(
        user_id="u-4",
        user_record={"user_email": "dave@example.com"},
        log_record=None,
        directory={},
    )

    assert result["name"] == "dave"
    assert result["nameSource"] == "email_prefix"
    assert result["confidence"] == "medium"


def test_user_id_is_last_nonempty_fallback() -> None:
    result = resolve_display_identity(
        user_id="u-5",
        user_record=None,
        log_record=None,
        directory={},
    )

    assert result["name"] == "u-5"
    assert result["nameSource"] == "user_id"
    assert result["confidence"] == "low"

