import json
import shutil
import subprocess
from pathlib import Path

import pytest


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"


def test_frontend_recovers_stale_csrf_once_and_shares_refresh() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend CSRF behavior test")

    script = r"""
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[1], "utf8");
const apiStart = source.indexOf("async function api(");
const apiEnd = source.indexOf("function normalizeAuthUser", apiStart);
const csrfTokenDeclaration = source.match(/let authCsrfToken = "";/)?.[0];
const refreshDeclaration = source.match(/let csrfRefreshPromise = null;/)?.[0];
const generationDeclaration = source.match(/let authSessionGeneration = 0;/)?.[0];
if (apiStart < 0 || apiEnd < 0 || !csrfTokenDeclaration || !refreshDeclaration || !generationDeclaration) {
  throw new Error("Unable to locate the frontend API implementation");
}
const runtime = `${csrfTokenDeclaration}\n${refreshDeclaration}\n${generationDeclaration}\n${source.slice(apiStart, apiEnd)}`;

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
  };
}

async function runScenario(fetch, scenario, configureContext) {
  const context = vm.createContext({ fetch, setTimeout, clearTimeout, AbortController });
  configureContext?.(context);
  vm.runInContext(runtime, context);
  return vm.runInContext(`(async () => { ${scenario} })()`, context);
}

(async () => {
  const retryCalls = [];
  let retryLogoutAttempts = 0;
  const recovered = await runScenario(async (path, options = {}) => {
    retryCalls.push({ path, token: options.headers?.["X-CSRF-Token"] || "" });
    if (path === "/api/auth/csrf") return response(200, { csrfToken: "fresh-token" });
    retryLogoutAttempts += 1;
    if (retryLogoutAttempts === 1) {
      return response(403, { detail: { error: "expired", code: "AUTH_CSRF_INVALID" } });
    }
    return response(200, { ok: true });
  }, `
    authCsrfToken = "stale-token";
    return api("/api/auth/logout", { method: "POST", body: "{}" });
  `);

  const singleRetryCalls = [];
  let singleRetryLogoutAttempts = 0;
  const secondFailure = await runScenario(async (path, options = {}) => {
    singleRetryCalls.push({ path, token: options.headers?.["X-CSRF-Token"] || "" });
    if (path === "/api/auth/csrf") return response(200, { csrfToken: "fresh-token" });
    singleRetryLogoutAttempts += 1;
    return response(403, { detail: { error: "expired", code: "AUTH_CSRF_INVALID" } });
  }, `
    authCsrfToken = "stale-token";
    try {
      await api("/api/auth/logout", { method: "POST", body: "{}" });
      return { code: "" };
    } catch (error) {
      return { code: error.code };
    }
  `);

  const deleteCalls = [];
  let deleteAttempts = 0;
  const recoveredDelete = await runScenario(async (path, options = {}) => {
    deleteCalls.push({ path, token: options.headers?.["X-CSRF-Token"] || "" });
    if (path === "/api/auth/csrf") return response(200, { csrfToken: "delete-fresh-token" });
    deleteAttempts += 1;
    if (deleteAttempts === 1) return response(403, { detail: { error: "expired", code: "AUTH_CSRF_INVALID" } });
    return response(200, { ok: true });
  }, `
    authCsrfToken = "delete-stale-token";
    return api("/api/me/keys/key-1", { method: "DELETE" });
  `);

  let sharedRefreshCalls = 0;
  const shared = await runScenario(async (path) => {
    if (path !== "/api/auth/csrf") throw new Error(`Unexpected request: ${path}`);
    sharedRefreshCalls += 1;
    await new Promise((resolve) => setTimeout(resolve, 10));
    return response(200, { csrfToken: "shared-token" });
  }, `
    const tokens = await Promise.all([ensureCsrfToken(), ensureCsrfToken()]);
    return tokens;
  `);

  const concurrentCalls = [];
  let concurrentRefreshCalls = 0;
  const concurrent = await runScenario(async (path, options = {}) => {
    concurrentCalls.push({ path, token: options.headers?.["X-CSRF-Token"] || "" });
    if (path === "/api/auth/csrf") {
      concurrentRefreshCalls += 1;
      await new Promise((resolve) => setTimeout(resolve, 15));
      return response(200, { csrfToken: "concurrent-fresh-token" });
    }
    if (options.headers?.["X-CSRF-Token"] === "stale-token") {
      return response(403, { detail: { error: "expired", code: "AUTH_CSRF_INVALID" } });
    }
    return response(200, { ok: true, path });
  }, `
    authCsrfToken = "stale-token";
    return Promise.all([
      api("/api/write/fast", { method: "POST", body: "{}" }),
      api("/api/write/slow", { method: "POST", body: "{}" }),
    ]);
  `);

  let staggeredRefreshCalls = 0;
  const staggered = await runScenario(async (path, options = {}) => {
    if (path === "/api/auth/csrf") {
      staggeredRefreshCalls += 1;
      return response(200, { csrfToken: `staggered-fresh-${staggeredRefreshCalls}` });
    }
    const token = options.headers?.["X-CSRF-Token"] || "";
    if (token === "staggered-stale-token") {
      if (path.endsWith("/slow")) await new Promise((resolve) => setTimeout(resolve, 20));
      return response(403, { detail: { error: "expired", code: "AUTH_CSRF_INVALID" } });
    }
    return response(200, { ok: true, path, token });
  }, `
    authCsrfToken = "staggered-stale-token";
    return Promise.all([
      api("/api/write/fast", { method: "POST", body: "{}" }),
      api("/api/write/slow", { method: "POST", body: "{}" }),
    ]);
  `);

  let resolveStaleRefresh;
  const staleRefresh = await runScenario(async (path) => {
    if (path !== "/api/auth/csrf") throw new Error(`Unexpected request: ${path}`);
    return new Promise((resolve) => { resolveStaleRefresh = () => resolve(response(200, { csrfToken: "old-session-token" })); });
  }, `
    const pending = ensureCsrfToken();
    authSessionGeneration += 1;
    authCsrfToken = "";
    globalThis.finishRefresh();
    const returnedToken = await pending;
    return { returnedToken, cachedToken: authCsrfToken };
  `, (context) => {
    context.finishRefresh = () => resolveStaleRefresh();
  });

  process.stdout.write(JSON.stringify({
    recovered,
    retryCalls,
    secondFailure,
    singleRetryCalls,
    singleRetryLogoutAttempts,
    recoveredDelete,
    deleteCalls,
    shared,
    sharedRefreshCalls,
    concurrent,
    concurrentCalls,
    concurrentRefreshCalls,
    staggered,
    staggeredRefreshCalls,
    staleRefresh,
  }));
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [node, "-e", script, str(APP_JS)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["recovered"] == {"ok": True}
    assert result["retryCalls"] == [
        {"path": "/api/auth/logout", "token": "stale-token"},
        {"path": "/api/auth/csrf", "token": ""},
        {"path": "/api/auth/logout", "token": "fresh-token"},
    ]
    assert result["secondFailure"] == {"code": "AUTH_CSRF_INVALID"}
    assert result["singleRetryLogoutAttempts"] == 2
    assert len(result["singleRetryCalls"]) == 3
    assert result["recoveredDelete"] == {"ok": True}
    assert result["deleteCalls"] == [
        {"path": "/api/me/keys/key-1", "token": "delete-stale-token"},
        {"path": "/api/auth/csrf", "token": ""},
        {"path": "/api/me/keys/key-1", "token": "delete-fresh-token"},
    ]
    assert result["shared"] == ["shared-token", "shared-token"]
    assert result["sharedRefreshCalls"] == 1
    assert result["concurrent"] == [
        {"ok": True, "path": "/api/write/fast"},
        {"ok": True, "path": "/api/write/slow"},
    ]
    assert result["concurrentRefreshCalls"] == 1
    assert [call["token"] for call in result["concurrentCalls"]].count("stale-token") == 2
    assert [call["token"] for call in result["concurrentCalls"]].count("concurrent-fresh-token") == 2
    assert result["staggeredRefreshCalls"] == 1
    assert result["staggered"] == [
        {"ok": True, "path": "/api/write/fast", "token": "staggered-fresh-1"},
        {"ok": True, "path": "/api/write/slow", "token": "staggered-fresh-1"},
    ]
    assert result["staleRefresh"] == {
        "returnedToken": "old-session-token",
        "cachedToken": "",
    }
