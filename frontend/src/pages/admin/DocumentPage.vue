<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import AppPager from '@/components/ui/AppPager.vue'
import FormError from '@/components/ui/FormError.vue'
import ManualAccessCard from './ManualAccessCard.vue'
import { ApiError } from '@/api/client'
import { sourcesApi } from '@/api/sources'
import type { ChunkPage, DocumentCard } from '@/api/types'
import {
  DELETED_REASON,
  DOC_STATUS_BADGE,
  DOC_STATUS_LABEL,
  EVENT_LABEL,
  formatDate,
  formatDateTime,
  formatSize,
  plural,
  shortId,
} from '@/utils/format'

// Карточка документа с фрагментами по макету a-doc.
const route = useRoute()
const id = computed(() => Number(route.params.id))

const doc = ref<DocumentCard | null>(null)
const chunks = ref<ChunkPage | null>(null)
const chunkPage = ref(1)
const texts = ref<Record<number, string>>({})
const tab = ref<'chunks' | 'text' | 'history'>('chunks')
const error = ref<string | null>(null)
const busy = ref(false)

async function load() {
  try {
    const [d, c] = await Promise.all([sourcesApi.document(id.value), sourcesApi.chunks(id.value, chunkPage.value)])
    doc.value = d
    chunks.value = c
    error.value = null
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить документ'
  }
}

// Документ в очереди или обрабатывается — обновляем карточку, пока не станет «готово».
let timer: number | undefined
function poll() {
  window.clearTimeout(timer)
  if (doc.value && (doc.value.status === 'queued' || doc.value.status === 'processing')) {
    timer = window.setTimeout(async () => {
      await load()
      poll()
    }, 3000)
  }
}
onMounted(async () => {
  await load()
  poll()
})
onBeforeUnmount(() => window.clearTimeout(timer))
watch(id, async () => {
  chunkPage.value = 1
  texts.value = {}
  await load()
  poll()
})

watch(tab, async (t) => {
  if (t !== 'text' || !doc.value) return
  for (const f of doc.value.files) {
    if (texts.value[f.id] === undefined) {
      try {
        texts.value[f.id] = (await sourcesApi.fileText(id.value, f.id)).text
      } catch {
        texts.value[f.id] = '(не удалось загрузить текст)'
      }
    }
  }
})

async function act(fn: () => Promise<unknown>) {
  busy.value = true
  error.value = null
  try {
    await fn()
    await load()
    poll()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось выполнить действие'
  } finally {
    busy.value = false
  }
}
const toggleExclude = () => act(() => sourcesApi.exclude(id.value, !doc.value?.is_excluded))
const reindex = () => act(() => sourcesApi.reindex(id.value))
const toggleCommon = () => act(() => sourcesApi.updateDocument(id.value, { is_common: !doc.value?.is_common }))

async function goChunks(p: number) {
  chunkPage.value = p
  chunks.value = await sourcesApi.chunks(id.value, p)
}

const fileName = (fileId: number | null) =>
  fileId === null ? null : (doc.value?.files.find((f) => f.id === fileId)?.name ?? null)

function chunkPlace(c: { kind: string; page_from: number | null; page_to: number | null; file_id: number | null }) {
  if (c.kind === 'header') return 'шапка источника'
  if (c.page_from === null) return fileName(c.file_id) ?? ''
  return c.page_from === c.page_to ? `стр. ${c.page_from}` : `стр. ${c.page_from}–${c.page_to}`
}

function eventText(e: { event: string; details: Record<string, unknown> }): string {
  const d = e.details
  if (e.event === 'indexed') {
    const parts = [`фрагментов ${d.chunks}`, `файлов ${d.files}`]
    if (d.ocr_pages) parts.push(`распознано страниц ${d.ocr_pages}`)
    return parts.join(', ')
  }
  if (e.event === 'error') return `${d.message ?? ''} (попытка ${d.attempt}${d.final ? ', последняя' : ''})`
  if (e.event === 'deleted') return DELETED_REASON[String(d.reason)] ?? ''
  if (d.actor) return `кто: ${d.actor}`
  return ''
}
</script>

