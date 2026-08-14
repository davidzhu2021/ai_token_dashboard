import asyncio
import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend import main

ROOT_DIR = Path(__file__).resolve().parents[1]


def test_index_uses_fresh_app_asset_and_disables_html_cache() -> None:
    client = TestClient(main.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Cache-Control" in response.headers
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert re.search(r'<script src="/assets/app\.js\?v=[^"]+"></script>', response.text)
    assert main.APP_JS_VERSION_PLACEHOLDER not in response.text
    assert main.app_js_version() in response.text
    assert "20260807-personal-key-speed" not in response.text
    assert "20260805-team-member-keys" not in response.text
    assert "20260805-owner-identity" not in response.text
    assert "20260805-department-leader" not in response.text
    assert "20260805-customer-workspace-bar" not in response.text
    assert "20260805-token-delete" not in response.text
    assert "20260804-member-removal" not in response.text
    assert "20260804-organization-restore" not in response.text
    assert "20260804-pending-adoption" not in response.text
    assert "20260803-canonical-model-names" not in response.text
    assert "20260731-organization-real-mode-status" not in response.text
    assert "20260731-organization-member-avatar" not in response.text
    assert "20260731-organization-admin-merge" not in response.text
    assert "20260731-admin-ranking-department" not in response.text
    assert "20260731-admin-member-department" not in response.text
    assert "20260730-usage-detail-skip" not in response.text
    assert "20260730-sidebar-nav-reveal" not in response.text
    assert "20260730-organization-billing" not in response.text
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
    assert main.app_js_version() in response.text


def test_app_js_version_is_a_content_fingerprint(tmp_path, monkeypatch) -> None:
    asset_dir = tmp_path / "assets"
    asset_dir.mkdir()
    app_script = asset_dir / "app.js"
    app_script.write_text("first", encoding="utf-8")
    monkeypatch.setattr(main, "ROOT_DIR", tmp_path)

    first_version = main.app_js_version()
    app_script.write_text("second", encoding="utf-8")

    assert first_version != main.app_js_version()


def test_versioned_app_asset_uses_immutable_cache() -> None:
    client = TestClient(main.app)

    response = client.get("/assets/app.js?v=20260814-test")
    etag = response.headers["ETag"]
    not_modified = client.get(
        "/assets/app.js?v=20260814-test",
        headers={"If-None-Match": etag},
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert not_modified.status_code == 304
    assert not_modified.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_unversioned_assets_keep_default_cache_policy() -> None:
    client = TestClient(main.app)

    unversioned = client.get("/assets/app.js")
    unrelated_query = client.get("/assets/app.js?cache_bust=20260814")
    other_versioned_asset = client.get("/assets/pay/README.md?v=20260814-test")

    assert unversioned.status_code == 200
    assert unrelated_query.status_code == 200
    assert other_versioned_asset.status_code == 200
    assert unversioned.headers.get("Cache-Control") != "public, max-age=31536000, immutable"
    assert unrelated_query.headers.get("Cache-Control") != "public, max-age=31536000, immutable"
    assert other_versioned_asset.headers.get("Cache-Control") != "public, max-age=31536000, immutable"


async def _wire_response(path: str) -> tuple[int, str | None]:
    """按 ASGI 原始字节测量响应体，绕开测试客户端的透明解压。"""
    body = bytearray()
    headers: dict[str, str] = {}

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            headers.update({key.decode(): value.decode() for key, value in message["headers"]})
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await main.app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "headers": [(b"host", b"test"), (b"accept-encoding", b"gzip")],
        },
        receive,
        send,
    )
    return len(body), headers.get("content-encoding")


def test_boot_assets_are_compressed_on_the_wire() -> None:
    """首屏要先下载 index.html 与 app.js 才能发接口请求，两者必须压缩后再传。

    只断言 content-encoding 不够：中间件顺序或 minimum_size 配错时头还在、
    体积却没降，所以直接按 ASGI 原始字节校验压缩率。
    """
    for path in ("/", "/assets/app.js"):
        wire_size, encoding = asyncio.run(_wire_response(path))
        uncompressed = (ROOT_DIR / ("index.html" if path == "/" else "assets/app.js")).stat().st_size

        assert encoding == "gzip", f"{path} 未压缩传输"
        assert wire_size < uncompressed // 2, f"{path} 压缩后仍有 {wire_size} 字节"
