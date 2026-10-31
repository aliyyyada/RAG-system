<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import CheckList from '@/components/ui/CheckList.vue'
import FormError from '@/components/ui/FormError.vue'
import { assignableRoles, adminApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { Group, Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

// «Назначить роль»: по табельному номеру, даже если человек ещё ни разу не входил.
const open = defineModel<boolean>('open', { required: true })
const props = defineProps<{ roles: Role[]; groups: Group[] }>()
const emit = defineEmits<{ saved: [] }>()
const auth = useAuthStore()

const number = ref('')
const fullName = ref('')
const roleIds = ref<number[]>([])
const groupIds = ref<number[]>([])
const error = ref<string | null>(null)
const busy = ref(false)

watch(open, (v) => {
  if (v) {
    number.value = ''
    fullName.value = ''
    roleIds.value = []
    groupIds.value = []
    error.value = null
  }
})

const roleItems = computed(() => assignableRoles(props.roles, !!auth.me?.is_admin))
const groupItems = computed(() => props.groups.map((g) => ({ id: g.id, name: g.name })))

async function save() {
  error.value = null
  if (!number.value.trim()) {
    error.value = 'Укажите табельный номер'
    return
  }
  if (!roleIds.value.length && !groupIds.value.length) {
    error.value = 'Выберите хотя бы одну роль или группу'
    return
  }
  busy.value = true
  try {
    await adminApi.assign({
      personnel_number: number.value.trim(),
      full_name: fullName.value.trim() || undefined,
      role_ids: roleIds.value,
      group_ids: groupIds.value,
    })
    open.value = false
    emit('saved')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog v-model:open="open" title="Назначить роль">
    <p class="small muted">
      Роли добавляются к уже назначенным. Если человек ещё не входил, роли применятся при первом
      входе.
    </p>
    <FormError :message="error" />
    <div class="field">
      <label class="label" for="as-num">Табельный номер</label>
      <input id="as-num" v-model="number" class="input" autocomplete="off" />
    </div>
    <div class="field">
      <label class="label" for="as-name">ФИО</label>
      <input id="as-name" v-model="fullName" class="input" autocomplete="off" />
      <span class="hint">Необязательно: при входе ФИО придёт из внешнего сервиса</span>
    </div>
    <CheckList v-model="roleIds" label="Роли" :items="roleItems" />
    <CheckList v-model="groupIds" label="Группы" :items="groupItems" empty="Групп пока нет" />
    <template #footer>
      <button type="button" class="btn btn-secondary" @click="open = false">Отмена</button>
      <button type="button" class="btn btn-primary" :disabled="busy" @click="save">Назначить</button>
    </template>
  </AppDialog>
</template>
