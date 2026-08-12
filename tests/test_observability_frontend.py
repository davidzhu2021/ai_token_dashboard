from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_platform_admin_navigation_and_views_exist() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for view in ("stability", "cost-control"):
        assert f'data-view="{view}"' in markup
    assert 'isPlatformAdmin() && currentUser?.observabilityDashboardsEnabled' in source
    assert 'el("stabilityTab")?.classList.toggle("hidden", !canUseObservability)' in source
    assert 'el("costControlTab")?.classList.toggle("hidden", !canUseObservability)' in source


def test_partial_states_and_request_drawer_are_rendered() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="stabilityDrawer"' in markup
    assert "当前窗口覆盖不足" in source
    assert "当前月份尚无完整 API 或人工成本数据" in source
    assert "提示词和响应正文" in markup


def test_cost_control_forms_and_write_handlers_exist() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "costBudgetForm",
        "costItemForm",
        "savingsActionForm",
        "costModelSplit",
    ):
        assert f'id="{element_id}"' in markup
    assert 'method: id ? "PATCH" : "POST"' in source
    assert 'method: "PUT"' in source
    assert 'method: "DELETE"' in source


def test_frontend_copy_does_not_expose_backend_branding() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    start = markup.index('id="stabilityView"')
    end = markup.index('id="costControlView"')
    cost_end = markup.find("</section>", end) + len("</section>")
    dashboard_copy = markup[start:cost_end]
    assert "LiteLLM" not in dashboard_copy
    assert "Virtual Key" not in dashboard_copy
    assert "master key" not in dashboard_copy.lower()
