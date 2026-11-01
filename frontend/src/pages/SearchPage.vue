<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import AnswerText from '@/components/chat/AnswerText.vue'
import SourceCard from '@/components/chat/SourceCard.vue'
import { documentsApi } from '@/api/documents'
import type { DocumentFilters } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useChatStore, type Turn } from '@/stores/chat'
import { plainAnswer } from '@/utils/answer'
import { plural } from '@/utils/format'

// «Поиск» по макету chat: история слева, диалог в центре, источники ответа справа.
// Ответ идёт потоком; номера [n] в тексте ведут на источник и открывают документ на месте.
const auth = useAuthStore()
const chat = useChatStore()
const route = useRoute()
const router = useRouter()

const question = ref('')
const msgs = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
const opts = ref<DocumentFilters | null>(null)
const histQ = ref('')
const period = ref('')
const showAll = ref(false)

const PERIODS = [
  { value: '', label: 'любая' },
  { value: '1', label: 'за год' },
  { value: '3', label: 'за 3 года' },
  { value: '5', label: 'за 5 лет' },
]

function dateFrom(years: string): string | null {
  if (!years) return null
  const d = new Date()
  d.setFullYear(d.getFullYear() - Number(years))
  return d.toISOString().slice(0, 10)
}
watch(period, (p) => {
  chat.filters = { ...chat.filters, date_from: dateFrom(p) }
})

function scrollDown() {
  void nextTick(() => msgs.value?.scrollTo({ top: msgs.value.scrollHeight }))
}

async function openFromRoute() {
  const id = Number(route.query.chat)
  if (id && !auth.isGuest) {
    await chat.openChat(id)
    scrollDown()
  } else if (!id && chat.chatId !== null && !chat.busy) {
    chat.newChat()
  }
}

onMounted(async () => {
  chat.ensureOwner()
  void chat.loadChats()
  void openFromRoute()
  scrollDown()
  try {
    opts.value = await documentsApi.filters()
  } catch {
    /* фильтры не критичны */
  }
  input.value?.focus()
})
watch(() => route.query.chat, () => void openFromRoute())
watch(() => auth.user?.id, () => {
  chat.ensureOwner()
  void chat.loadChats()
})

let histTimer: number | undefined
watch(histQ, (q) => {
  window.clearTimeout(histTimer)
  histTimer = window.setTimeout(() => void chat.loadChats(q.trim() || undefined), 300)
})

async function send() {
  const q = question.value.trim()
  if (!q || chat.busy) return
  question.value = ''
  showAll.value = false
  scrollDown()
  const wasNew = chat.chatId === null
  const p = chat.ask(q)
  // Новый чат вошедшего: адрес /?chat=7, чтобы можно было обновить страницу.
  const stopWatch = watch(
    () => chat.chatId,
    (id) => {
      if (wasNew && id !== null) void router.replace({ query: { chat: String(id) } })
    },
  )
  await p
  stopWatch()
  scrollDown()
}
watch(() => chat.activeTurn?.answer, scrollDown)

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    void send()
  }
}

function startNew() {
  chat.newChat()
  void router.replace({ query: {} })
  question.value = ''
  input.value?.focus()
}

// ---------- История ----------

const groups = computed(() => {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const yesterday = new Date(today.getTime() - 86_400_000)
  const out: { title: string; items: typeof chat.chats }[] = [
    { title: 'Сегодня', items: [] },
    { title: 'Вчера', items: [] },
    { title: 'Ранее', items: [] },
  ]
  for (const c of chat.chats) {
    const d = new Date(c.updated_at)
    const g = d >= today ? out[0] : d >= yesterday ? out[1] : out[2]
    g?.items.push(c)
  }
  return out.filter((g) => g.items.length)
})

// ---------- Ответ и источники ----------

const lastTurn = computed(() => chat.turns[chat.turns.length - 1] ?? null)
const unavailable = computed(() => lastTurn.value?.unavailable ?? [])

const shownSources = computed(() => {
  const t = chat.activeTurn
  if (!t || t.noAnswer) return []
  const cited = t.sources.filter((s) => s.cited)
  return showAll.value || !cited.length ? t.sources : cited
})
const hiddenCount = computed(() => (chat.activeTurn?.sources.length ?? 0) - shownSources.value.length)

