// Thin fetch wrapper that attaches the JWT and centralizes error handling.

const TOKEN_KEY = "jira_ai_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

// Listeners notified when a request returns 401 so the app can log out.
const unauthorizedHandlers = new Set();
export function onUnauthorized(fn) {
  unauthorizedHandlers.add(fn);
  return () => unauthorizedHandlers.delete(fn);
}
function notifyUnauthorized() {
  unauthorizedHandlers.forEach((fn) => {
    try {
      fn();
    } catch {
      /* ignore */
    }
  });
}

function authHeaders(extra = {}) {
  const token = getToken();
  const headers = { ...extra };
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

async function parseError(res) {
  let detail = `Request failed (${res.status})`;
  try {
    const data = await res.json();
    if (data && data.detail) {
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } else if (data && data.error) {
      detail = data.error;
    }
  } catch {
    /* non-JSON error body */
  }
  return detail;
}

// JSON request helper. Returns parsed JSON (or {} for empty bodies).
export async function apiFetch(path, { method = "GET", body, headers } = {}) {
  const opts = { method, headers: authHeaders(headers) };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error(await parseError(res));
  }
  if (!res.ok) {
    throw new Error(await parseError(res));
  }
  if (res.status === 204) return {};
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

// Blob download helper (reports, documents). Triggers a browser download.
export async function apiDownload(path, { method = "GET", body, fallbackName } = {}) {
  const opts = { method, headers: authHeaders() };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error(await parseError(res));
  }
  if (!res.ok) throw new Error(await parseError(res));

  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  let filename = fallbackName || "download";
  const filenameRegex = /filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/i;
  const match = disposition.match(filenameRegex);
  if (match && match[1]) {
    filename = match[1].replace(/['"]/g, "").trim();
  }
  triggerBlobDownload(blob, filename);
}

// Multipart upload helper. Sends a FormData body with auth (no Content-Type —
// the browser sets the multipart boundary). Returns parsed JSON.
export async function apiUpload(path, formData) {
  const res = await fetch(path, { method: "POST", headers: authHeaders(), body: formData });
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error(await parseError(res));
  }
  if (!res.ok) throw new Error(await parseError(res));
  const text = await res.text();
  return text ? JSON.parse(text) : {};
}

// Fetch a binary resource (e.g. an image) with auth and return an object URL.
// The caller owns the URL and must URL.revokeObjectURL it when done.
export async function apiObjectUrl(path) {
  const res = await fetch(path, { headers: authHeaders() });
  if (res.status === 401) {
    notifyUnauthorized();
    throw new Error(await parseError(res));
  }
  if (!res.ok) throw new Error(await parseError(res));
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}

export function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
