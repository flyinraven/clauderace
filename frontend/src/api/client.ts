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
  /**
   * The request was sent more than once. A retried request can fail in ways a
   * first attempt cannot - notably a 409 from a server that deduplicates,
   * which means an earlier attempt actually landed. Callers that opt into
   * `retry` need to tell those apart.
   */
  afterRetry: boolean
  constructor(message: string, status: number, afterRetry = false) {
    super(message)
    this.status = status
    this.afterRetry = afterRetry
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

type Options = Omit<RequestInit, 'body'> & {
  body?: unknown
  raw?: boolean
  /**
   * Retry this request even though it is not a read. Only for endpoints the
   * server itself deduplicates, so that a retry cannot do the work twice -
   * document upload rejects a repeated file hash with 409.
   */
  retry?: boolean
}

const SLOW_REQUEST_MS = 3000

/**
 * A sleeping instance drops the request that wakes it, and an ingest heavy
 * enough to restart the instance takes every in-flight request with it. Both
 * look like a dead server to one attempt and like a pause to three, so reads
 * are retried before the failure is shown.
 *
 * Reads by default. A write has to ask, and may only ask when the server
 * deduplicates it - otherwise a retry would ingest the same document twice.
 */
const RETRY_DELAYS_MS = [2000, 5000]

const isRetryable = (method: string | undefined): boolean =>
  (method ?? 'GET').toUpperCase() === 'GET' || (method ?? '').toUpperCase() === 'HEAD'

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export async function api<T = unknown>(path: string, options: Options = {}): Promise<T> {
  const { body, raw, headers, retry, ...rest } = options
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

  const retryDelays = retry || isRetryable(rest.method) ? RETRY_DELAYS_MS : []
  let retried = false

  let response: Response
  try {
    for (let attempt = 0; ; attempt += 1) {
      try {
        response = await fetch(`${BASE_URL}/api${path}`, { ...rest, headers: finalHeaders, body: payload })
        break
      } catch {
        if (attempt >= retryDelays.length) {
          throw new ApiError(
            'Could not reach the server. If it has been idle it may be starting up — wait a moment and try again.',
            0,
            retried,
          )
        }
        await sleep(retryDelays[attempt])
        retried = true
      }
    }
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
    throw new ApiError(detail, response.status, retried)
  }

  if (raw) return response as unknown as T
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/**
 * Wake the instance without asking the caller to care whether it worked.
 *
 * Render's free tier sleeps after about fifteen minutes and drops the request
 * that wakes it. That is survivable for a read, which retries, and visible for
 * an upload, which is one shot from the user's point of view and takes a large
 * file with it. Calling this when a page that is about to POST first opens
 * means the instance is usually awake by the time the button is pressed.
 *
 * Deliberately never rejects: it is an optimisation, and a page must not fail
 * to load because a warm-up did.
 */
export async function warm(): Promise<void> {
  try {
    await api('/health')
  } catch {
    /* the real request will report a server that is genuinely unreachable */
  }
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
