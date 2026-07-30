"""首屏引导路径只等真正渲染首屏的请求。

模型目录不参与仪表盘渲染（首屏不读 modelCatalog），但它冷缓存时要打上游
/models 与 /model/info，曾被 await 在引导路径上，把首屏拖慢好几秒。这些断言
锁定修复后的契约：引导期只等用量与权限，模型目录改为后台预取，且预取与按需
加载共用同一个在途请求。
"""

from pathlib import Path

APP_JS = Path(__file__).parents[1] / "assets" / "app.js"


def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def show_app_body() -> str:
    source = app_js()
    start = source.index("async function showApp(")
    return source[start : source.index("\nasync function loadAuthScope(", start)]


def test_boot_does_not_await_model_catalog() -> None:
    body = show_app_body()

    # 引导期的最后一个 await 决定首屏何时可用：模型目录不该出现在里面。
    final_await = body[body.rindex("await Promise.all(") :]
    assert "loadModels" not in final_await, "模型目录仍被 await 在首屏引导路径上"
    assert "loadCurrentViewData()" in final_await
    assert "scopePromise" in final_await


def test_boot_still_prefetches_model_catalog_in_background() -> None:
    body = show_app_body()

    # 预取要保留，否则第一次打开模型广场反而变慢；且必须静默，避免弹出
    # 用户没有触发过的错误提示。
    assert "loadModels({ silent: true })" in body


def test_model_catalog_requests_are_deduplicated() -> None:
    source = app_js()

    # 后台预取与 switchView 的按需加载会撞在一起，必须复用在途请求，
    # 否则对上游目录接口发两次。
    assert "let modelCatalogRequest = null;" in source
    assert "if (!modelCatalogRequest) {" in source

    start = source.index("async function loadModels(")
    load_models = source[start : source.index("\nasync function showApp(", start)]
    assert "modelCatalogRequest = null;" in load_models, "在途请求未在结束后清空"

    # 报错与否按调用方决定：静默预取失败不弹提示，但用户已打开模型广场时，
    # 同一个在途请求失败仍要报错。
    assert "if (error && !silent)" in load_models
