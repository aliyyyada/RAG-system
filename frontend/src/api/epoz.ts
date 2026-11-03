import { api } from './client'

// Проверка ЕПоЗ: администрирование редакций (backend/epoz/api.py, право epoz.manage).

export interface EpozPartInfo {
  title: string
  present: boolean
  document_id?: number
  file_name?: string
  inherited?: boolean
  uploaded_at?: string
  status?: 'ok' | 'warning' | 'error'
  messages?: string[]
  chunks?: number | null
  clauses?: number | null
  method?: string | null
  headings?: number | null
  headings_matched?: number | null
  refs_rate?: number | null
}

export interface EpozReport {
  edition_id: number
  status: string
  parts: Record<string, EpozPartInfo>
  missing_parts: string[]
  missing_refs: string[]
  blockers: string[]
  warnings: string[]
  can_activate: boolean
}

export interface EpozStatus {
  active: { id: number; title: string; activated_at: string | null; activated_by: string | null; report: EpozReport } | null
  draft: { id: number; created_by: string | null; created_at: string; report: EpozReport } | null
  groups: { id: number; name: string }[]
  group_ids: number[]
  customer_group: 'I' | 'II' | null
  part_titles: Record<string, string>
}

export interface EpozUploadResult {
  file: string
  part: string | null
  ok: boolean
  status?: string
  message?: string
  messages?: string[]
}

export interface EpozClauseResult {
  part: string
  label: string
  found: boolean
  clauses: { label: string; level: number; is_heading: boolean; text: string }[]
  truncated: boolean
}

export const epozApi = {
  status: () => api<EpozStatus>('/admin/epoz/status'),
  upload: (form: FormData) => api<EpozStatus & { results: EpozUploadResult[] }>('/admin/epoz/draft/files', { form }),
  cancelDraft: () => api<void>('/admin/epoz/draft', { method: 'DELETE' }),
  activate: (force: boolean) => api<EpozStatus>('/admin/epoz/draft/activate', { method: 'POST', body: { force } }),
  setGroups: (group_ids: number[]) => api<EpozStatus>('/admin/epoz/groups', { method: 'PUT', body: { group_ids } }),
  setSettings: (customer_group: 'I' | 'II' | null) =>
    api<EpozStatus>('/admin/epoz/settings', { method: 'PUT', body: { customer_group } }),
  clauses: (part: string, label: string, draft: boolean) =>
    api<EpozClauseResult>('/admin/epoz/clauses', { query: { part, label, draft } }),
  okpd2: () => api<{ active: Okpd2Edition | null }>('/admin/okpd2'),
  okpd2Import: (form: FormData) => api<Okpd2Report>('/admin/okpd2/import', { form }),
  okpd2Lookup: (query: { code?: string; q?: string }) => api<Okpd2Lookup>('/admin/okpd2/lookup', { query }),
}

export interface Okpd2Edition {
  id: number
  title: string
  effective_date: string | null
  records: number
  imported_by: string | null
  imported_at: string | null
}

export interface Okpd2Report {
  edition_id: number
  title: string
  records: number
  status: 'imported' | 'unchanged'
  warnings: string[]
}

export interface Okpd2Lookup {
  found: boolean
  chain?: { code: string; name: string; level: number }[]
  hits?: { code: string; name: string; score: number }[]
}
