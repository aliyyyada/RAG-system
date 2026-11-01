<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from 'reka-ui'
import AppIcon from '@/components/AppIcon.vue'
import LogoSlot from '@/components/LogoSlot.vue'
import { useAuthStore } from '@/stores/auth'
import { initials, shortName } from '@/utils/format'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

// Пункты шапки видны по правам. Это удобство, а не защита: backend проверяет права сам.
const nav = computed(() =>
  [
    { to: '/', label: 'Поиск', show: auth.can('search.common'), active: route.name === 'search' },
    {
      to: '/documents',
      label: 'Документы',
      show: auth.can('search.common', 'documents.view'),
      active: route.name === 'documents' || route.name === 'document',
    },
    {
      to: '/contradictions',
      label: 'Противоречия',
      show: !auth.isGuest && auth.can('contradictions.view'),
      active: route.name === 'contradictions',
    },
    {
      to: '/admin',
      label: 'Администрирование',
      show: !auth.isGuest && !!auth.me?.can_admin,
      active: route.path.startsWith('/admin'),
    },
  ].filter((i) => i.show),
)

const userLine = computed(() => {
  const u = auth.user
  if (!u) return ''
  return shortName(u.full_name, u.personnel_number)
})

const userSub = computed(() => {
  const u = auth.user
  if (!u) return ''
  const first = u.department || (auth.me?.is_admin ? 'Администратор' : '')
  return [first, u.personnel_number].filter(Boolean).join(' · ')
})

const loginLink = computed(() => ({
  name: 'login',
  query: route.fullPath !== '/' ? { next: route.fullPath } : {},
}))

async function logout(everywhere: boolean) {
  await auth.logout(everywhere)
  await router.push(auth.me?.guest_mode_enabled ? '/' : '/login')
}
</script>

<template>
  <header class="hdr">
    <RouterLink to="/" class="brand" style="color: inherit; text-decoration: none">
      <LogoSlot />Поиск по документам
    </RouterLink>
    <nav class="hnav" aria-label="Разделы">
      <RouterLink
        v-for="item in nav"
        :key="item.to"
        :to="item.to"
        :class="{ on: item.active }"
        :aria-current="item.active ? 'page' : undefined"
        >{{ item.label }}</RouterLink
      >
    </nav>
    <div class="hright">
      <template v-if="auth.isGuest">
        <span class="small" style="color: #aeb6b6">Гостевой режим</span>
        <RouterLink class="btn btn-on-dark btn-sm" :to="loginLink">Войти</RouterLink>
      </template>
      <DropdownMenuRoot v-else>
        <DropdownMenuTrigger class="avatar-btn" aria-label="Меню пользователя">
          <div class="huser">
            {{ userLine }}<small>{{ userSub }}</small>
          </div>
          <div class="avatar">{{ initials(auth.user!.full_name, auth.user!.personnel_number) }}</div>
        </DropdownMenuTrigger>
        <DropdownMenuPortal>
          <DropdownMenuContent class="hmenu" align="end" :side-offset="8">
            <DropdownMenuItem class="hmenu-item" @select="logout(false)">
              <AppIcon name="ext" />Выйти
            </DropdownMenuItem>
            <DropdownMenuSeparator class="hmenu-sep" />
            <DropdownMenuItem class="hmenu-item" @select="logout(true)">
              <AppIcon name="lock" />Выйти на всех устройствах
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenuPortal>
      </DropdownMenuRoot>
    </div>
  </header>
</template>
