import { api } from './client'
import type { AdminUserDetail, Group, GroupDetail, Permission, Role, UserPage } from './types'

export interface UserQuery {
  q?: string
  group_id?: number | null
  account_type?: string | null
  status?: string | null
  page?: number
  page_size?: number
}

export const adminApi = {
  users: (query: UserQuery) =>
    api<UserPage>('/admin/users', { query: query as Record<string, unknown> }),
  user: (id: number) => api<AdminUserDetail>(`/admin/users/${id}`),
  assign: (body: { personnel_number: string; full_name?: string; role_ids: number[]; group_ids: number[] }) =>
    api<AdminUserDetail>('/admin/users', { method: 'POST', body }),
  createLocal: (body: {
    personnel_number: string
    full_name: string
    department?: string
    password: string
    is_emergency: boolean
    role_ids: number[]
    group_ids: number[]
  }) => api<AdminUserDetail>('/admin/users/local', { method: 'POST', body }),
  updateUser: (
    id: number,
    body: Partial<{
      full_name: string
      department: string
      is_active: boolean
      role_ids: number[]
      group_ids: number[]
      password: string
    }>,
  ) => api<AdminUserDetail>(`/admin/users/${id}`, { method: 'PATCH', body }),
  unlock: (id: number) => api<AdminUserDetail>(`/admin/users/${id}/unlock`, { method: 'POST' }),
  revokeSessions: (id: number) =>
    api<AdminUserDetail>(`/admin/users/${id}/revoke-sessions`, { method: 'POST' }),

  groups: () => api<Group[]>('/admin/groups'),
  group: (id: number) => api<GroupDetail>(`/admin/groups/${id}`),
  createGroup: (body: { name: string; description: string; role_ids: number[]; member_ids: number[] }) =>
    api<GroupDetail>('/admin/groups', { method: 'POST', body }),
  updateGroup: (
    id: number,
    body: Partial<{ name: string; description: string; role_ids: number[]; member_ids: number[] }>,
  ) => api<GroupDetail>(`/admin/groups/${id}`, { method: 'PATCH', body }),
  deleteGroup: (id: number) => api<void>(`/admin/groups/${id}`, { method: 'DELETE' }),

  permissions: () => api<Permission[]>('/admin/permissions'),
  roles: () => api<Role[]>('/admin/roles'),
  createRole: (body: { name: string; description: string; permissions: string[] }) =>
    api<Role>('/admin/roles', { method: 'POST', body }),
  updateRole: (id: number, body: Partial<{ name: string; description: string; permissions: string[] }>) =>
    api<Role>(`/admin/roles/${id}`, { method: 'PATCH', body }),
  deleteRole: (id: number) => api<void>(`/admin/roles/${id}`, { method: 'DELETE' }),
}

/** Роли, которые можно назначать людям: без встроенных «Гость» и «Пользователь». */
export function assignableRoles(roles: Role[], isAdmin: boolean) {
  return roles
    .filter((r) => r.builtin_key !== 'guest' && r.builtin_key !== 'user')
    .map((r) => ({
      id: r.id,
      name: r.name,
      disabled: r.builtin_key === 'admin' && !isAdmin,
      hint: r.builtin_key === 'admin' && !isAdmin ? 'выдаёт только администратор' : undefined,
    }))
}
