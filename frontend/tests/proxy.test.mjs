import test from "node:test";
import assert from "node:assert/strict";
import { onRequest } from "../functions/api/[[path]].js";

const TOKEN = "synthetic-proxy-test-token";
const env = { NEWSLENS_API_BASE: "https://backend.example.test", NEWSLENS_API_TOKEN: TOKEN };
const meta = { generated_at: "2026-11-05T00:00:00+00:00", data_mode: "live" };
const request = (path = "status", options = {}) => new Request(`https://frontend.example.test/api/${path}`, options);
const ok = (data = {}) => Response.json({ data, meta });

async function errorCode(response) {
  const body = await response.json();
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(JSON.stringify(body).includes(TOKEN), false);
  assert.equal(typeof body.error.message, "string");
  return body.error.code;
}

test("forwards only fixed route and server token; response headers contain no upstream secrets", async t => {
  let captured;
  t.mock.method(globalThis, "fetch", async (url, options) => {
    captured = { url, options };
    return new Response(JSON.stringify({ data: { worker_running: true }, meta }), {
      headers: { "Content-Type": "application/json", "Set-Cookie": TOKEN, Authorization: TOKEN, "X-Upstream-Secret": TOKEN },
    });
  });
  const response = await onRequest({ request: request("status", { headers: { Authorization: "Bearer browser-input", Cookie: "private=browser" } }), env });
  assert.equal(response.status, 200);
  assert.equal(captured.url, "https://backend.example.test/api/status");
  assert.equal(captured.options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(captured.options.headers.Cookie, undefined);
  assert.equal(captured.options.redirect, "manual");
  assert.equal(captured.options.cache, "no-store");
  assert.equal(response.headers.get("Set-Cookie"), null);
  assert.equal(response.headers.get("Authorization"), null);
  assert.equal(response.headers.get("X-Upstream-Secret"), null);
  assert.equal((await response.text()).includes(TOKEN), false);
});

test("forwards valid filters while preserving exact category and pagination", async t => {
  let url;
  t.mock.method(globalThis, "fetch", async value => { url = new URL(value); return ok([]); });
  const response = await onRequest({ request: request("news?symbol=AAPL&category=Politics%20%26%20Policy&sentiment=unclear&limit=20&offset=40"), env });
  assert.equal(response.status, 200);
  assert.equal(url.searchParams.get("category"), "Politics & Policy");
  assert.equal(url.searchParams.get("offset"), "40");
});

test("all five routes accept their expected response data shapes", async t => {
  t.mock.method(globalThis, "fetch", async url => ok(url.includes("/status") ? {} : []));
  for (const path of ["status", "quotes", "news", "bars?symbol=MSFT&limit=500", "alerts?limit=100&offset=10000"]) {
    assert.equal((await onRequest({ request: request(path), env })).status, 200, path);
  }
});

test("rejects unknown and nested routes without contacting backend", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("unexpected"); });
  for (const path of ["keys", "admin", "logs", "news/secret", "https://evil.example/", "%73tatus", "status%2Fsecret"]) {
    assert.equal((await onRequest({ request: request(path), env })).status, 404, path);
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("rejects every mutating method and OPTIONS", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("unexpected"); });
  for (const method of ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]) {
    const response = await onRequest({ request: request("news", { method }), env });
    assert.equal(response.status, 405);
    assert.equal(response.headers.get("Allow"), "GET");
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("rejects unconfigured or unsafe backend origins without fetch", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("unexpected"); });
  for (const base of ["", "not-a-url", "http://remote.example", "ftp://remote.example", "https://user:password@remote.example", "https://remote.example/api", "https://remote.example/?url=https://evil.example", "https://remote.example/#fragment"]) {
    const response = await onRequest({ request: request(), env: { ...env, NEWSLENS_API_BASE: base } });
    assert.equal(response.status, 503, base);
    assert.equal(await errorCode(response), "BACKEND_NOT_CONFIGURED");
  }
  for (const token of ["", undefined, "secret\r\nInjected: true"]) {
    assert.equal((await onRequest({ request: request(), env: { ...env, NEWSLENS_API_TOKEN: token } })).status, 503);
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("allows loopback HTTP only for local development", async t => {
  t.mock.method(globalThis, "fetch", async () => ok());
  for (const base of ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"]) {
    assert.equal((await onRequest({ request: request(), env: { ...env, NEWSLENS_API_BASE: base } })).status, 200);
  }
});

test("rejects arbitrary, duplicate or unbounded query parameters", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new Error("unexpected"); });
  for (const path of ["status?limit=1", "quotes?url=https://evil.example", "news?symbol=AAPL&symbol=MSFT", "news?symbol=aapl", "news?category=invalid", "news?sentiment=buy", "news?limit=0", "news?limit=101", "news?offset=-1", "news?offset=10001", "news?limit=1.2", "news?limit=1e2", "alerts?limit=999999999999999999999", "bars", "bars?symbol=AAPL&limit=501", "bars?symbol=../secrets"]) {
    assert.equal((await onRequest({ request: request(path), env })).status, 400, path);
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("does not follow backend redirects", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async (_url, options) => {
    assert.equal(options.redirect, "manual");
    return new Response(null, { status: 302, headers: { Location: "https://other.example/" } });
  });
  const response = await onRequest({ request: request(), env });
  assert.equal(response.status, 502);
  assert.equal(await errorCode(response), "BACKEND_REDIRECT");
  assert.equal(fetch.mock.callCount(), 1);
});

