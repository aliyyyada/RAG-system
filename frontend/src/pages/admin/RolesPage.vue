<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import FormError from '@/components/ui/FormError.vue'
import { adminApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { Permission, Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import RoleDialog from './dialogs/RoleDialog.vue'

// Экран «Роли и права» по макету: матрица «право × роль» из галочек.
// Галочки меняются локально, кнопка «Сохранить» отправляет изменённые роли.
const auth = useAuthStore()
const roles = ref<Role[]>([])
const catalog = ref<Permission[]>([])
const draft = ref<Record<number, Set<string>>>({})
const loading = ref(true)
const saving = ref(false)
const error = ref<string | null>(null)
const info = ref<string | null>(null)
const dialogOpen = ref(false)
const dialogRole = ref<Role | null>(null)

async function load() {
  loading.value = true
  error.value = null
  try {
    const [r, p] = await Promise.all([adminApi.roles(), adminApi.permissions()])
    roles.value = r
    catalog.value = p
    draft.value = Object.fromEntries(r.map((x) => [x.id, new Set(x.permissions)]))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить роли'
  } finally {
    loading.value = false
  }
}
onMounted(load)

const groups = computed(() => {
  const out: { title: string; items: Permission[] }[] = []
  for (const p of catalog.value) {
    let g = out.find((x) => x.title === p.group)
    if (!g) out.push((g = { title: p.group, items: [] }))
    g.items.push(p)
  }
  return out
})

function disabled(role: Role, perm: Permission): boolean {
  if (role.builtin_key === 'admin') return true // у администратора всегда все права
  if (role.builtin_key === 'guest' && !perm.guest_allowed) return true // гостю — только общие документы
  // Нельзя выдать или отнять право, которого нет у тебя самого (кроме администратора).
  if (!auth.me?.is_admin && !auth.can(perm.code)) return true
  return false
}

function checked(role: Role, code: string): boolean {
  return draft.value[role.id]?.has(code) ?? false
}

function toggle(role: Role, code: string, on: boolean) {
  const next = new Set(draft.value[role.id])
  if (on) next.add(code)
  else next.delete(code)
  draft.value = { ...draft.value, [role.id]: next }
}

function same(a: Set<string>, b: string[]): boolean {
  return a.size === b.length && b.every((x) => a.has(x))
}

const changed = computed(() =>
  roles.value.filter((r) => r.builtin_key !== 'admin' && !same(draft.value[r.id] ?? new Set(), r.permissions)),
)

function rowDirty(code: string): boolean {
  return changed.value.some((r) => (draft.value[r.id]?.has(code) ?? false) !== r.permissions.includes(code))
}

async function save() {
  saving.value = true
  error.value = null
  info.value = null
  try {
    const order = catalog.value.map((p) => p.code)
    for (const r of changed.value) {
      const perms = order.filter((c) => draft.value[r.id]?.has(c))
      await adminApi.updateRole(r.id, { permissions: perms })
    }
    const n = changed.value.length
    await load()
    await auth.reloadMe() // если поменялись права моей роли — шапка обновится сразу
    info.value = n ? 'Права сохранены' : null
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить'
    await load()
  } finally {
    saving.value = false
  }
}

function openRole(role: Role | null) {
  dialogRole.value = role
  dialogOpen.value = true
}
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Роли и права</h1>
        <p>Изменения применяются сразу. Каждое изменение пишется в журнал аудита.</p>
      </div>
      <button class="btn btn-secondary" type="button" @click="openRole(null)"><AppIcon name="plus" />Новая роль</button>
      <button class="btn btn-primary" type="button" :disabled="saving || !changed.length" @click="save">
        {{ saving ? 'Сохраняем…' : 'Сохранить' }}
      </button>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />
    <div v-if="info" class="alert a-ok" role="status" style="margin-bottom: 12px"><div>{{ info }}</div></div>
    <div class="card" style="overflow-x: auto">
      <table class="tbl matrix" :aria-busy="loading">
        <thead>
          <tr>
            <th style="width: 44%">Право</th>
            <th v-for="r in roles" :key="r.id">
              <template v-if="r.builtin_key">
                {{ r.name }} <AppIcon name="lock" :size="14" style="vertical-align: -2px" />
              </template>
              <button v-else type="button" class="role-h" :title="`Изменить роль «${r.name}»`" @click="openRole(r)">
                {{ r.name }}
              </button>
            </th>
          </tr>
        </thead>
        <tbody>
          <template v-for="g in groups" :key="g.title">
            <tr class="grp"><td :colspan="roles.length + 1">{{ g.title }}</td></tr>
            <tr v-for="p in g.items" :key="p.code" :class="{ dirty: rowDirty(p.code) }">
              <td>
                {{ p.title }}
                <span v-if="p.coming_soon" class="badge b-neutral plain" style="height: 20px; font-size: 11px">скоро</span>
                <span class="code">{{ p.code }}</span>
              </td>
              <td v-for="r in roles" :key="r.id">
                <input
                  type="checkbox"
                  :checked="checked(r, p.code)"
                  :disabled="disabled(r, p)"
                  :aria-label="`${r.name}: ${p.title}`"
                  @change="toggle(r, p.code, ($event.target as HTMLInputElement).checked)"
                />
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
    <p class="small muted" style="margin-top: 10px">
      Серые недоступные флажки у гостя — права, которые нельзя выдать без входа. Встроенного
      администратора нельзя лишить прав, иначе в админку никто не попадёт. Роль «Пользователь» есть у
      каждого вошедшего.
    </p>
    <RoleDialog v-model:open="dialogOpen" :role="dialogRole" @saved="load" />
  </div>
</template>
