import { api, downloadFile } from './client'
import type { DocumentFilters, UserDocumentList, UserDocumentView, ViewChunkList } from './types'

export interface UserDocumentQuery {
  q?: string
  source?: string | null
  doc_type?: string | null
  date_from?: string | null
  date_to?: string | null
  page?: number
  page_size?: number
}

// Документы для пользователя: backend отдаёт только то, что разрешили источники.
export const documentsApi = {
  list: (query: UserDocumentQuery) =>
    api<UserDocumentList>('/documents', { query: query as Record<string, unknown> }),
  filters: () => api<DocumentFilters>('/documents/filters'),
  get: (id: number) => api<UserDocumentView>(`/documents/${id}`),
  chunks: (id: number, page = 1, pageSize = 500) =>
    api<ViewChunkList>(`/documents/${id}/chunks`, { query: { page, page_size: pageSize } }),
  download: (docId: number, fileId: number, name: string) =>
    downloadFile(`/documents/${docId}/files/${fileId}`, name),
}
