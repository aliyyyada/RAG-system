// Типы ответов backend (backend/app/api/auth.py и admin.py).

export interface UserInfo {
  id: number
  personnel_number: string
  full_name: string
  department: string | null
  account_type: AccountType
}

export type AccountType = 'external' | 'local' | 'test'

export interface SkillInfo {
  name: string
  title: string
  available: boolean
}

export interface Me {
  user: UserInfo | null
  permissions: string[]
  roles: string[]
  is_admin: boolean
  can_admin: boolean
  guest_mode_enabled: boolean
  skills: SkillInfo[]
}

export interface TokenResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  me: Me
}

export interface ApiErrorDetail {
  code: string
  message: string
  remaining_attempts?: number
  lockout_minutes?: number
  locked_until?: string
}

export interface Ref {
  id: number
  name: string
}

export type UserStatus = 'active' | 'blocked' | 'locked'

export interface AdminUser {
  id: number
  personnel_number: string
  full_name: string
  department: string | null
  account_type: AccountType
  is_emergency: boolean
  is_active: boolean
  status: UserStatus
  failed_attempts: number
  locked_until: string | null
  last_login_at: string | null
  created_at: string
  roles: Ref[]
  groups: Ref[]
}

export interface AdminUserDetail extends AdminUser {
  effective_permissions: string[]
  effective_roles: string[]
  active_sessions: number
}

export interface UserPage {
  items: AdminUser[]
  total: number
  page: number
  page_size: number
}

export interface Group {
  id: number
  name: string
  description: string
  roles: Ref[]
  members_count: number
}

export interface GroupDetail extends Group {
  members: Ref[]
}

export interface Permission {
  code: string
  title: string
  group: string
  guest_allowed: boolean
  coming_soon: boolean
}

export type BuiltinKey = 'guest' | 'user' | 'admin'

export interface Role {
  id: number
  name: string
  description: string
  builtin_key: BuiltinKey | null
  permissions: string[]
  users_count: number
  groups_count: number
}
