<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import AppPager from '@/components/ui/AppPager.vue'
import FormError from '@/components/ui/FormError.vue'
import { adminApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { AdminUser, Group, Permission, Role, UserPage } from '@/api/types'
import {
  ACCOUNT_TYPE_BADGE,
  ACCOUNT_TYPE_LABEL,
  formatDateTime,
  plural,
} from '@/utils/format'
import AssignRoleDialog from './dialogs/AssignRoleDialog.vue'
import GroupDialog from './dialogs/GroupDialog.vue'
import LocalUserDialog from './dialogs/LocalUserDialog.vue'
import UserEditDialog from './dialogs/UserEditDialog.vue'

// Экран «Пользователи и группы» по макету.
const route = useRoute()
const router = useRouter()

const tab = computed(() => (route.query.tab === 'groups' ? 'groups' : 'users'))
const PAGE_SIZE = 25

const q = ref('')
const groupFilter = ref<number | null>(null)
const typeFilter = ref<string | null>(null)
const page = ref(1)

const data = ref<UserPage | null>(null)
const groups = ref<Group[]>([])
const roles = ref<Role[]>([])
const permissions = ref<Permission[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const assignOpen = ref(false)
const localOpen = ref(false)
const editOpen = ref(false)
const editId = ref<number | null>(null)
const groupOpen = ref(false)
const groupId = ref<number | null>(null)

async function loadUsers() {
  loading.value = true
  error.value = null
  try {
    data.value = await adminApi.users({
      q: q.value.trim() || undefined,
      group_id: groupFilter.value,
      account_type: typeFilter.value,
      page: page.value,
      page_size: PAGE_SIZE,
    })
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить пользователей'
  } finally {
    loading.value = false
  }
}

async function loadRefs() {
  try {
    const [g, r, p] = await Promise.all([adminApi.groups(), adminApi.roles(), adminApi.permissions()])
    groups.value = g
    roles.value = r
    permissions.value = p
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить справочники'
  }
}

async function reload() {
  await Promise.all([loadUsers(), loadRefs()])
}

onMounted(reload)

let timer: number | undefined
watch(q, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => {
    page.value = 1
    void loadUsers()
  }, 300)
})
watch([groupFilter, typeFilter], () => {
  page.value = 1
  void loadUsers()
})

function go(p: number) {
  page.value = p
  void loadUsers()
}

function setTab(t: 'users' | 'groups') {
  void router.replace({ query: t === 'groups' ? { tab: 'groups' } : {} })
}

function rolesText(u: AdminUser): string {
  const names = u.roles.map((r) => r.name)
  // «Пользователь» есть у всех вошедших; у администратора не дублируем (как в макете).
  if (u.roles.some((r) => roles.value.find((x) => x.id === r.id)?.builtin_key === 'admin')) {
    return names.join(', ')
  }
  return ['Пользователь', ...names].join(', ')
}

function edit(u: AdminUser) {
  editId.value = u.id
  editOpen.value = true
}

async function unlock(u: AdminUser) {
  try {
    await adminApi.unlock(u.id)
    await loadUsers()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось разблокировать'
  }
}

function openGroup(id: number | null) {
  groupId.value = id
  groupOpen.value = true
}

