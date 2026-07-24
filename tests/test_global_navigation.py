from pathlib import Path

from fastapi.testclient import TestClient

from backend import main


DOCUMENT_URL = "https://t83dfrspj4.feishu.cn/wiki/La8twWEToibR9Jk6yZucTz78nDc"
APP_JS = Path(__file__).parents[1] / "assets" / "app.js"


def test_global_navigation_is_available_on_home_and_console() -> None:
    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    assert response.text.count('aria-label="全局导航"') == 2
    assert response.text.count('data-global-page="home"') == 2
    assert response.text.count('data-global-page="console"') == 2
    assert response.text.count('data-global-page="models"') == 2
    assert response.text.count(f'href="{DOCUMENT_URL}"') == 2


def test_model_plaza_is_removed_from_sidebar_navigation() -> None:
    response = TestClient(main.app).get("/")
    sidebar_start = response.text.index('<aside class="sidebar"')
    sidebar_end = response.text.index("</aside>", sidebar_start)
    sidebar = response.text[sidebar_start:sidebar_end]

    assert 'data-view="models"' not in sidebar
    assert "模型广场" not in sidebar
    assert 'id="modelsView"' in response.text
    assert 'id="appShell" class="app-shell"' in response.text


def test_global_navigation_keeps_protected_pages_behind_login() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'if (!currentUser) {\n    promptForLogin();' in source
    assert 'showToast("请先登录后访问控制台和模型广场")' in source
    assert 'el("ssoButton").lastChild.textContent = isLoggedIn ? "进入控制台"' in source
    assert 'if (currentView === "keys") clearRevealedKeys();' in source
    assert 'switchView(page === "models" ? "models" : "dashboard")' in source
    assert 'setGlobalPage(view === "models" ? "models" : "console")' in source
    assert 'el("appShell").classList.toggle("models-layout", view === "models")' in source
