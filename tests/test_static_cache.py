from fastapi.testclient import TestClient

from backend import main


def test_index_uses_fresh_app_asset_and_disables_html_cache() -> None:
    client = TestClient(main.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Cache-Control" in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "/assets/app.js?v=20260729-topup-manual-qr" in response.text
    assert "20260729-topup-billing-center" not in response.text
    assert "20260728-model-alias-display-names" not in response.text
    assert "20260728-vendor-official-icons" not in response.text
    assert "20260728-model-plaza-pricing" not in response.text
    assert "20260728-auth-required-fix" not in response.text
    assert "20260727-carher-landing" not in response.text
    assert "20260727-toc-auth" not in response.text
    assert "20260720-oidc-login-fix" not in response.text

    admin_view = response.text.index('id="adminView"')
    assert response.text.index('id="adminDetailCard"', admin_view) < response.text.index('id="adminDailyOverview"', admin_view)

    team_view = response.text.index('id="teamView"')
    assert response.text.index('id="teamSelector"', team_view) < response.text.index('id="teamDetailCard"', team_view)
    assert response.text.index('id="teamDetailCard"', team_view) < response.text.index('id="teamDailyOverview"', team_view)

    department_view = response.text.index('id="departmentView"')
    assert response.text.index('id="departmentDetailCard"', department_view) < response.text.index('id="departmentOverviewHero"', department_view)


def test_spa_fallback_disables_html_cache() -> None:
    client = TestClient(main.app)

    response = client.get("/some/dashboard/path")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
