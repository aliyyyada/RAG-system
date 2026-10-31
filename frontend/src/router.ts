import { createRouter, createWebHistory, type RouteLocationRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    /** Страница без шапки (вход). */
    bare?: boolean
    /** Нужен вход. */
    requiresAuth?: boolean
    /** Нужно хотя бы одно из прав. */
    permissions?: string[]
    title?: string
  }
}

// Разделы админки по макету. Те, что появятся на следующих этапах, ведут на заглушку.
export const ADMIN_SECTIONS = [
  { group: 'Доступ', name: 'admin-users', path: 'users', title: 'Пользователи и группы', icon: 'users', permissions: ['admin.users'] },
  { group: 'Доступ', name: 'admin-roles', path: 'roles', title: 'Роли и права', icon: 'shield', permissions: ['admin.roles'] },
  { group: 'Документы', name: 'admin-sources', path: 'sources', title: 'Источники', icon: 'db', permissions: ['admin.integrations'], stage: 2 },
  { group: 'Документы', name: 'admin-documents', path: 'documents', title: 'Коллекции и документы', icon: 'doc', permissions: ['admin.integrations'], stage: 2 },
  { group: 'Система', name: 'admin-audit', path: 'audit', title: 'Журнал аудита', icon: 'list', permissions: ['audit.view'], stage: 7 },
  { group: 'Система', name: 'admin-health', path: 'health', title: 'Состояние системы', icon: 'cpu', permissions: ['admin.integrations'], stage: 7 },
  { group: 'Система', name: 'admin-api-keys', path: 'api-keys', title: 'API-ключи', icon: 'key', permissions: ['admin.integrations'], stage: 7 },
  { group: 'Система', name: 'admin-skills', path: 'skills', title: 'Навыки', icon: 'puzzle', permissions: ['admin.roles'], stage: 7 },
] as const

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/pages/LoginPage.vue'), meta: { bare: true, title: 'Вход' } },
    { path: '/', name: 'search', component: () => import('@/pages/SearchPage.vue'), meta: { permissions: ['search.common'], title: 'Поиск' } },
    { path: '/documents', name: 'documents', component: () => import('@/pages/PlaceholderPage.vue'), meta: { permissions: ['search.common', 'documents.view'], title: 'Документы' }, props: { title: 'Документы', stage: 4 } },
    { path: '/contradictions', name: 'contradictions', component: () => import('@/pages/PlaceholderPage.vue'), meta: { requiresAuth: true, permissions: ['contradictions.view'], title: 'Противоречия' }, props: { title: 'Противоречия', stage: 6 } },
    {
      path: '/admin',
      component: () => import('@/layouts/AdminLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        { path: '', name: 'admin', redirect: () => firstAdminSection() },
        ...ADMIN_SECTIONS.map((s) => ({
          path: s.path,
          name: s.name,
          component:
            s.name === 'admin-users'
              ? () => import('@/pages/admin/UsersPage.vue')
              : s.name === 'admin-roles'
                ? () => import('@/pages/admin/RolesPage.vue')
                : () => import('@/pages/PlaceholderPage.vue'),
          props: 'stage' in s ? { title: s.title, stage: s.stage, embedded: true } : undefined,
          meta: { requiresAuth: true, permissions: [...s.permissions], title: s.title },
        })),
      ],
    },
    { path: '/forbidden', name: 'forbidden', component: () => import('@/pages/ForbiddenPage.vue'), meta: { title: 'Нет доступа' } },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/pages/NotFoundPage.vue'), meta: { title: 'Страница не найдена' } },
  ],
})

function firstAdminSection(): RouteLocationRaw {
  const auth = useAuthStore()
  const s = ADMIN_SECTIONS.find((x) => auth.can(...x.permissions))
  return s ? { name: s.name } : { name: 'forbidden' }
}

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.bootstrap()
  if (to.name === 'login') {
    return auth.isGuest ? true : { name: 'search' }
  }
  const needsLogin = to.matched.some((r) => r.meta.requiresAuth)
  if (auth.isGuest && (needsLogin || !auth.me?.guest_mode_enabled)) {
    return { name: 'login', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  }
  const perms = to.meta.permissions
  if (perms && !auth.can(...perms)) {
    if (auth.isGuest) return { name: 'login', query: { next: to.fullPath } }
    return { name: 'forbidden' }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} — Поиск по документам` : 'Поиск по документам'
})
