/**
 * HTTP-клиент к backend.
 *
 * Access-токен живёт только в памяти (переменная ниже), не в localStorage: так его не
 * украдёт внедрённый на страницу скрипт. Refresh-токен — в HttpOnly-cookie, JS его не видит.
 * Если backend ответил 401, один раз обновляем сессию через /api/auth/refresh и повторяем
 * запрос. Несколько одновременных 401 ждут одного и того же обновления.
 */
import type { ApiErrorDetail, TokenResponse } from './types'

let accessToken: string | null = null
let refreshing: Promise<TokenResponse | null> | null = null
let onSessionLost: (() => void) | null = null
let onTokens: ((t: TokenResponse) => void) | null = null

export class ApiError extends Error {
  readonly status: number
  readonly detail: ApiErrorDetail

  constructor(status: number, detail: ApiErrorDetail) {
    super(detail.message)
    this.status = status
    this.detail = detail
  }
}

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function hasAccessToken(): boolean {
  return accessToken !== null
}

/** Колбэки стора авторизации: новые токены и окончательная потеря сессии. */
export function bindSessionHandlers(handlers: {
  tokens: (t: TokenResponse) => void
  lost: () => void
}): void {
  onTokens = handlers.tokens
  onSessionLost = handlers.lost
}

async function parseError(res: Response): Promise<ApiError> {
  let detail: ApiErrorDetail = { code: 'http_' + res.status, message: defaultMessage(res.status) }
  try {
    const body = await res.json()
    if (body?.detail && typeof body.detail === 'object' && !Array.isArray(body.detail)) {
      detail = { ...detail, ...body.detail }
    } else if (Array.isArray(body?.detail)) {
      detail = { code: 'validation', message: 'Проверьте заполнение полей' }
    }
  } catch {
    /* не JSON — оставляем текст по умолчанию */
  }
  return new ApiError(res.status, detail)
}

function defaultMessage(status: number): string {
  if (status === 403) return 'Нет доступа'
  if (status === 404) return 'Не найдено'
  if (status >= 500) return 'Сервер временно недоступен. Попробуйте позже.'
  return 'Не удалось выполнить запрос'
}

/** Обновить access-токен по refresh-cookie. null — сессии нет. */
export function refreshSession(): Promise<TokenResponse | null> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const res = await fetch('/api/auth/refresh', {
          method: 'POST',
          credentials: 'same-origin',
        })
        if (!res.ok) return null
        const tokens = (await res.json()) as TokenResponse
        accessToken = tokens.access_token
        onTokens?.(tokens)
        return tokens
      } catch {
        return null
      } finally {
        refreshing = null
      }
    })()
  }
  return refreshing
}

export async function api<T>(
  path: string,
  init: { method?: string; body?: unknown; form?: FormData; query?: Record<string, unknown> } = {},
): Promise<T> {
  const url = new URL('/api' + path, window.location.origin)
  for (const [k, v] of Object.entries(init.query ?? {})) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v))
  }

  const send = () => {
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (init.body !== undefined) headers['Content-Type'] = 'application/json'
    if (accessToken) headers.Authorization = `Bearer ${accessToken}`
    // FormData (загрузка файла): Content-Type с границей частей браузер ставит сам.
    const body = init.form ?? (init.body === undefined ? undefined : JSON.stringify(init.body))
    return fetch(url, {
      method: init.method ?? (init.form ? 'POST' : 'GET'),
      headers,
      body,
      credentials: 'same-origin',
    })
  }

  let res = await send()
  if (res.status === 401 && accessToken) {
    const tokens = await refreshSession()
    if (tokens) {
      res = await send()
    } else {
      accessToken = null
      onSessionLost?.()
    }
  }
  if (!res.ok) throw await parseError(res)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}
