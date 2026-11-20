// Calls to the REST API with the session cookie. Calls that change something carry the
// header X-LO-Request, which the server demands for cookie sign-ins (protection against
// requests from other sites).

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-LO-Request"] = "1";
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`/api${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (response.status === 401) {
    // Session ended: back to the sign-in page, then here again.
    window.location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    throw new ApiError(401, "Signed out");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      // no JSON body
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