function docsCount(t: Turn) {
  const cited = t.sources.filter((s) => s.cited)
  return new Set((cited.length ? cited : t.sources).map((s) => s.document_id ?? s.caption)).size
}

function onCite(t: Turn, n: number) {
  chat.activeKey = t.key
  chat.activeCite = n
  const src = t.sources.find((s) => s.n === n)
  if (src && !src.cited) showAll.value = true
  void nextTick(() => document.querySelector(`.src-list [data-n="${n}"]`)?.scrollIntoView({ block: 'nearest' }))
}

function time(iso: string) {
  return new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
}

function seconds(ms?: number) {
  if (ms === undefined) return ''
  return ms < 10_000 ? `${(ms / 1000).toFixed(1).replace('.', ',')} с` : `${Math.round(ms / 1000)} с`
}

const copied = ref<number | null>(null)
async function copy(t: Turn) {
  try {
    await navigator.clipboard.writeText(plainAnswer(t.answer))
    copied.value = t.key
    window.setTimeout(() => (copied.value = null), 2000)
  } catch {
    /* буфер обмена недоступен (не HTTPS) */
  }
}

function stageText(t: Turn) {
  if (t.stage === 'search') return 'Ищу в документах'
  if (t.stage === 'rerank') return 'Отбираю лучшие фрагменты'
  if (t.stage === 'queue') return `Ожидание модели: вы ${(t.position ?? 0) + 1}-й в очереди`
  return 'Пишу ответ'
}
const pending = (t: Turn) => ['search', 'rerank', 'queue', 'generate'].includes(t.stage)

const filtersOn = computed(() => {
  const f = chat.filters
  return !!(f.source || f.collection_id || f.doc_type || f.date_from || f.only_active)
})
function resetFilters() {
  chat.filters = {}
  period.value = ''
}
</script>

