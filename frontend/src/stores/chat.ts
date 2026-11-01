import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { ApiError } from '@/api/client'
import { chatApi } from '@/api/chat'
import type { AnswerSource, ChatItem, ChatOut, SearchFiltersIn, SearchMode, UnavailableSource } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

/**
 * Состояние экрана «Поиск»: текущий диалог, поток ответа, история чатов, фильтры.
 *
 * Живёт в Pinia, а не в компоненте: человек открывает источник (документ на нужном месте),
 * возвращается «К ответу» — и видит тот же диалог, а не пустой экран.
 * История вошедшего — на сервере (90 дней); гостя — только в этом браузере (localStorage).
 */

export type TurnStage = 'search' | 'rerank' | 'queue' | 'generate' | 'done' | 'error' | 'interrupted'

export interface Turn {
  key: number
  question: string
  askedAt: string
  answer: string
  sources: AnswerSource[]
  stage: TurnStage
  found?: number
  selected?: number
  position?: number
  elapsedMs?: number
  error?: string
  warning?: string
  noAnswer?: boolean
  messageId?: number
  feedback?: number | null
  unavailable: UnavailableSource[]
}

const GUEST_KEY = 'rag.guest-chat.v1'
let keySeq = 1

function guestLoad(): Turn[] {
  try {
    const raw = localStorage.getItem(GUEST_KEY)
    const turns = raw ? (JSON.parse(raw) as Turn[]) : []
    return turns.map((t) => ({ ...t, key: keySeq++ }))
  } catch {
    return []
  }
}

function fromServer(chat: ChatOut): Turn[] {
  const turns: Turn[] = []
  for (const m of chat.messages) {
    if (m.role === 'user') {
      turns.push({
        key: keySeq++,
        question: m.content,
        askedAt: m.created_at,
        answer: '',
        sources: [],
        stage: 'interrupted',
        unavailable: [],
      })
    } else {
      const t = turns[turns.length - 1]
      if (!t) continue
      const meta = m.meta as Record<string, unknown>
      t.answer = m.content
      t.sources = m.sources
      t.messageId = m.id
      t.feedback = m.feedback
      t.stage = m.status === 'done' ? 'done' : m.status === 'error' ? 'error' : 'interrupted'
      t.elapsedMs = typeof meta.elapsed_ms === 'number' ? meta.elapsed_ms : undefined
      t.found = typeof meta.found === 'number' ? meta.found : undefined
      t.noAnswer = meta.no_answer === true
      if (m.status === 'error') t.error = 'Ответ не был сформирован: модель была недоступна.'
      if (m.status === 'pending') t.error = 'Ответ ещё пишется или был прерван.'
    }
  }
  return turns
}

