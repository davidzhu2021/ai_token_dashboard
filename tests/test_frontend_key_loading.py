"""Static contracts for fast, safe personal-key loading."""

from pathlib import Path


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"


def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_personal_key_cache_is_short_lived_user_scoped_and_sanitized() -> None:
    source = app_js()

    assert "const PERSONAL_KEY_CACHE_TTL_MS = 30_000;" in source
    assert 'const PERSONAL_KEY_CACHE_PREFIX = "tongqu:personal-keys:v1:";' in source
    assert "function personalKeyCacheIdentity(" in source
    assert "window.sessionStorage.setItem(storageKey" in source
    assert "CACHEABLE_PERSONAL_KEY_FIELDS" in source
    cache_fields = source[
        source.index("const CACHEABLE_PERSONAL_KEY_FIELDS")
        : source.index("let hasLoadedPersonalKeys")
    ]
    assert "currentPlainKey" not in cache_fields
    assert '"key"' not in cache_fields
    assert "revealedKeys" not in cache_fields


def test_personal_key_requests_skip_models_and_reuse_inflight_work() -> None:
    source = app_js()
    load_keys = source[
        source.index("async function fetchPersonalKeys(")
        : source.index("function prefetchPersonalKeys(")
    ]

    assert 'new URLSearchParams({ include_models: "0" })' in load_keys
    assert 'params.set("refresh", "1")' in load_keys
    assert "if (keyListRequest) return keyListRequest;" in load_keys
    assert "if (keyRefreshRequest) return keyRefreshRequest;" in load_keys
    assert "await pendingListRequest;" in load_keys


def test_personal_key_cache_restores_before_background_revalidation() -> None:
    source = app_js()
    load_keys = source[
        source.index("function loadKeys(")
        : source.index("function personalKeysAreFresh(")
    ]

    assert "restorePersonalKeyCache();" in load_keys
    assert "fetchPersonalKeys(false, options)" in load_keys
    assert 'loadKeys(false, { silent: true });' in source
    assert "prefetchPersonalKeys();" in source
    assert "if (!personalKeysAreFresh() && !isKeysLoading)" in source
    assert "!personalKeys.length && !isKeysLoading" not in source


def test_personal_key_rendering_keeps_previous_rows_during_refresh() -> None:
    source = app_js()
    render = source[source.index("function renderKeys()") : source.index("function renderKeyModelChoices()")]

    assert 'hasLoadedPersonalKeys ? "更新中" : "加载中"' in render
    assert "if (isKeysLoading && !hasLoadedPersonalKeys)" in render
    assert 'if (keyRefreshError)' in render
    assert "列表暂未更新" in render
    fetch = source[source.index("async function fetchPersonalKeys(") : source.index("function loadKeys(")]
    assert "if (hadLoadedData || hasLoadedPersonalKeys) keyRefreshError = message;" in fetch


def test_personal_key_cache_and_state_clear_on_logout_and_mutations_refresh() -> None:
    source = app_js()
    show_login = source[source.index("function showLogin()") : source.index("async function submitPasswordLogin()")]
    show_app = source[source.index("async function showApp(user) {") : source.index("async function loadAuthScope()")]

    assert "clearPersonalKeyCache(previousUser);" in show_login
    assert "hasLoadedPersonalKeys = false;" in show_login
    assert "keyListRequest = null;" in show_login
    assert "keyRefreshRequest = null;" in show_login
    assert "previousKeyIdentity !== nextKeyIdentity" in show_app
    assert "clearPersonalKeyCache(previousUser);" in show_app
    assert "authSessionGeneration += 1;" in show_app
    assert "hasLoadedPersonalKeys = false;" in show_app
    assert "clearRevealedKeys();" in show_app
    assert "clearPlainKey();" in show_app
    assert source.count("await loadKeys(true);") >= 4
    assert "personalKeys = [];\n    await loadKeys(true);" not in source