test("sanitizes authentication failures and other backend errors", async t => {
  for (const status of [401, 403, 404, 500, 503]) {
    t.mock.method(globalThis, "fetch", async () => new Response(`backend secrets: ${TOKEN}`, { status }));
    const response = await onRequest({ request: request(), env });
    assert.equal(response.status, status === 401 || status === 403 ? 502 : 503);
    await errorCode(response);
    t.mock.restoreAll();
  }
});

test("rejects HTML, malformed JSON and wrong envelopes without reflecting their bodies", async t => {
  const responses = [
    new Response(`<html>${TOKEN}</html>`, { headers: { "Content-Type": "text/html" } }),
    new Response(`invalid-${TOKEN}`, { headers: { "Content-Type": "application/json" } }),
    Response.json({ secret: TOKEN }),
    Response.json({ data: [], meta }),
    Response.json({ data: {}, meta: { ...meta, data_mode: "demo" } }),
  ];
  for (const upstream of responses) {
    t.mock.method(globalThis, "fetch", async () => upstream);
    const response = await onRequest({ request: request(), env });
    assert.equal(response.status, 502);
    assert.equal(await errorCode(response), "INVALID_BACKEND_RESPONSE");
    t.mock.restoreAll();
  }
});

test("rejects oversized declared and streamed response bodies", async t => {
  for (const headers of [{ "Content-Length": String(2 * 1024 * 1024 + 1) }, {}]) {
    t.mock.method(globalThis, "fetch", async () => new Response("x".repeat(2 * 1024 * 1024 + 1), {
      headers: { "Content-Type": "application/json", ...headers },
    }));
    const response = await onRequest({ request: request(), env });
    assert.equal(response.status, 502);
    assert.equal(await errorCode(response), "INVALID_BACKEND_RESPONSE");
    t.mock.restoreAll();
  }
});

test("offline errors expose neither token nor implementation exception text", async t => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error(`private failure ${TOKEN}`); });
  const response = await onRequest({ request: request(), env });
  assert.equal(response.status, 503);
  assert.equal(await errorCode(response), "BACKEND_UNAVAILABLE");
});

test("aborts a pending backend request after six seconds", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let signal;
  t.mock.method(globalThis, "fetch", async (_url, options) => new Promise((_resolve, reject) => {
    signal = options.signal;
    signal.addEventListener("abort", () => reject(new Error(`aborted ${TOKEN}`)), { once: true });
  }));
  const pending = onRequest({ request: request(), env });
  t.mock.timers.tick(6000);
  const response = await pending;
  assert.equal(signal.aborted, true);
  assert.equal(response.status, 504);
  assert.equal(await errorCode(response), "BACKEND_TIMEOUT");
});
