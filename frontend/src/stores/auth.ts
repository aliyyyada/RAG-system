import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  ApiError,
  api,
  bindSessionHandlers,
  refreshSession,
  setAccessToken,
} from '@/api/client'
import type { ApiErrorDetail, Me, TokenResponse } from '@/api/types'

/**
 * Кто сейчас работает с системой и что ему можно.
 *
 * Права приходят с backend (/api/auth/me) и используются только чтобы не показывать
 * лишние кнопки. Защита — на backend: даже если вручную открыть /admin/users,
 * сервер ответит 403.
 */
export const useAuthStore = defineStore('auth', () => {
  const me = ref<Me | null>(null)
  const ready = ref(false)
  let refreshTimer: number | undefined

  const user = computed(() => me.value?.user ?? null)
  const isGuest = computed(() => !me.value?.user)
  const permissions = computed(() => new Set(me.value?.permissions ?? []))

  function can(...perms: string[]): boolean {
    return perms.some((p) => permissions.value.has(p))
  }

  function scheduleRefresh(expiresIn: number): void {
    window.clearTimeout(refreshTimer)
    // Обновляем за минуту до истечения access-токена, чтобы запрос не упал на 401.
    const ms = Math.max(30, expiresIn - 60) * 1000
    refreshTimer = window.setTimeout(() => void refreshSession(), ms)
  }

  function applyTokens(t: TokenResponse): void {
    setAccessToken(t.access_token)
    me.value = t.me
    scheduleRefresh(t.expires_in)
  }

  async function loadGuest(): Promise<void> {
    setAccessToken(null)
    window.clearTimeout(refreshTimer)
    me.value = await api<Me>('/auth/me')
  }

  bindSessionHandlers({
    tokens: applyTokens,
    lost: () => {
      void loadGuest().catch(() => undefined)
    },
  })

  /** При открытии страницы: есть живая сессия (refresh-cookie) — входим молча. */
  async function bootstrap(): Promise<void> {
    if (ready.value) return
    try {
      const tokens = await refreshSession()
      if (!tokens) await loadGuest()
    } catch {
      me.value = null
    } finally {
      ready.value = true
    }
  }

  async function reloadMe(): Promise<void> {
    me.value = await api<Me>('/auth/me')
  }

  async function login(login: string, password: string): Promise<void> {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ login, password }),
      credentials: 'same-origin',
    })
    if (!res.ok) {
      let detail: ApiErrorDetail = {
        code: 'http_' + res.status,
        message: 'Не удалось войти. Попробуйте ещё раз.',
      }
      try {
        const body = await res.json()
        if (body?.detail && !Array.isArray(body.detail)) detail = body.detail
      } catch {
        /* не JSON */
      }
      throw new ApiError(res.status, detail)
    }
    applyTokens((await res.json()) as TokenResponse)
  }

  async function logout(everywhere = false): Promise<void> {
    try {
      if (everywhere) await api('/auth/logout-all', { method: 'POST' })
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' })
    } finally {
      await loadGuest().catch(() => {
        me.value = null
      })
    }
  }

  return { me, ready, user, isGuest, can, bootstrap, login, logout, reloadMe }
})