export const useChatStore = defineStore('chat', () => {
  const auth = useAuthStore()
  const chatId = ref<number | null>(null)
  const turns = ref<Turn[]>([])
  const chats = ref<ChatItem[]>([])
  const historyDays = ref(90)
  const filters = ref<SearchFiltersIn>({})
  // Режим поиска (этап 5): «Смешанный» по умолчанию; выбор помним в этом браузере.
  const MODE_KEY = 'rag.search-mode'
  const savedMode = localStorage.getItem(MODE_KEY)
  const mode = ref<SearchMode>(savedMode === 'chunks' || savedMode === 'graph' ? savedMode : 'mixed')
  watch(mode, (m) => localStorage.setItem(MODE_KEY, m))
  const busy = ref(false)
  const loadingChat = ref(false)
  const loadError = ref<string | null>(null)
  /** Ответ, чьи источники показаны справа (по умолчанию — последний). */
  const activeKey = ref<number | null>(null)
  /** Номер источника, на который навели или нажали в тексте ответа. */
  const activeCite = ref<number | null>(null)
  let controller: AbortController | null = null
  let owner: string | null = null

  const activeTurn = computed(
    () => turns.value.find((t) => t.key === activeKey.value) ?? turns.value[turns.value.length - 1] ?? null,
  )

  /** Другой человек вошёл в этом браузере — чужой диалог не показываем. */
  function ensureOwner() {
    const who = auth.user ? `u:${auth.user.id}` : 'guest'
    if (owner === who) return
    owner = who
    controller?.abort()
    chatId.value = null
    chats.value = []
    activeKey.value = null
    turns.value = auth.isGuest ? guestLoad() : []
  }

  watch(
    () => turns.value.filter((t) => t.stage === 'done' || t.stage === 'error'),
    (done) => {
      if (owner === 'guest') localStorage.setItem(GUEST_KEY, JSON.stringify(done.slice(-30)))
    },
    { deep: true },
  )

  async function loadChats(q?: string) {
    if (auth.isGuest) return
    try {
      chats.value = await chatApi.list(q)
    } catch {
      /* список истории не критичен */
    }
  }

  function newChat() {
    controller?.abort()
    chatId.value = null
    turns.value = []
    activeKey.value = null
    if (auth.isGuest) localStorage.removeItem(GUEST_KEY)
  }

  async function openChat(id: number) {
    if (chatId.value === id && turns.value.length) return
    controller?.abort()
    loadingChat.value = true
    loadError.value = null
    try {
      const chat = await chatApi.get(id)
      chatId.value = chat.id
      historyDays.value = chat.history_days
      turns.value = fromServer(chat)
      activeKey.value = null
    } catch (e) {
      loadError.value = e instanceof ApiError && e.status === 404 ? 'Чат не найден или уже удалён' : 'Не удалось открыть чат'
      chatId.value = null
      turns.value = []
    } finally {
      loadingChat.value = false
    }
  }

  async function removeChat(id: number) {
    await chatApi.remove(id)
    if (chatId.value === id) newChat()
    chats.value = chats.value.filter((c) => c.id !== id)
  }

  function guestHistory() {
    const h: { role: 'user' | 'assistant'; content: string }[] = []
    for (const t of turns.value) {
      if (t.stage !== 'done' || t.noAnswer) continue
      h.push({ role: 'user', content: t.question }, { role: 'assistant', content: t.answer })
    }
    return h.slice(-6)
  }

  async function ask(question: string): Promise<number | null> {
    const q = question.trim()
    if (!q || busy.value) return chatId.value
    const history = auth.isGuest ? guestHistory() : undefined
    const turn: Turn = {
      key: keySeq++,
      question: q,
      askedAt: new Date().toISOString(),
      answer: '',
      sources: [],
      stage: 'search',
      unavailable: [],
    }
    turns.value.push(turn)
    // Дальше меняем реактивную копию, иначе Vue не увидит обновлений.
    const t = turns.value[turns.value.length - 1] as Turn
    activeKey.value = t.key
    activeCite.value = null
    busy.value = true
    controller = new AbortController()
    const startedNew = chatId.value === null
    try {
      for await (const ev of chatApi.ask(
        { question: q, chat_id: chatId.value, filters: filters.value, history, mode: mode.value },
        controller.signal,
      )) {
        const d = ev.data as Record<string, any>
        switch (ev.event) {
          case 'meta':
            if (d.chat_id) chatId.value = d.chat_id
            if (d.message_id) t.messageId = d.message_id
            t.unavailable = d.unavailable_sources ?? []
            break
          case 'status':
            t.stage = d.stage
            if (d.found !== undefined) t.found = d.found
            if (d.selected !== undefined) t.selected = d.selected
            if (d.position !== undefined) t.position = d.position
            break
          case 'sources':
            t.sources = d.items ?? []
            t.selected = t.sources.length
            if (d.warning) t.warning = d.warning
            break
          case 'token':
            if (t.stage !== 'generate') t.stage = 'generate'
            t.answer += d.text ?? ''
            break
          case 'done':
            t.answer = d.answer ?? t.answer
            if (d.sources) t.sources = d.sources
            t.elapsedMs = d.elapsed_ms
            t.noAnswer = d.no_answer === true
            if (d.warning) t.warning = d.warning
            t.stage = 'done'
            break
          case 'error':
            t.error = d.message ?? 'Не удалось ответить'
            t.stage = 'error'
            break
        }
      }
      if (t.stage !== 'done' && t.stage !== 'error') {
        t.stage = 'error'
        t.error = 'Соединение прервалось. Попробуйте задать вопрос ещё раз.'
      }
    } catch (e) {
      if (controller.signal.aborted) {
        t.stage = 'interrupted'
      } else {
        t.stage = 'error'
        t.error = e instanceof ApiError ? e.message : 'Нет связи с сервером. Проверьте сеть и попробуйте ещё раз.'
      }
    } finally {
      busy.value = false
      controller = null
    }
    if (startedNew && chatId.value !== null) void loadChats()
    return chatId.value
  }

  function stop() {
    controller?.abort()
  }

  async function sendFeedback(turn: Turn, value: -1 | 0 | 1) {
    if (chatId.value === null || turn.messageId === undefined) return
    await chatApi.feedback(chatId.value, turn.messageId, value)
    const t = turns.value.find((x) => x.key === turn.key)
    if (t) t.feedback = value || null
  }

  return {
    chatId,
    turns,
    chats,
    historyDays,
    filters,
    mode,
    busy,
    loadingChat,
    loadError,
    activeKey,
    activeCite,
    activeTurn,
    ensureOwner,
    loadChats,
    newChat,
    openChat,
    removeChat,
    ask,
    stop,
    sendFeedback,
  }
})