<template>
  <div class="chat chat-app">
    <aside class="chat-side" aria-label="История чатов">
      <div class="top">
        <button class="btn btn-primary" type="button" @click="startNew"><AppIcon name="plus" />Новый вопрос</button>
        <div v-if="!auth.isGuest" class="input-search">
          <AppIcon name="search" />
          <input v-model="histQ" class="input" placeholder="Поиск по истории" aria-label="Поиск по истории" />
        </div>
      </div>
      <div class="hist">
        <template v-if="!auth.isGuest">
          <template v-for="g in groups" :key="g.title">
            <h5>{{ g.title }}</h5>
            <RouterLink
              v-for="c in g.items"
              :key="c.id"
              :to="{ name: 'search', query: { chat: c.id } }"
              :class="{ on: c.id === chat.chatId }"
              :title="c.title"
            >{{ c.title }}</RouterLink>
          </template>
          <p v-if="!chat.chats.length" class="hist-empty">{{ histQ ? 'Ничего не найдено' : 'Здесь появятся ваши вопросы' }}</p>
        </template>
        <p v-else class="hist-empty">
          Без входа история хранится только в этом браузере.
          <RouterLink :to="{ name: 'login' }">Войдите</RouterLink>, чтобы она сохранялась.
        </p>
      </div>
      <div class="foot">{{ auth.isGuest ? 'Без входа доступны только общие документы' : `История хранится ${chat.historyDays} дней` }}</div>
    </aside>

    <section class="chat-main">
      <div class="chat-filters" role="group" aria-label="Фильтры поиска">
        <label class="flt">
          Источник
          <select v-model="chat.filters.source" class="select select-sm">
            <option :value="undefined">все</option>
            <option v-for="s in opts?.sources ?? []" :key="s.value" :value="s.value">{{ s.label }}</option>
          </select>
        </label>
        <label class="flt">
          Коллекция
          <select v-model="chat.filters.collection_id" class="select select-sm">
            <option :value="undefined">все</option>
            <option v-for="c in opts?.collections ?? []" :key="c.value" :value="Number(c.value)">{{ c.label }}</option>
          </select>
        </label>
        <label class="flt">
          Тип
          <select v-model="chat.filters.doc_type" class="select select-sm">
            <option :value="undefined">все</option>
            <option v-for="t in opts?.doc_types ?? []" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>
        <label class="flt">
          Дата
          <select v-model="period" class="select select-sm">
            <option v-for="p in PERIODS" :key="p.value" :value="p.value">{{ p.label }}</option>
          </select>
        </label>
        <button
          type="button"
          class="chip"
          :class="{ on: chat.filters.only_active }"
          :aria-pressed="!!chat.filters.only_active"
          @click="chat.filters = { ...chat.filters, only_active: !chat.filters.only_active }"
        >
          <AppIcon v-if="chat.filters.only_active" name="check" :size="15" />Только действующие
        </button>
        <span class="grow"></span>
        <button v-if="filtersOn" class="btn btn-ghost btn-sm" type="button" @click="resetFilters">Сбросить</button>
      </div>

      <div ref="msgs" class="msgs" aria-live="polite">
        <div v-if="auth.isGuest" class="alert a-info">
          <AppIcon name="info" />
          <div>
            <b>Вы вошли как гость</b>Ответы строятся только по общим документам.
            <RouterLink :to="{ name: 'login' }">Войти</RouterLink>
          </div>
        </div>
        <div v-for="u in unavailable" :key="u.id" class="alert a-warn" role="status">
          <AppIcon name="plug" />
          <div><b>{{ u.message }}</b>Ответ построен по остальным источникам.</div>
        </div>
        <div v-if="chat.loadError" class="alert a-danger" role="alert">
          <AppIcon name="warn" />
          <div><b>{{ chat.loadError }}</b><a href="#" @click.prevent="startNew">Задать новый вопрос</a></div>
        </div>

        <div v-if="chat.loadingChat" class="steps"><span class="cur"><span class="typing"><i></i><i></i><i></i></span>Загружаю диалог</span></div>

        <StateBlock
          v-if="!chat.turns.length && !chat.loadingChat && !chat.loadError"
          icon="search"
          tone="plain"
          title="Задайте вопрос по документам"
          text="Система найдёт ответ в документах, доступных вам, и покажет, откуда он взят. Например: «Кто подписывает наряд-допуск на работы на высоте?»"
        />

        <template v-for="t in chat.turns" :key="t.key">
          <div class="q"><small>Вы · {{ time(t.askedAt) }}</small>{{ t.question }}</div>
          <div class="a" :class="{ 'a-active': t.key === chat.activeTurn?.key && chat.turns.length > 1 }" @click="chat.activeKey = t.key">
            <div class="a-h">
              <span class="a-mark">AI</span>
              <template v-if="pending(t) && !t.answer">Готовлю ответ</template>
              <template v-else-if="t.noAnswer">Ответ не найден</template>
              <template v-else-if="t.stage === 'error' && !t.answer">Ошибка</template>
              <template v-else-if="t.sources.length">Ответ по {{ docsCount(t) }} {{ plural(docsCount(t), 'документу', 'документам', 'документам') }}</template>
              <template v-else>Ответ</template>
            </div>

            <div v-if="pending(t) && !t.answer" class="steps">
              <span v-if="t.found !== undefined" class="done"><AppIcon name="check" :size="15" />Найдено {{ t.found }} {{ plural(t.found, 'фрагмент', 'фрагмента', 'фрагментов') }}</span>
              <span v-if="t.stage === 'queue' || t.stage === 'generate'" class="done"><AppIcon name="check" :size="15" />Отобрано {{ t.selected }} лучших</span>
              <span class="cur"><span class="typing"><i></i><i></i><i></i></span>{{ stageText(t) }}</span>
            </div>

            <div v-if="t.noAnswer" class="a-body no-answer">
              <p><b>{{ t.answer }}</b></p>
              <p class="muted">
                Переформулируйте вопрос<template v-if="filtersOn"> или сбросьте фильтры</template>.
                <template v-if="auth.isGuest"> Гостю доступны только общие документы: возможно, ответ есть в документах, открытых после входа.</template>
              </p>
            </div>
            <div v-else-if="t.answer" class="a-body">
              <AnswerText
                :text="t.answer"
                :sources="t.sources.length"
                :streaming="t.stage === 'generate'"
                @cite="(n) => onCite(t, n)"
                @hover="(n) => (chat.activeCite = n)"
              />
            </div>
            <div v-if="t.warning" class="small muted">{{ t.warning }}</div>
            <div v-if="t.error" class="alert a-danger" role="alert"><AppIcon name="warn" /><div><b>{{ t.error }}</b></div></div>
            <div v-if="t.stage === 'interrupted' && !t.error" class="small muted">Ответ остановлен</div>

            <div v-if="t.stage === 'done' && !t.noAnswer" class="a-foot">
              <template v-if="t.elapsedMs !== undefined"><span>Сгенерировано за {{ seconds(t.elapsedMs) }}</span><span>·</span></template>
              <a href="#" @click.prevent="copy(t)">{{ copied === t.key ? 'Скопировано' : 'Скопировать' }}</a>
              <template v-if="!auth.isGuest && t.messageId">
                <span>·</span>
                <a v-if="t.feedback !== -1" href="#" @click.prevent="chat.sendFeedback(t, -1)">Ответ неточный</a>
                <span v-else>Отмечено как неточный, спасибо</span>
              </template>
            </div>
          </div>
        </template>
      </div>

      <div class="composer">
        <div class="composer-box">
          <textarea
            ref="input"
            v-model="question"
            rows="2"
            placeholder="Задайте вопрос по документам предприятия"
            aria-label="Вопрос"
            maxlength="2000"
            @keydown="onKey"
          ></textarea>
          <button v-if="chat.busy" class="btn btn-secondary" type="button" @click="chat.stop()"><AppIcon name="x" />Остановить</button>
          <button v-else class="btn btn-primary" type="button" :disabled="!question.trim()" @click="send"><AppIcon name="send" />Отправить</button>
        </div>
        <p class="small muted" style="margin-top: 6px">Enter — отправить, Shift+Enter — новая строка. Ответ строится только по документам, доступным вам.</p>
      </div>
    </section>

    <aside class="sources" aria-label="Источники ответа">
      <div class="sources-h">
        <h3>Источники ответа</h3>
        <p class="small muted">Нажмите, чтобы открыть документ на этом месте</p>
      </div>
      <div class="src-list">
        <SourceCard v-for="s in shownSources" :key="`${chat.activeTurn?.key}-${s.n}`" :source="s" :active="chat.activeCite === s.n" />
        <button v-if="hiddenCount > 0" class="btn btn-ghost btn-sm" type="button" @click="showAll = true">
          Показать все найденные ({{ chat.activeTurn?.sources.length }})
        </button>
        <p v-if="!shownSources.length" class="small muted src-empty">
          <template v-if="chat.activeTurn && pending(chat.activeTurn)">Ищу подходящие фрагменты…</template>
          <template v-else-if="chat.activeTurn?.noAnswer">В доступных вам документах подходящих фрагментов нет.</template>
          <template v-else>Здесь появятся документы, на которые опирается ответ.</template>
        </p>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.chat-app {
  height: calc(100vh - var(--header-h));
  grid-template-columns: 272px minmax(0, 1fr) 400px;
}
@media (max-width: 1280px) {
  .chat-app {
    grid-template-columns: 220px minmax(0, 1fr) 320px;
  }
}
.flt {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  font-size: var(--fs-13);
  color: var(--ink-500);
}
.select-sm {
  height: 32px;
  width: auto;
  min-width: 96px;
  max-width: 190px;
  text-overflow: ellipsis;
  font-size: var(--fs-14);
  color: var(--ink-900);
}
.chip.on {
  border-color: var(--accent);
  background: var(--accent-tint);
  color: var(--ink-900);
  font-weight: 600;
}
.hist-empty {
  padding: var(--s-3) var(--s-4);
  font-size: var(--fs-13);
  color: var(--ink-500);
}
.a-active .a-body {
  border-color: var(--line-strong);
}
.no-answer {
  border-left: 4px solid var(--line-strong);
}
.src-empty {
  padding: var(--s-3);
}
</style>
