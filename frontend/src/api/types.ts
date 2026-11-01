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

// ---------------------------------------------------------------------------
// Этап 2: источники, документы, фрагменты
// ---------------------------------------------------------------------------

export type DocStatus = 'queued' | 'processing' | 'ready' | 'error'

export interface SyncRun {
  id: number
  kind: 'incremental' | 'reconcile'
  status: 'running' | 'ok' | 'error'
  started_at: string
  finished_at: string | null
  stats: Record<string, number>
  error: string | null
}

export interface SourceReport {
  at_source: number | null
  known: number
  ready: number
  queued: number
  processing: number
  error: number
  deleted: number
  excluded: number
  rate_per_minute: number
  eta_minutes: number | null
  progress: number
  errors_by_stage: Record<string, number>
  runs: SyncRun[]
}

export interface Source {
  id: number
  code: string
  name: string
  adapter: string
  base_url: string
  enabled: boolean
  sync_interval_minutes: number
  overlap_minutes: number
  reconcile_hour: number
  available: boolean | null
  last_error: string | null
  last_sync_at: string | null
  last_reconcile_at: string | null
  last_checked_at: string | null
  created_at: string
  has_callback_token: boolean
  collection_id: number | null
  report: SourceReport
}

export interface SourceSettings {
  name: string
  base_url: string
  enabled: boolean
  sync_interval_minutes: number
  overlap_minutes: number
  reconcile_hour: number
}

export interface DocumentRow {
  id: number
  caption: string
  number: string | null
  title: string
  doc_type: string | null
  doc_date: string | null
  source_id: number | null
  source_name: string | null
  collection_id: number
  external_id: string | null
  version: string | null
  indexed_version: string | null
  status: DocStatus
  error_stage: string | null
  error_stage_title: string | null
  error_message: string | null
  attempts: number
  is_common: boolean
  is_excluded: boolean
  deleted_at: string | null
  deleted_reason: string | null
  chunk_count: number
  status_changed_at: string
}

export interface DocumentPage {
  items: DocumentRow[]
  total: number
  page: number
  page_size: number
}

export interface DocFile {
  id: number
  name: string
  mime: string
  size: number
  sha256: string
  pages: number | null
  ocr_pages: number
  warnings: string[]
}

export interface DocEvent {
  id: number
  created_at: string
  event: string
  version: string | null
  details: Record<string, unknown>
}

export interface DocumentCard extends DocumentRow {
  topic: string | null
  header: Record<string, unknown>
  parts_summary: { name: string; rows: number; declared: number | null }[]
  files: DocFile[]
  events: DocEvent[]
  collection_name: string
  manual: boolean
}

export interface ChunkItem {
  id: number
  ord: number
  kind: 'header' | 'text' | 'table'
  text: string
  section: string | null
  point: string | null
  page_from: number | null
  page_to: number | null
  token_estimate: number
  file_id: number | null
}

export interface ChunkPage {
  items: ChunkItem[]
  total: number
  page: number
  page_size: number
}

export interface CollectionInfo {
  id: number
  name: string
  kind: 'source' | 'manual' | 'topic'
  source_id: number | null
  documents: number
  errors: number
}

// ---------- Документы для пользователя (этап 3) ----------

/** Источник, который не ответил на запрос прав: его документы скрыты. */
export interface UnavailableSource {
  id: number
  name: string
  message: string
}

export interface UserDocument {
  id: number
  caption: string
  number: string | null
  title: string
  doc_type: string | null
  doc_date: string | null
  topic: string | null
  source_name: string
  legal_status: string | null
  is_common: boolean
}

export interface UserDocumentList {
  items: UserDocument[]
  total: number
  total_in_system: number
  page: number
  page_size: number
  unavailable_sources: UnavailableSource[]
}

export interface DocumentFilters {
  sources: { value: string; label: string }[]
  collections: { value: string; label: string }[]
  doc_types: string[]
  unavailable_sources: UnavailableSource[]
}

export interface UserDocumentView extends UserDocument {
  fields: { label: string; value: string }[]
  description: string | null
  files: { id: number; name: string; mime: string; size: number; pages: number | null }[]
  parts: { name: string; rows: number }[]
  version: string | null
}

export interface ViewChunk {
  id: number
  ord: number
  kind: 'header' | 'text' | 'table'
  text: string
  section: string | null
  point: string | null
  page_from: number | null
  page_to: number | null
  file_id: number | null
}

export interface ViewChunkList {
  items: ViewChunk[]
  total: number
  page: number
  page_size: number
}

/** Кому открыта ручная загрузка (админка). */
export interface ManualDocumentAccess {
  is_common: boolean
  users: { id: number; personnel_number: string; full_name: string }[]
  groups: { id: number; name: string }[]
  all_groups: { id: number; name: string }[]
}

// ---------- Поиск и чат (этап 4) ----------

export interface SearchFiltersIn {
  source?: string | null
  collection_id?: number | null
  doc_type?: string | null
  date_from?: string | null
  date_to?: string | null
  only_active?: boolean
}

/** Источник ответа: номер [n] в тексте ответа = n. */
export interface AnswerSource {
  n: number
  document_id?: number
  chunk_id?: number
  caption: string
  place?: string
  source_name?: string
  legal_status?: string | null
  /** Отрывок; найденные слова обрамлены ⟦ и ⟧. */
  snippet?: string
  score?: number
  cited: boolean
  /** false — права на документ с тех пор отозваны (только в истории). */
  available?: boolean
}

export interface ChatItem {
  id: number
  title: string
  created_at: string
  updated_at: string
}

export interface ChatMessageOut {
  id: number
  role: 'user' | 'assistant'
  content: string
  sources: AnswerSource[]
  status: 'pending' | 'done' | 'error' | 'interrupted'
  meta: Record<string, unknown>
  feedback: number | null
  created_at: string
}

export interface ChatOut extends ChatItem {
  messages: ChatMessageOut[]
  history_days: number
}
