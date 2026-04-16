/** Optional bearer for mutating API routes when backend sets SHARK_API_TOKEN. */
const TOKEN = (import.meta.env.VITE_SHARK_API_TOKEN as string | undefined)?.trim();

export function apiFetch(input: string | URL, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (TOKEN) {
    headers.set('Authorization', `Bearer ${TOKEN}`);
  }
  return fetch(input, { ...init, headers });
}