const usersTotal = computed(() => data.value?.total ?? 0)
const isFiltered = computed(() => !!(q.value.trim() || groupFilter.value || typeFilter.value))
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Пользователи и группы</h1>
        <p>Пользователь появляется здесь после первого входа. Права = роли пользователя + роли его групп.</p>
      </div>
      <template v-if="tab === 'users'">
        <button class="btn btn-secondary" type="button" @click="localOpen = true">
          <AppIcon name="plus" />Локальная учётка
        </button>
        <button class="btn btn-primary" type="button" @click="assignOpen = true">
          <AppIcon name="plus" />Назначить роль
        </button>
      </template>
      <button v-else class="btn btn-primary" type="button" @click="openGroup(null)">
        <AppIcon name="plus" />Новая группа
      </button>
    </div>

    <div class="tabs" role="tablist" style="margin-bottom: 16px">
      <a
        href="?"
        role="tab"
        :aria-selected="tab === 'users'"
        :class="{ on: tab === 'users' }"
        @click.prevent="setTab('users')"
        >Пользователи<span class="cnt">{{ usersTotal }}</span></a
      >
      <a
        href="?tab=groups"
        role="tab"
        :aria-selected="tab === 'groups'"
        :class="{ on: tab === 'groups' }"
        @click.prevent="setTab('groups')"
        >Группы<span class="cnt">{{ groups.length }}</span></a
      >
    </div>

    <FormError :message="error" style="margin-bottom: 12px" />

    <template v-if="tab === 'users'">
      <div class="row" style="margin-bottom: 12px">
        <div class="input-search" style="width: 360px">
          <AppIcon name="search" />
          <input v-model="q" class="input" placeholder="ФИО или табельный номер" aria-label="Поиск пользователя" />
        </div>
        <select v-model="groupFilter" class="select" style="width: 180px" aria-label="Группа">
          <option :value="null">Все группы</option>
          <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
        </select>
        <select v-model="typeFilter" class="select" style="width: 160px" aria-label="Тип учётки">
          <option :value="null">Все типы</option>
          <option value="external">Внешний</option>
          <option value="local">Локальный</option>
          <option value="test">Тестовый</option>
        </select>
      </div>
      <div class="card">
        <table class="tbl" :aria-busy="loading">
          <thead>
            <tr>
              <th>Таб. №</th>
              <th>ФИО</th>
              <th>Подразделение</th>
              <th>Группы</th>
              <th>Роли</th>
              <th>Учётка</th>
              <th>Последний вход</th>
              <th>Статус</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="loading && !data">
              <td colspan="9"><div class="skel" style="width: 60%"></div></td>
            </tr>
            <tr v-for="u in data?.items ?? []" :key="u.id">
              <td class="num mono" style="white-space: nowrap">{{ u.personnel_number }}</td>
              <td><b>{{ u.full_name || '—' }}</b></td>
              <td>{{ u.department || '—' }}</td>
              <td>{{ u.groups.map((g) => g.name).join(', ') || '—' }}</td>
              <td>{{ rolesText(u) }}</td>
              <td>
                <span class="badge plain" :class="ACCOUNT_TYPE_BADGE[u.account_type]">{{ ACCOUNT_TYPE_LABEL[u.account_type] }}</span>
                <div v-if="u.is_emergency" class="small muted">аварийная</div>
              </td>
              <td class="num">{{ formatDateTime(u.last_login_at) }}</td>
              <td>
                <span v-if="u.status === 'active'" class="badge b-ok">Активен</span>
                <template v-else-if="u.status === 'locked'">
                  <span class="badge b-danger">Заблокирован</span>
                  <div class="small muted">
                    {{ u.failed_attempts }} {{ plural(u.failed_attempts, 'неудачная попытка', 'неудачные попытки', 'неудачных попыток') }}
                  </div>
                </template>
                <template v-else>
                  <span class="badge b-danger">Заблокирован</span>
                  <div class="small muted">администратором</div>
                </template>
              </td>
              <td>
                <button v-if="u.status === 'locked'" class="btn btn-ghost btn-sm" type="button" @click="unlock(u)">
                  Разблокировать
                </button>
                <button v-else class="btn btn-ghost btn-sm" type="button" @click="edit(u)">Изменить</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="data && !data.items.length" style="display: grid; min-height: 240px">
          <StateBlock
            icon="empty"
            tone="plain"
            :title="isFiltered ? 'Никого не нашли' : 'Пользователей пока нет'"
            :text="isFiltered ? 'Измените условия поиска.' : 'Люди появятся после первого входа. Роль можно назначить заранее по табельному номеру.'"
          />
        </div>
        <AppPager
          v-if="data && data.items.length"
          :page="data.page"
          :page-size="data.page_size"
          :total="data.total"
          :shown="data.items.length"
          @go="go"
        />
      </div>
    </template>

    <template v-else>
      <div class="card">
        <table class="tbl">
          <thead>
            <tr>
              <th>Группа</th>
              <th>Описание</th>
              <th>Роли</th>
              <th class="r">Участников</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="g in groups" :key="g.id">
              <td><b>{{ g.name }}</b></td>
              <td>{{ g.description || '—' }}</td>
              <td>{{ g.roles.map((r) => r.name).join(', ') || '—' }}</td>
              <td class="r num">{{ g.members_count }}</td>
              <td><button class="btn btn-ghost btn-sm" type="button" @click="openGroup(g.id)">Изменить</button></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!groups.length" style="display: grid; min-height: 240px">
          <StateBlock icon="users" tone="plain" title="Групп пока нет" text="Создайте группу (например, «Отдел ОТ и ПБ»), добавьте в неё людей и назначьте роли." />
        </div>
      </div>
    </template>

    <AssignRoleDialog v-model:open="assignOpen" :roles="roles" :groups="groups" @saved="reload" />
    <LocalUserDialog v-model:open="localOpen" :roles="roles" :groups="groups" @saved="reload" />
    <UserEditDialog
      v-model:open="editOpen"
      :user-id="editId"
      :roles="roles"
      :groups="groups"
      :permissions="permissions"
      @saved="loadUsers"
    />
    <GroupDialog v-model:open="groupOpen" :group-id="groupId" :roles="roles" @saved="reload" />
  </div>
</template>
