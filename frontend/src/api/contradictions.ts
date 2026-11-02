import { api } from './client'

// Противоречия (этап 6). Backend показывает только пары, где человеку доступны оба документа.

export type ReviewStatus = 'new' | 'confirmed' | 'rejected'
export type ActiveFilter = 'active' | 'inactive' | 'all'

export interface ContradictionSide {
  document: { id: number; caption: string; legal_status: string | null; doc_date: string | null }
  chunk_id: number
  place: string
  excerpt: string
  quote: string
}

export interface Contradiction {
  id: number
  verdict: 'contradiction' | 'possible'
  verdict_label: string
  topic: string
  explanation: string
  found_at: string
  origin: 'vector' | 'graph'
  similarity: number | null
  review_status: ReviewStatus
  review_label: string
  reviewed_by: string | null
  reviewed_at: string | null
  review_note: string | null
  active: boolean
  inactive_reason: string | null
  sides: ContradictionSide[]
}

export interface ContradictionList {
  items: Contradiction[]
  total: number
  page: number
  page_size: number
  counts: Record<ReviewStatus, number>
  topics: string[]
  unavailable_sources: { id: number; name: string; message: string }[]
}

export interface ContradictionQuery {
  status?: ReviewStatus
  active?: ActiveFilter
  topic?: string | null
  document_id?: number | null
  page?: number
  page_size?: number
}

export interface ContradictionsAdmin {
  enabled: boolean
  model: string
  documents: { indexed: number; done: number; pending: number; cancelled: number }
  checks: { contradiction: number; possible: number; none: number; error: number }
  found: number
  reviews: Record<ReviewStatus, number>
  seconds_per_pair: number
  seconds_basis: 'measured' | 'assumption'
  pairs_per_document: number
  pairs_basis: 'measured' | 'assumption'
  forecast_seconds: number
  forecast_hours: number
  jobs: number
  settings: { neighbors: number; min_similarity: number; graph_neighbors: number; graph_min_similarity: number }
}

export const contradictionsApi = {
  list: (q: ContradictionQuery) =>
    api<ContradictionList>('/contradictions', { query: q as Record<string, unknown> }),
  count: () => api<{ new: number }>('/contradictions/count'),
  review: (id: number, status: ReviewStatus, note?: string) =>
    api<Contradiction>(`/contradictions/${id}/review`, { method: 'POST', body: { status, note } }),
}

export const contradictionsAdminApi = {
  status: () => api<ContradictionsAdmin>('/admin/contradictions'),
  setEnabled: (enabled: boolean) =>
    api<{ enabled: boolean; queued_documents: number }>('/admin/contradictions/enabled', {
      method: 'POST',
      body: { enabled },
    }),
  rescan: () => api<{ queued_documents: number }>('/admin/contradictions/rescan', { method: 'POST' }),
  retryErrors: () => api<{ pairs: number }>('/admin/contradictions/retry-errors', { method: 'POST' }),
}
