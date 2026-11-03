import { api } from './client'

// Проверка закупочного пакета по ЕПоЗ (backend/epoz/checks/api.py, право skill.epoz_check.use).

export type CheckStatus = 'ok' | 'fail' | 'warn' | 'manual'

export interface CheckConfig {
  ready: boolean
  reason: string | null
  roles: { key: string; label: string }[]
  criteria: { code: string; block: number; block_title: string; title: string }[]
  customer_group: 'I' | 'II' | null
  limits: { max_files: number; max_file_mb: number }
}

export interface CheckItem {
  label: string
  status: CheckStatus
  note?: string
  quote?: { file: string; page: number | null; text: string }
}

export interface CheckResult {
  code: string
  block: number
  block_title: string
  title: string
  description: string
  status: CheckStatus | null
  summary: string
  items: CheckItem[]
  quotes: { file: string; page: number | null; text: string }[]
  epoz: { part: string; part_title: string; label: string; text: string }[]
  values: Record<string, unknown>
  model: string | null
  ms: number | null
}

export interface CheckFile {
  id: number
  name: string
  role: string
  role_label: string
  role_detected: string | null
  role_detected_label: string | null
  is_winner: boolean
  parse_status: 'queued' | 'done' | 'error'
  parse_error: string | null
  pages: number | null
  ocr_pages: number
  data: Record<string, unknown>
}

export interface CheckRun {
  id: number
  status: 'queued' | 'running' | 'done' | 'error' | 'cancelled'
  created_at: string
  finished_at: string | null
  error: string | null
  progress: { stage?: string; done?: number; total?: number; criteria_total?: number; current?: string | null }
  params: { customer_group?: 'I' | 'II' | null }
  epoz: { edition_id: number | null; title: string | null }
  files: CheckFile[]
  results: CheckResult[]
  counts: { ok: number; fail: number; warn: number; manual: number; pending: number }
}

export const checkApi = {
  config: () => api<CheckConfig>('/checks/config'),
  guessRoles: (names: string[]) => {
    const form = new FormData()
    form.append('names', JSON.stringify(names))
    return api<Record<string, string>>('/checks/guess-roles', { form })
  },
  start: (form: FormData) => api<CheckRun>('/checks', { form }),
  current: () => api<{ run: CheckRun | null }>('/checks/current'),
  clear: (id: number) => api<void>(`/checks/${id}`, { method: 'DELETE' }),
}
