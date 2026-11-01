import { api, streamRequest } from './client'
import { readSse, type SseEvent } from './sse'
import type { ChatItem, ChatOut, SearchFiltersIn, SearchMode } from './types'

export interface AskBody {
  question: string
  chat_id?: number | null
  filters?: SearchFiltersIn
  /** Только гость: его история живёт в браузере. */
  history?: { role: 'user' | 'assistant'; content: string }[]
  /** Режим поиска (этап 5): по фрагментам, по связям или смешанный. */
  mode?: SearchMode
}

export const chatApi = {
  async *ask(body: AskBody, signal: AbortSignal): AsyncGenerator<SseEvent> {
    const res = await streamRequest('/chat/ask', body, signal)
    yield* readSse(res)
  },
  list: (q?: string) => api<ChatItem[]>('/chats', { query: { q } }),
  get: (id: number) => api<ChatOut>(`/chats/${id}`),
  rename: (id: number, title: string) => api<ChatItem>(`/chats/${id}`, { method: 'PATCH', body: { title } }),
  remove: (id: number) => api<void>(`/chats/${id}`, { method: 'DELETE' }),
  feedback: (chatId: number, messageId: number, value: -1 | 0 | 1) =>
    api<void>(`/chats/${chatId}/messages/${messageId}/feedback`, { method: 'POST', body: { value } }),
}
