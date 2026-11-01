import { api } from './client'
import type {
  ChunkPage,
  CollectionInfo,
  DocumentCard,
  DocumentPage,
  DocumentRow,
  ManualDocumentAccess,
  Source,
  SourceSettings,
} from './types'

export interface DocumentQuery {
  source_id?: number | null
  collection_id?: number | null
  status?: string | null
  q?: string
  page?: number
  page_size?: number
}

export const sourcesApi = {
  list: () => api<Source[]>('/admin/sources'),
  get: (id: number) => api<Source>(`/admin/sources/${id}`),
  create: (body: SourceSettings & { code: string }) =>
    api<Source>('/admin/sources', { method: 'POST', body }),
  update: (id: number, body: Partial<SourceSettings>) =>
    api<Source>(`/admin/sources/${id}`, { method: 'PATCH', body }),
  sync: (id: number, reconcile = false) =>
    api<{ queued: boolean }>(`/admin/sources/${id}/sync`, { method: 'POST', body: { reconcile } }),
  retryFailed: (id: number) =>
    api<{ count: number }>(`/admin/sources/${id}/retry-failed`, { method: 'POST' }),
  callbackToken: (id: number) =>
    api<{ token: string }>(`/admin/sources/${id}/callback-token`, { method: 'POST' }),

  collections: () => api<CollectionInfo[]>('/admin/collections'),
  documents: (query: DocumentQuery) =>
    api<DocumentPage>('/admin/documents', { query: query as Record<string, unknown> }),
  document: (id: number) => api<DocumentCard>(`/admin/documents/${id}`),
  chunks: (id: number, page: number, pageSize = 30) =>
    api<ChunkPage>(`/admin/documents/${id}/chunks`, { query: { page, page_size: pageSize } }),
  fileText: (docId: number, fileId: number) =>
    api<{ id: number; name: string; text: string; warnings: string[] }>(
      `/admin/documents/${docId}/files/${fileId}/text`,
    ),
  retry: (id: number) => api<DocumentRow>(`/admin/documents/${id}/retry`, { method: 'POST' }),
  reindex: (id: number) => api<DocumentRow>(`/admin/documents/${id}/reindex`, { method: 'POST' }),
  exclude: (id: number, excluded: boolean) =>
    api<DocumentRow>(`/admin/documents/${id}/exclude`, { method: 'POST', body: { excluded } }),
  updateDocument: (id: number, body: { is_common?: boolean; title?: string }) =>
    api<DocumentRow>(`/admin/documents/${id}`, { method: 'PATCH', body }),
  upload: (form: FormData) => api<{ id: number }>('/documents/manual', { form }),
}

// Этап 3: кэш прав и доступ к ручным загрузкам.
export const accessApi = {
  resetCache: (userId?: number) =>
    api<{ reset: number }>('/admin/access-cache/reset', { method: 'POST', body: userId ? { user_id: userId } : {} }),
  manual: (docId: number) => api<ManualDocumentAccess>(`/admin/documents/${docId}/access`),
  setManual: (docId: number, body: { personnel_numbers: string[]; group_ids: number[] }) =>
    api<ManualDocumentAccess>(`/admin/documents/${docId}/access`, { method: 'PUT', body }),
}
