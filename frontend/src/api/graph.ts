import { api } from './client'
import type { DocumentGraph, GraphAdmin } from './types'

// Граф знаний (этап 5). Пользователю — только то, что подтверждено доступными ему документами.
export const graphApi = {
  document: (id: number) => api<DocumentGraph>(`/documents/${id}/graph`),
}

export const graphAdminApi = {
  status: () => api<GraphAdmin>('/admin/graph'),
  setLlm: (enabled: boolean) =>
    api<{ enabled: boolean; queued_documents: number }>('/admin/graph/llm', { method: 'POST', body: { enabled } }),
  estimate: (sample: number) =>
    api<{ queued: boolean }>('/admin/graph/estimate', { method: 'POST', body: { sample } }),
  rebuildLinks: () => api<{ queued_documents: number }>('/admin/graph/rebuild-links', { method: 'POST' }),
  retryErrors: () => api<{ chunks: number }>('/admin/graph/retry-errors', { method: 'POST' }),
}
