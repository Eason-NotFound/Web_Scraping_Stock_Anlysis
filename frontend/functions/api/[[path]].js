// Read-only same-origin proxy. Secrets stay in Pages Functions, never browser JS.
const MAX_BYTES = 2 * 1024 * 1024;
const TIMEOUT_MS = 6000;
const ROUTES = new Set(["status", "quotes", "news", "bars", "alerts"]);
const CATEGORIES = new Set(["Finance", "Politics & Policy", "Macro", "Other"]);
const SENTIMENTS = new Set(["positive", "negative", "neutral", "unclear"]);
const QUERY_KEYS = {
  status: [], quotes: [],
  news: ["symbol", "category", "sentiment", "limit", "offset"],
  bars: ["symbol", "limit"],
  alerts: ["symbol", "limit", "offset"],
};

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...extraHeaders,
    },
  });
}

function failure(status, code, message, headers) {
  return jsonResponse({ error: { code, message } }, status, headers);
}

function backendOrigin(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const url = new URL(raw.trim());
    const local = ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) return null;
    if (url.username || url.password || url.search || url.hash || url.pathname !== "/") return null;
    return url.origin;
  } catch {
    return null;
  }
}

function checkedQuery(route, input) {
  const output = new URLSearchParams();
  for (const [key, value] of input) {
    if (!QUERY_KEYS[route].includes(key) || output.has(key)) return null;
    if (key === "symbol" && !/^[A-Z][A-Z.]{0,9}$/.test(value)) return null;
    if (key === "category" && !CATEGORIES.has(value)) return null;
    if (key === "sentiment" && !SENTIMENTS.has(value)) return null;
    if (key === "limit" || key === "offset") {
      if (!/^\d+$/.test(value)) return null;
      const number = Number(value);
      const maximum = key === "offset" ? 10000 : route === "bars" ? 500 : 100;
      const minimum = key === "offset" ? 0 : 1;
      if (!Number.isSafeInteger(number) || number < minimum || number > maximum) return null;
    }
    output.append(key, value);
  }
  if (route === "bars" && !output.has("symbol")) return null;
  return output;
}

async function readJsonBounded(response) {
  const length = Number(response.headers.get("Content-Length"));
  if (length > MAX_BYTES) throw new Error("oversized");
  if (!/\bapplication\/(?:[a-z0-9.-]+\+)?json\b/i.test(response.headers.get("Content-Type") || "")) {
    throw new Error("content-type");
  }
  if (!response.body) throw new Error("empty-body");
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BYTES) throw new Error("oversized");
      chunks.push(value);
    }
  } finally {
    // Stop oversized/invalid responses rather than continuing to download them.
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  const buffer = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    buffer.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer));
}

export async function onRequest({ request, env = {} }) {
  if (request.method !== "GET") {
    return failure(405, "METHOD_NOT_ALLOWED", "Only GET requests are available.", { Allow: "GET" });
  }
  const url = new URL(request.url);
  const match = /^\/api\/([a-z]+)\/?$/.exec(url.pathname);
  const route = match?.[1];
  if (!ROUTES.has(route)) return failure(404, "NOT_FOUND", "This API route is not available.");
  const query = checkedQuery(route, url.searchParams);
  if (query === null) return failure(400, "INVALID_QUERY", "The request contains unsupported or invalid query parameters.");

  const origin = backendOrigin(env.NEWSLENS_API_BASE);
  const token = env.NEWSLENS_API_TOKEN;
  if (!origin || typeof token !== "string" || !token || /[\r\n]/.test(token)) {
    return failure(503, "BACKEND_NOT_CONFIGURED", "The live data connection has not been configured.");
  }
  const upstreamUrl = new URL(`/api/${route}`, origin);
  upstreamUrl.search = query.toString();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(upstreamUrl.toString(), {
      method: "GET",
      headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
      redirect: "manual",
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status >= 300 && response.status < 400) {
      return failure(502, "BACKEND_REDIRECT", "The backend returned an unexpected redirect.");
    }
    if (response.status === 401 || response.status === 403) {
      return failure(502, "BACKEND_AUTH_FAILED", "The backend connection is not authorised.");
    }
    if (!response.ok) {
      return failure(503, "BACKEND_UNAVAILABLE", "The local backend is unavailable. Check its status and try again.");
    }
    let payload;
    try {
      payload = await readJsonBounded(response);
    } catch {
      if (controller.signal.aborted) throw new Error("aborted");
      return failure(502, "INVALID_BACKEND_RESPONSE", "The backend returned an invalid or oversized response.");
    }
    const dataOk = route === "status"
      ? payload?.data !== null && typeof payload?.data === "object" && !Array.isArray(payload.data)
      : Array.isArray(payload?.data);
    if (!dataOk || payload?.meta?.data_mode !== "live" || typeof payload.meta.generated_at !== "string") {
      return failure(502, "INVALID_BACKEND_RESPONSE", "The backend returned an invalid response format.");
    }
    return jsonResponse(payload);
  } catch {
    return controller.signal.aborted
      ? failure(504, "BACKEND_TIMEOUT", "The local backend did not respond in time.")
      : failure(503, "BACKEND_UNAVAILABLE", "The local backend is offline or could not be reached.");
  } finally {
    clearTimeout(timer);
  }
}
