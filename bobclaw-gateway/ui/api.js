export const ACCESS_KEY = "bc_access";
export const REFRESH_KEY = "bc_refresh";

let unauthorizedHandler = null;

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = typeof handler === "function" ? handler : null;
}

export function getAccessToken() {
  return localStorage.getItem(ACCESS_KEY);
}

export function getRefreshToken() {
  return localStorage.getItem(REFRESH_KEY);
}

export function hasSessionTokens() {
  return Boolean(getAccessToken() && getRefreshToken());
}

export function setSessionTokens(tokens) {
  localStorage.setItem(ACCESS_KEY, tokens.access_token);
  localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
}

export function clearSessionTokens() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

export function notifyUnauthorized() {
  clearSessionTokens();
  if (unauthorizedHandler) {
    unauthorizedHandler();
  }
}

function headersFrom(opts = {}) {
  return new Headers(opts.headers || {});
}

function withAuth(opts = {}) {
  const headers = headersFrom(opts);
  const token = getAccessToken();

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  if (opts.body && !(opts.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  return {
    ...opts,
    headers
  };
}

export async function refreshAccessToken() {
  const refreshToken = getRefreshToken();
  if (!refreshToken) {
    return false;
  }

  const response = await fetch("/auth/refresh", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ refresh_token: refreshToken })
  });

  if (!response.ok) {
    return false;
  }

  const payload = await response.json();
  if (!payload.access_token || !payload.refresh_token) {
    return false;
  }

  setSessionTokens(payload);
  return true;
}

export async function apiFetch(path, opts = {}) {
  const firstResponse = await fetch(path, withAuth(opts));

  if (firstResponse.status !== 401) {
    return firstResponse;
  }

  let refreshed = false;
  try {
    refreshed = await refreshAccessToken();
  } catch (_error) {
    refreshed = false;
  }

  if (!refreshed) {
    notifyUnauthorized();
    return firstResponse;
  }

  const retryResponse = await fetch(path, withAuth(opts));
  if (retryResponse.status === 401) {
    notifyUnauthorized();
  }

  return retryResponse;
}
