<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import CheckList from '@/components/ui/CheckList.vue'
import FormError from '@/components/ui/FormError.vue'
import { assignableRoles, adminApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { AdminUserDetail, Group, Permission, Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { ACCOUNT_TYPE_LABEL, formatDateTime } from '@/utils/format'

const open = defineModel<boolean>('open', { required: true })
const props = defineProps<{
  userId: number | null
  roles: Role[]
  groups: Group[]
  permissions: Permission[]
}>()
const emit = defineEmits<{ saved: [] }>()
const auth = useAuthStore()

const user = ref<AdminUserDetail | null>(null)
const fullName = ref('')
const roleIds = ref<number[]>([])
const groupIds = ref<number[]>([])
const active = ref(true)
const password = ref('')
const error = ref<string | null>(null)
const info = ref<string | null>(null)
const busy = ref(false)

function fill(u: AdminUserDetail) {
  user.value = u
  fullName.value = u.full_name
  roleIds.value = u.roles.map((r) => r.id)
  groupIds.value = u.groups.map((g) => g.id)
  active.value = u.is_active
  password.value = ''
}

watch(open, async (v) => {
  if (!v || props.userId === null) return
  error.value = null
  info.value = null
  user.value = null
  try {
    fill(await adminApi.user(props.userId))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить пользователя'
  }
})

const roleItems = computed(() => assignableRoles(props.roles, !!auth.me?.is_admin))
const groupItems = computed(() => props.groups.map((g) => ({ id: g.id, name: g.name })))
const isSelf = computed(() => user.value?.id === auth.user?.id)
const permTitles = computed(() => {
  const granted = new Set(user.value?.effective_permissions ?? [])
  return props.permissions.filter((p) => granted.has(p.code)).map((p) => p.title)
})

async function run(action: () => Promise<AdminUserDetail>, done: string) {
  error.value = null
  info.value = null
  busy.value = true
  try {
    fill(await action())
    info.value = done
    emit('saved')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить'
  } finally {
    busy.value = false
  }
}

async function save() {
  const u = user.value
  if (!u) return
  if (password.value && password.value.length < 10) {
    error.value = 'Пароль должен быть не короче 10 символов'
    return
  }
  await run(
    () =>
      adminApi.updateUser(u.id, {
        full_name: fullName.value,
        role_ids: roleIds.value,
        group_ids: groupIds.value,
        is_active: active.value,
        ...(password.value ? { password: password.value } : {}),
      }),
    'Изменения сохранены',
  )
}
</script>

<template>
  <AppDialog v-model:open="open" :title="user ? user.full_name || user.personnel_number : 'Пользователь'" wide>
    <FormError :message="error" />
    <div v-if="info" class="alert a-ok" role="status"><div>{{ info }}</div></div>
    <template v-if="user">
      <dl class="kv">
        <dt>Табельный номер</dt><dd class="mono">{{ user.personnel_number }}</dd>
        <dt>Учётка</dt>
        <dd>{{ ACCOUNT_TYPE_LABEL[user.account_type] }}<span v-if="user.is_emergency"> · аварийная</span></dd>
        <dt>Подразделение</dt><dd>{{ user.department || '—' }}</dd>
        <dt>Последний вход</dt><dd class="num">{{ formatDateTime(user.last_login_at) }}</dd>
        <dt>Активных сессий</dt><dd class="num">{{ user.active_sessions }}</dd>
        <dt>Итоговые роли</dt><dd>{{ user.effective_roles.join(', ') }}</dd>
        <dt>Итоговые права</dt><dd>{{ permTitles.join('; ') || '—' }}</dd>
      </dl>
      <div v-if="user.status === 'locked'" class="alert a-warn">
        <div>
          <b>Вход заблокирован после {{ user.failed_attempts }} неудачных попыток</b>
          до {{ formatDateTime(user.locked_until) }}
        </div>
        <button type="button" class="btn btn-secondary btn-sm" style="margin-left: auto" :disabled="busy" @click="run(() => adminApi.unlock(user!.id), 'Блокировка снята')">
          Разблокировать
        </button>
      </div>
      <div class="field">
        <label class="label" for="ue-name">ФИО</label>
        <input id="ue-name" v-model="fullName" class="input" />
        <span v-if="user.account_type === 'external'" class="hint">Обновится из внешнего сервиса при следующем входе</span>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <CheckList v-model="roleIds" label="Роли (кроме «Пользователь» — она есть у всех)" :items="roleItems" />
        <CheckList v-model="groupIds" label="Группы" :items="groupItems" empty="Групп пока нет" />
      </div>
      <div v-if="user.account_type !== 'external'" class="field">
        <label class="label" for="ue-pw">Новый пароль</label>
        <input id="ue-pw" v-model="password" class="input" type="password" autocomplete="new-password" />
        <span class="hint">Оставьте пустым, чтобы не менять. После смены все сессии закроются.</span>
      </div>
      <label class="check">
        <input v-model="active" type="checkbox" :disabled="isSelf" />
        Учётная запись активна
        <span v-if="isSelf" class="small muted">(себя заблокировать нельзя)</span>
      </label>
    </template>
    <template #footer>
      <button
        v-if="user"
        type="button"
        class="btn btn-danger"
        style="margin-right: auto"
        :disabled="busy || user.active_sessions === 0"
        @click="run(() => adminApi.revokeSessions(user!.id), 'Все сессии завершены')"
      >
        Завершить все сессии
      </button>
      <button type="button" class="btn btn-secondary" @click="open = false">Закрыть</button>
      <button type="button" class="btn btn-primary" :disabled="busy || !user" @click="save">Сохранить</button>
    </template>
  </AppDialog>
</template>
