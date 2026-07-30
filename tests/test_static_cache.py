from fastapi.testclient import TestClient

from backend import main


def test_index_uses_fresh_app_asset_and_disables_html_cache() -> None:
    client = TestClient(main.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Cache-Control" in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "/assets/app.js?v=20260730-customer-organizations" in response.text
    assert "20260729-ranking-column-sort" not in response.text
    assert "20260729-topup-manual-qr" not in response.text
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


def test_summary_cards_use_two_layer_metadata_header() -> None:
    client = TestClient(main.app)

    response = client.get("/")

    assert response.status_code == 200
    summary_cards = (
        ("personalDailyOverview", "heroContext", "heroFreshness"),
        ("adminDailyOverview", "adminHeroContext", "adminHeroFreshness"),
        ("teamDailyOverview", "teamHeroContext", "teamHeroFreshness"),
        ("departmentOverviewHero", "departmentHeroContext", "departmentHeroFreshness"),
    )
    for overview_id, context_id, freshness_id in summary_cards:
        overview_start = response.text.index(f'id="{overview_id}"')
        main_card_start = response.text.index('<div class="daily-main-card">', overview_start)
        source_card_start = response.text.index('<div class="daily-source-card">', main_card_start)
        card_markup = response.text[main_card_start:source_card_start]

        assert 'class="daily-main-title-row"' in card_markup
        assert 'class="daily-main-meta"' in card_markup
        assert f'id="{context_id}" class="daily-context"' in card_markup
        assert f'id="{freshness_id}" class="daily-freshness"' in card_markup
        assert card_markup.index('class="daily-main-title-row"') < card_markup.index('class="daily-main-meta"')


def test_spa_fallback_disables_html_cache() -> None:
    client = TestClient(main.app)

    response = client.get("/some/dashboard/path")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
