<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import CheckList from '@/components/ui/CheckList.vue'
import FormError from '@/components/ui/FormError.vue'
import { assignableRoles, adminApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { AdminUser, Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

// Создание и изменение группы: название, роли группы, участники.
const open = defineModel<boolean>('open', { required: true })
const props = defineProps<{ groupId: number | null; roles: Role[] }>()
const emit = defineEmits<{ saved: [] }>()
const auth = useAuthStore()

const name = ref('')
const description = ref('')
const roleIds = ref<number[]>([])
const memberIds = ref<number[]>([])
const members = ref<{ id: number; name: string; hint?: string }[]>([])
const search = ref('')
const found = ref<AdminUser[]>([])
const error = ref<string | null>(null)
const busy = ref(false)
const confirmDelete = ref(false)

watch(open, async (v) => {
  if (!v) return
  error.value = null
  confirmDelete.value = false
  search.value = ''
  found.value = []
  if (props.groupId === null) {
    name.value = ''
    description.value = ''
    roleIds.value = []
    memberIds.value = []
    members.value = []
    return
  }
  try {
    const g = await adminApi.group(props.groupId)
    name.value = g.name
    description.value = g.description
    roleIds.value = g.roles.map((r) => r.id)
    members.value = g.members.map((m) => ({ id: m.id, name: m.name }))
    memberIds.value = g.members.map((m) => m.id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить группу'
  }
})

const roleItems = computed(() => assignableRoles(props.roles, !!auth.me?.is_admin))

let timer: number | undefined
watch(search, (q) => {
  window.clearTimeout(timer)
  if (q.trim().length < 2) {
    found.value = []
    return
  }
  timer = window.setTimeout(async () => {
    try {
      found.value = (await adminApi.users({ q: q.trim(), page_size: 10 })).items
    } catch {
      found.value = []
    }
  }, 250)
})

function addMember(u: AdminUser) {
  if (!memberIds.value.includes(u.id)) {
    members.value.push({ id: u.id, name: u.full_name || u.personnel_number, hint: u.personnel_number })
    memberIds.value = [...memberIds.value, u.id]
  }
  search.value = ''
}

async function save() {
  error.value = null
  if (!name.value.trim()) {
    error.value = 'Укажите название группы'
    return
  }
  busy.value = true
  const body = {
    name: name.value.trim(),
    description: description.value.trim(),
    role_ids: roleIds.value,
    member_ids: memberIds.value,
  }
  try {
    if (props.groupId === null) await adminApi.createGroup(body)
    else await adminApi.updateGroup(props.groupId, body)
    open.value = false
    emit('saved')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить'
  } finally {
    busy.value = false
  }
}

async function remove() {
  if (props.groupId === null) return
  if (!confirmDelete.value) {
    confirmDelete.value = true
    return
  }
  busy.value = true
  try {
    await adminApi.deleteGroup(props.groupId)
    open.value = false
    emit('saved')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось удалить'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog v-model:open="open" :title="groupId === null ? 'Новая группа' : 'Группа'" wide>
    <FormError :message="error" />
    <div class="field">
      <label class="label" for="gd-name">Название</label>
      <input id="gd-name" v-model="name" class="input" placeholder="Например, Отдел ОТ и ПБ" />
    </div>
    <div class="field">
      <label class="label" for="gd-desc">Описание</label>
      <textarea id="gd-desc" v-model="description" class="textarea" rows="2"></textarea>
    </div>
    <div class="grid grid-cols-2 gap-4">
      <CheckList v-model="roleIds" label="Роли группы" :items="roleItems" />
      <div class="stack" style="gap: 8px">
        <CheckList v-model="memberIds" label="Участники" :items="members" empty="Участников пока нет" />
        <div class="input-search">
          <svg class="i"><use href="#i-search" /></svg>
          <input v-model="search" class="input" placeholder="Добавить: ФИО или табельный номер" aria-label="Найти пользователя" />
        </div>
        <div v-if="found.length" class="card">
          <button
            v-for="u in found"
            :key="u.id"
            type="button"
            class="hmenu-item"
            :disabled="memberIds.includes(u.id)"
            @click="addMember(u)"
          >
            <span class="mono small">{{ u.personnel_number }}</span>{{ u.full_name }}
          </button>
        </div>
      </div>
    </div>
    <template #footer>
      <button v-if="groupId !== null" type="button" class="btn btn-danger" style="margin-right: auto" :disabled="busy" @click="remove">
        {{ confirmDelete ? 'Точно удалить?' : 'Удалить группу' }}
      </button>
      <button type="button" class="btn btn-secondary" @click="open = false">Отмена</button>
      <button type="button" class="btn btn-primary" :disabled="busy" @click="save">Сохранить</button>
    </template>
  </AppDialog>
</template>
