<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import CheckList from '@/components/ui/CheckList.vue'
import FormError from '@/components/ui/FormError.vue'
import { assignableRoles, adminApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { Group, Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

// Локальная учётка: пароль хранится у нас (argon2). Нужна для аварийного администратора
// или служебного входа, когда внешнего сервиса нет.
const open = defineModel<boolean>('open', { required: true })
const props = defineProps<{ roles: Role[]; groups: Group[] }>()
const emit = defineEmits<{ saved: [] }>()
const auth = useAuthStore()

const form = ref({ login: '', name: '', department: '', password: '', password2: '', emergency: false })
const roleIds = ref<number[]>([])
const groupIds = ref<number[]>([])
const error = ref<string | null>(null)
const busy = ref(false)

watch(open, (v) => {
  if (v) {
    form.value = { login: '', name: '', department: '', password: '', password2: '', emergency: false }
    roleIds.value = []
    groupIds.value = []
    error.value = null
  }
})

const roleItems = computed(() => assignableRoles(props.roles, !!auth.me?.is_admin))
const groupItems = computed(() => props.groups.map((g) => ({ id: g.id, name: g.name })))

async function save() {
  error.value = null
  const f = form.value
  if (!f.login.trim() || !f.name.trim()) {
    error.value = 'Укажите логин и ФИО'
    return
  }
  if (f.password.length < 10) {
    error.value = 'Пароль должен быть не короче 10 символов'
    return
  }
  if (f.password !== f.password2) {
    error.value = 'Пароли не совпадают'
    return
  }
  busy.value = true
  try {
    await adminApi.createLocal({
      personnel_number: f.login.trim(),
      full_name: f.name.trim(),
      department: f.department.trim() || undefined,
      password: f.password,
      is_emergency: f.emergency,
      role_ids: roleIds.value,
      group_ids: groupIds.value,
    })
    open.value = false
    emit('saved')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось создать учётку'
  } finally {
    busy.value = false
    form.value.password = ''
    form.value.password2 = ''
  }
}
</script>

<template>
  <AppDialog v-model:open="open" title="Локальная учётка">
    <p class="small muted">
      Пароль хранится в системе, а не во внешнем сервисе. В рабочем режиме входить может только
      аварийная учётка, каждый её вход пишется в журнал аудита.
    </p>
    <FormError :message="error" />
    <div class="grid grid-cols-2 gap-4">
      <div class="field">
        <label class="label" for="lu-login">Логин</label>
        <input id="lu-login" v-model="form.login" class="input" autocomplete="off" />
      </div>
      <div class="field">
        <label class="label" for="lu-dep">Подразделение</label>
        <input id="lu-dep" v-model="form.department" class="input" autocomplete="off" />
      </div>
    </div>
    <div class="field">
      <label class="label" for="lu-name">ФИО или назначение</label>
      <input id="lu-name" v-model="form.name" class="input" autocomplete="off" />
    </div>
    <div class="grid grid-cols-2 gap-4">
      <div class="field">
        <label class="label" for="lu-pw">Пароль</label>
        <input id="lu-pw" v-model="form.password" class="input" type="password" autocomplete="new-password" />
        <span class="hint">Не короче 10 символов</span>
      </div>
      <div class="field">
        <label class="label" for="lu-pw2">Повторите пароль</label>
        <input id="lu-pw2" v-model="form.password2" class="input" type="password" autocomplete="new-password" />
      </div>
    </div>
    <label v-if="auth.me?.is_admin" class="check">
      <input v-model="form.emergency" type="checkbox" /> Аварийная учётка администратора
    </label>
    <CheckList v-model="roleIds" label="Роли" :items="roleItems" />
    <CheckList v-model="groupIds" label="Группы" :items="groupItems" empty="Групп пока нет" />
    <template #footer>
      <button type="button" class="btn btn-secondary" @click="open = false">Отмена</button>
      <button type="button" class="btn btn-primary" :disabled="busy" @click="save">Создать</button>
    </template>
  </AppDialog>
</template>