<template>
  <div class="page">
    <div class="crumbs">
      <RouterLink :to="{ name: 'admin-documents' }">Коллекции</RouterLink> /
      <RouterLink v-if="doc" :to="{ name: 'admin-documents', query: { collection: doc.collection_id } }">{{ doc.collection_name }}</RouterLink>
      <template v-if="doc"> / {{ doc.number ? '№ ' + doc.number : 'D-' + String(doc.id).padStart(6, '0') }}</template>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />

    <template v-if="doc">
      <div class="page-h">
        <div class="grow">
          <h1>{{ doc.caption.split(' «')[0] }}</h1>
          <p v-if="doc.title && doc.caption.includes('«')">«{{ doc.title }}»</p>
        </div>
        <span v-if="doc.deleted_at" class="badge b-outline">Удалён</span>
        <span v-if="doc.is_excluded" class="badge b-warn plain">Исключён из поиска</span>
        <span class="badge" :class="DOC_STATUS_BADGE[doc.status]">{{ DOC_STATUS_LABEL[doc.status] }}</span>
        <button class="btn btn-secondary" type="button" :disabled="busy" @click="toggleExclude">
          {{ doc.is_excluded ? 'Вернуть в поиск' : 'Исключить из поиска' }}
        </button>
        <button class="btn btn-dark" type="button" :disabled="busy || !!doc.deleted_at" @click="reindex">
          <AppIcon name="refresh" />Переиндексировать
        </button>
      </div>

      <div v-if="doc.status === 'error'" class="alert a-danger" style="margin-bottom: 16px" role="alert">
        <AppIcon name="warn" />
        <div>
          <b>Ошибка на этапе «{{ doc.error_stage_title }}»</b>
          {{ doc.error_message }}. Попыток: {{ doc.attempts }}.
          <template v-if="doc.indexed_version">В поиске остаётся версия от {{ formatDateTime(doc.indexed_version) }}.</template>
        </div>
      </div>
      <div v-else-if="doc.deleted_at" class="alert a-info" style="margin-bottom: 16px">
        <AppIcon name="info" />
        <div><b>Документ не участвует в поиске</b>{{ DELETED_REASON[doc.deleted_reason ?? ''] }}, {{ formatDateTime(doc.deleted_at) }}.</div>
      </div>

      <div style="display: grid; grid-template-columns: 360px 1fr; gap: 16px; align-items: start">
        <div class="stack">
        <div class="card">
          <div class="card-b">
            <dl class="kv" style="grid-template-columns: 130px 1fr">
              <dt>Наш id</dt>
              <dd class="mono">D-{{ String(doc.id).padStart(6, '0') }}</dd>
              <template v-if="doc.external_id">
                <dt>Id в источнике</dt>
                <dd class="mono" :title="doc.external_id">{{ shortId(doc.external_id) }}</dd>
              </template>
              <dt>Версия</dt>
              <dd class="num">{{ formatDateTime(doc.version) }}</dd>
              <dt>Проиндексирована</dt>
              <dd class="num">{{ formatDateTime(doc.indexed_version) }}</dd>
              <template v-if="doc.doc_date">
                <dt>Дата документа</dt>
                <dd class="num">{{ formatDate(doc.doc_date) }}</dd>
              </template>
              <dt>Тема</dt>
              <dd>{{ doc.topic ?? '—' }}</dd>
              <dt>Общий</dt>
              <dd>
                <label class="check">
                  <input type="checkbox" :checked="doc.is_common" :disabled="!doc.manual || busy" @change="toggleCommon" />
                  {{ doc.is_common ? 'Да' : 'Нет' }}{{ doc.manual ? '' : ', из источника' }}
                </label>
              </dd>
              <dt>Файлы</dt>
              <dd>
                <template v-for="f in doc.files" :key="f.id">
                  {{ f.name }}<br />
                  <span class="small muted">
                    <template v-if="f.pages">{{ f.pages }} стр., </template>
                    {{ f.ocr_pages ? `распознано OCR: ${f.ocr_pages} стр.` : f.mime.includes('pdf') ? 'текстовый слой' : 'DOCX' }},
                    {{ formatSize(f.size) }}
                  </span>
                  <div v-for="w in f.warnings" :key="w" class="small" style="color: var(--warn)">{{ w }}</div>
                </template>
                <template v-if="!doc.files.length">—</template>
              </dd>
              <dt>Табличные части</dt>
              <dd>
                <div v-for="p in doc.parts_summary" :key="p.name">
                  «{{ p.name }}» — {{ p.rows }} {{ plural(p.rows, 'строка', 'строки', 'строк') }}
                </div>
                <template v-if="!doc.parts_summary.length">—</template>
              </dd>
              <dt>Фрагментов</dt>
              <dd class="num">{{ doc.chunk_count }}</dd>
            </dl>
          </div>
        </div>
        <ManualAccessCard v-if="doc.manual" :key="doc.id" :document-id="doc.id" :is-common="doc.is_common" />
        </div>

        <div class="card">
          <div class="tabs" style="padding: 0 12px" role="tablist">
            <a href="#" role="tab" :class="{ on: tab === 'chunks' }" @click.prevent="tab = 'chunks'">Фрагменты<span class="cnt">{{ doc.chunk_count }}</span></a>
            <a href="#" role="tab" :class="{ on: tab === 'text' }" @click.prevent="tab = 'text'">Распознанный текст</a>
            <a href="#" role="tab" :class="{ on: tab === 'history' }" @click.prevent="tab = 'history'">История<span class="cnt">{{ doc.events.length }}</span></a>
          </div>

          <div v-if="tab === 'chunks'" class="stack" style="padding: 16px">
            <div v-for="c in chunks?.items ?? []" :key="c.id" class="chunk">
              <div class="chunk-h">
                <b>№ {{ c.ord + 1 }}</b>
                <span>{{ chunkPlace(c) }}</span>
                <span v-if="c.section">раздел {{ c.section }}</span>
                <span v-if="c.point">п. {{ c.point }}</span>
                <span v-if="c.kind === 'table'" class="badge b-neutral plain" style="height: 20px">таблица</span>
                <span class="grow"></span>
                <span class="num">≈ {{ c.token_estimate }} {{ plural(c.token_estimate, 'токен', 'токена', 'токенов') }}</span>
              </div>
              <div class="chunk-b">{{ c.text }}</div>
            </div>
            <p v-if="chunks && !chunks.items.length" class="muted">Фрагментов пока нет: документ ещё не обработан.</p>
            <AppPager
              v-if="chunks && chunks.total > chunks.page_size"
              :page="chunks.page"
              :page-size="chunks.page_size"
              :total="chunks.total"
              :shown="chunks.items.length"
              @go="goChunks"
            />
          </div>

          <div v-else-if="tab === 'text'" class="stack" style="padding: 16px">
            <div v-for="f in doc.files" :key="f.id" class="chunk">
              <div class="chunk-h"><b>{{ f.name }}</b></div>
              <div class="chunk-b">{{ texts[f.id] ?? 'Загрузка…' }}</div>
            </div>
            <p v-if="!doc.files.length" class="muted">У документа нет файлов: он собран из шапки и табличных частей источника.</p>
          </div>

          <table v-else class="tbl">
            <thead>
              <tr><th>Когда</th><th>Событие</th><th>Версия</th><th>Подробности</th></tr>
            </thead>
            <tbody>
              <tr v-for="e in doc.events" :key="e.id">
                <td class="num" style="white-space: nowrap">{{ formatDateTime(e.created_at) }}</td>
                <td>{{ EVENT_LABEL[e.event] ?? e.event }}</td>
                <td class="num" style="white-space: nowrap">{{ formatDateTime(e.version) }}</td>
                <td>{{ eventText(e) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
    <div v-else-if="!error" class="card card-b"><div class="skel" style="width: 40%"></div></div>
  </div>
</template>
