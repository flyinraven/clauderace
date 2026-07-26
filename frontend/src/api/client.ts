/**
 * Thin API client.
 *
 * Two things it handles that matter operationally: attaching the bearer token,
 * and being explicit about Render free-tier cold starts. A sleeping instance
 * takes about a minute to answer the first request, so a slow response is
 * reported as "waking up" rather than looking like a hang.
 */

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const TOKEN_KEY = 'race.token'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

/** Fired when the server rejects our token, so the app can bounce to login. */
export const onUnauthorized = new Set<() => void>()

/** Fired when a request is taking long enough to be a cold start. */
export const onSlowRequest = new Set<(slow: boolean) => void>()

type Options = Omit<RequestInit, 'body'> & { body?: unknown; raw?: boolean }

const SLOW_REQUEST_MS = 3000

export async function api<T = unknown>(path: string, options: Options = {}): Promise<T> {
  const { body, raw, headers, ...rest } = options
  const token = getToken()

  const finalHeaders: Record<string, string> = { ...(headers as Record<string, string>) }
  if (token) finalHeaders.Authorization = `Bearer ${token}`

  let payload: BodyInit | undefined
  if (body instanceof FormData) {
    payload = body
  } else if (body !== undefined) {
    finalHeaders['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }

  const slowTimer = setTimeout(() => onSlowRequest.forEach((fn) => fn(true)), SLOW_REQUEST_MS)

  let response: Response
  try {
    response = await fetch(`${BASE_URL}/api${path}`, { ...rest, headers: finalHeaders, body: payload })
  } catch {
    throw new ApiError(
      'Could not reach the server. If it has been idle it may be starting up — wait a moment and try again.',
      0,
    )
  } finally {
    clearTimeout(slowTimer)
    onSlowRequest.forEach((fn) => fn(false))
  }

  if (response.status === 401) {
    setToken(null)
    onUnauthorized.forEach((fn) => fn())
    throw new ApiError('Your session has expired. Please sign in again.', 401)
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const data = await response.json()
      if (typeof data?.detail === 'string') detail = data.detail
      else if (Array.isArray(data?.detail)) detail = data.detail.map((d: { msg?: string }) => d.msg).join('; ')
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(detail, response.status)
  }

  if (raw) return response as unknown as T
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/**
 * Fetch an image as an object URL.
 *
 * The image endpoint is authenticated and `<img src>` cannot send an
 * Authorization header, so the bytes are fetched here rather than putting the
 * token in a query string where it would end up in server logs.
 */
export async function fetchImageObjectUrl(imageId: number): Promise<string> {
  const token = getToken()
  const response = await fetch(`${BASE_URL}/api/images/${imageId}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!response.ok) throw new ApiError(`Could not load image ${imageId}`, response.status)
  return URL.createObjectURL(await response.blob())
}
