<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import LogoSlot from '@/components/LogoSlot.vue'
import { ApiError } from '@/api/client'
import type { ApiErrorDetail } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { plural } from '@/utils/format'

// Экран строго по согласованному макету «Вход»: слева только логотип и название,
// под кнопкой «Продолжить без входа» подписи нет.
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const login = ref('')
const password = ref('')
const busy = ref(false)
const error = ref<ApiErrorDetail | null>(null)

const guestAllowed = computed(() => auth.me?.guest_mode_enabled !== false)
const passwordWrong = computed(() => error.value?.code === 'invalid_credentials')

const errorTitle = computed(() => {
  const e = error.value
  if (!e) return ''
  if (e.code === 'locked') return 'Вход временно заблокирован'
  if (e.code === 'provider_unavailable') return 'Не удалось проверить пароль'
  return e.message
})

const errorText = computed(() => {
  const e = error.value
  if (!e) return ''
  if (e.code === 'invalid_credentials' && e.remaining_attempts !== undefined) {
    const n = e.remaining_attempts
    const minutes = e.lockout_minutes ?? 15
    return `${plural(n, 'Осталась', 'Осталось', 'Осталось')} ${n} ${plural(n, 'попытка', 'попытки', 'попыток')}, затем вход будет заблокирован на ${minutes} ${plural(minutes, 'минуту', 'минуты', 'минут')}.`
  }
  if (e.code === 'locked' || e.code === 'provider_unavailable') return e.message
  return ''
})

function safeNext(): string {
  const next = route.query.next
  // Только внутренние пути: защищает от перенаправления на чужой сайт после входа.
  return typeof next === 'string' && next.startsWith('/') && !next.startsWith('//') ? next : '/'
}

async function submit() {
  if (busy.value) return
  error.value = null
  if (!login.value.trim() || !password.value) {
    error.value = { code: 'empty', message: 'Введите табельный номер и пароль' }
    return
  }
  busy.value = true
  try {
    await auth.login(login.value.trim(), password.value)
    password.value = ''
    await router.replace(safeNext())
  } catch (e) {
    error.value =
      e instanceof ApiError
        ? e.detail
        : { code: 'network', message: 'Нет связи с сервером. Попробуйте ещё раз.' }
    password.value = ''
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login">
    <div class="login-brand">
      <LogoSlot />
      <h1>Поиск по документам</h1>
    </div>
    <div class="login-form">
      <form novalidate @submit.prevent="submit">
        <div>
          <h2>Вход</h2>
          <p class="muted" style="margin-top: 6px">
            Используйте табельный номер и пароль от корпоративных систем.
          </p>
        </div>
        <div v-if="error" class="alert a-danger" role="alert">
          <AppIcon name="warn" />
          <div>
            <b>{{ errorTitle }}</b>{{ errorText }}
          </div>
        </div>
        <div class="field">
          <label class="label" for="login">Табельный номер</label>
          <input
            id="login"
            v-model="login"
            class="input"
            name="username"
            autocomplete="username"
            autofocus
            style="height: 48px; font-size: 17px"
          />
        </div>
        <div class="field">
          <label class="label" for="password">Пароль</label>
          <input
            id="password"
            v-model="password"
            class="input"
            :class="{ err: passwordWrong }"
            type="password"
            name="password"
            autocomplete="current-password"
            style="height: 48px; font-size: 17px"
          />
        </div>
        <button class="btn btn-primary btn-lg" type="submit" :disabled="busy">
          {{ busy ? 'Проверяем…' : 'Войти' }}
        </button>
        <template v-if="guestAllowed">
          <div class="divider">или</div>
          <RouterLink class="btn btn-secondary btn-lg" to="/">Продолжить без входа</RouterLink>
        </template>
      </form>
    </div>
  </div>
</template>
