<script setup lang="ts">
import LegalStatus from '@/components/LegalStatus.vue'
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { documentsApi } from '@/api/documents'
import { graphApi } from '@/api/graph'
import type { DocumentGraph, GraphLink, UserDocumentView, ViewChunk } from '@/api/types'
import { buildToc, chunkPlace, parseChunkText } from '@/utils/documents'
import { formatSize, plural } from '@/utils/format'

// Просмотр документа по макету doc. Открывается из списка или по ссылке на источник
// в ответе (этап 4): /documents/12?chunk=345 — страница прокручена к фрагменту, он выделен.
const route = useRoute()
const router = useRouter()
const id = computed(() => Number(route.params.id))

const MAX_CHUNK_PAGES = 20 // 20 × 500 фрагментов — с запасом для самого длинного документа

const doc = ref<UserDocumentView | null>(null)
const chunks = ref<ViewChunk[]>([])
const denied = ref<{ code: string; message: string } | null>(null)
const error = ref<string | null>(null)
const loading = ref(true)
const downloadError = ref<string | null>(null)
// Связи из графа (этап 5): что документ отменяет и изменяет, чем изменён, что в нём упомянуто.
const graph = ref<DocumentGraph | null>(null)

async function loadGraph() {
  graph.value = null
  try {
    graph.value = await graphApi.document(id.value)
  } catch {
    graph.value = null // связи — дополнение: без них документ всё равно открывается
  }
}

async function load() {
  loading.value = true
  denied.value = null
  error.value = null
  doc.value = null
  chunks.value = []
  try {
    doc.value = await documentsApi.get(id.value)
    void loadGraph()
    const all: ViewChunk[] = []
    for (let page = 1; page <= MAX_CHUNK_PAGES; page++) {
      const part = await documentsApi.chunks(id.value, page)
      all.push(...part.items)
      if (all.length >= part.total || !part.items.length) break
    }
    chunks.value = all
  } catch (e) {
    if (e instanceof ApiError && [403, 404, 503].includes(e.status)) {
      denied.value = { code: e.detail.code, message: e.message }
    } else {
      error.value = e instanceof ApiError ? e.message : 'Не удалось открыть документ'
    }
  } finally {
    loading.value = false
  }
  await nextTick()
  scrollToHit(0)
}
watch(id, load, { immediate: true })

// Найденные места: ?chunk=345 или ?chunk=345,346.
const hits = computed(() =>
  String(route.query.chunk ?? '')
    .split(',')
    .map(Number)
    .filter((n) => n > 0 && chunks.value.some((c) => c.id === n)),
)
const hitIndex = ref(0)
function scrollToHit(i: number) {
  if (!hits.value.length) return
  hitIndex.value = (i + hits.value.length) % hits.value.length
  scrollToChunk(hits.value[hitIndex.value] ?? 0)
}
function scrollToChunk(chunkId: number) {
  document.getElementById(`chunk-${chunkId}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
}
watch(() => route.query.chunk, () => scrollToHit(0))

const toc = computed(() => buildToc(chunks.value))
const activeSection = ref<number | null>(null)

// Блоки листа: заголовок раздела выводим, когда раздел сменился; номер страницы — когда
// сменилась страница.
const sheet = computed(() => {
  let prevSection: string | null = null
  let prevPage: number | null = null
  return chunks.value.map((c) => {
    const heading = c.section && c.section !== prevSection ? c.section.split('›').pop()?.trim() : null
    const page = c.page_from !== null && c.page_from !== prevPage ? c.page_from : null
    prevSection = c.section
    if (c.page_to !== null) prevPage = c.page_to
    return { chunk: c, heading, page, blocks: parseChunkText(c.text) }
  })
})

const headerLine = computed(() => {
  const d = doc.value
  if (!d) return { title: '', subtitle: '' }
  const i = d.caption.indexOf(' «')
  return i > 0 ? { title: d.caption.slice(0, i), subtitle: d.caption.slice(i + 1) } : { title: d.caption, subtitle: '' }
})

async function download(file: { id: number; name: string }) {
  downloadError.value = null
  try {
    await documentsApi.download(id.value, file.id, file.name)
  } catch (e) {
    downloadError.value = e instanceof ApiError ? e.message : 'Не удалось скачать файл'
  }
}
// Связи по макету: сначала исходящие («Отменяет», «Изменяет»), затем входящие («Изменён
// документом»). Ссылка ведёт на место, где связь написана: исходящая — в этом документе,
// входящая — в другом документе. Цель недоступна или её нет в системе — только реквизиты.
const links = computed(() => {
  const g = graph.value
  if (!g) return []
  const order = { cancels: 0, amends: 1, executes: 2, refers: 3 }
  const sort = (a: GraphLink, b: GraphLink) => order[a.kind] - order[b.kind]
  return [...g.outgoing.slice().sort(sort).map((l) => ({ ...l, dir: 'out' as const })), ...g.incoming.slice().sort(sort).map((l) => ({ ...l, dir: 'in' as const }))]
})
function linkTarget(l: GraphLink & { dir: 'in' | 'out' }) {
  if (l.dir === 'out') {
    return l.document ? { name: 'document', params: { id: l.document.id } } : { name: 'document', params: { id: id.value }, query: { chunk: l.chunk_id } }
  }
  return { name: 'document', params: { id: l.evidence_document_id }, query: { chunk: l.chunk_id } }
}
const kindTitle = (l: GraphLink & { dir: 'in' | 'out' }) => l.kind_label.charAt(0).toUpperCase() + l.kind_label.slice(1)

const mainFile = computed(() => doc.value?.files[0] ?? null)
const fileKind = (mime: string) => (mime.includes('pdf') ? 'PDF' : 'DOCX')
</script>

<template>
  <div v-if="denied" class="page" style="display: grid; place-items: center; min-height: 60vh">
    <StateBlock
      v-if="denied.code === 'source_unavailable'"
      icon="plug"
      tone="warn"
      :title="denied.message"
      text="Система не смогла проверить ваши права в этом источнике, поэтому его документы скрыты. Попробуйте открыть документ через минуту."
    >
      <button class="btn btn-secondary" type="button" @click="load"><AppIcon name="refresh" />Повторить</button>
    </StateBlock>
    <StateBlock v-else-if="denied.code === 'login_required'" icon="lock" title="Документ доступен только после входа" text="Без входа доступны только общие документы.">
      <RouterLink class="btn btn-primary" :to="{ name: 'login', query: { next: route.fullPath } }">Войти</RouterLink>
    </StateBlock>
    <StateBlock
      v-else-if="denied.code === 'no_access'"
      icon="lock"
      title="Нет доступа к документу"
      :text="`${denied.message}. Если он нужен для работы, обратитесь к руководителю подразделения.`"
    >
      <RouterLink class="btn btn-secondary" :to="{ name: 'documents' }">К списку документов</RouterLink>
    </StateBlock>
    <StateBlock v-else icon="empty" tone="plain" title="Документ не найден" text="Возможно, он удалён в источнике или исключён из поиска.">
      <RouterLink class="btn btn-secondary" :to="{ name: 'documents' }">К списку документов</RouterLink>
    </StateBlock>
  </div>

  <div v-else-if="error" class="page"><FormError :message="error" /></div>

  <template v-else-if="doc">
    <div class="doc-toolbar">
      <button v-if="route.query.from === 'chat'" class="btn btn-ghost btn-sm" type="button" @click="router.back()"><AppIcon name="back" />К ответу</button>
      <RouterLink v-else class="btn btn-ghost btn-sm" :to="{ name: 'documents' }"><AppIcon name="back" />К списку</RouterLink>
      <div class="grow">
        <h1 class="doc-title">
          {{ headerLine.title }} <span v-if="headerLine.subtitle" class="muted">{{ headerLine.subtitle }}</span>
        </h1>
      </div>
      <LegalStatus :status="doc.legal_status" />
      <span v-if="doc.is_common" class="badge b-outline plain">Общий</span>
      <template v-if="hits.length">
        <span class="small muted num">Фрагмент {{ hitIndex + 1 }} из {{ hits.length }}</span>
        <button class="btn btn-secondary btn-sm" type="button" :disabled="hits.length < 2" @click="scrollToHit(hitIndex - 1)">Предыдущий</button>
        <button class="btn btn-secondary btn-sm" type="button" :disabled="hits.length < 2" @click="scrollToHit(hitIndex + 1)">Следующий</button>
      </template>
      <button v-if="mainFile" class="btn btn-secondary btn-sm" type="button" @click="download(mainFile)">
        <AppIcon name="down" />Скачать {{ fileKind(mainFile.mime) }}
      </button>
    </div>
    <FormError :message="downloadError" />

    <div class="docv" style="height: calc(100vh - 128px); min-height: 520px">
      <nav class="toc" aria-label="Содержание">
        <a
          v-for="t in toc"
          :key="t.chunkId"
          href="#"
          :class="{ sub: t.depth > 0, on: activeSection === t.chunkId }"
          @click.prevent="activeSection = t.chunkId; scrollToChunk(t.chunkId)"
          >{{ t.title }}</a
        >
        <p v-if="!toc.length" class="small muted" style="padding: 0 20px">Документ без разделов.</p>
      </nav>

      <div class="sheet-wrap">
        <article class="sheet" aria-label="Текст документа">
          <p v-if="!chunks.length" class="muted">Текст документа ещё не готов.</p>
          <template v-for="s in sheet" :key="s.chunk.id">
            <div v-if="s.page && s.chunk.page_from !== 1" class="pno">Страница {{ s.page }}</div>
            <h2 v-if="s.heading">{{ s.heading }}</h2>
            <div
              :id="`chunk-${s.chunk.id}`"
              class="chunk-view"
              :class="{ hl: hits.includes(s.chunk.id), 'from-source': s.chunk.kind === 'header' }"
              :data-place="chunkPlace(s.chunk)"
            >
              <template v-for="(b, i) in s.blocks" :key="i">
                <h3 v-if="b.type === 'h'">{{ b.text }}</h3>
                <p v-else-if="b.type === 'p'">{{ b.text }}</p>
                <table v-else>
                  <thead>
                    <tr><th v-for="(h, j) in b.head" :key="j">{{ h }}</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="(r, k) in b.rows" :key="k"><td v-for="(c, j) in r" :key="j">{{ c }}</td></tr>
                  </tbody>
                </table>
              </template>
            </div>
          </template>
        </article>
      </div>

      <aside class="meta-side">
        <div>
          <h4>Реквизиты</h4>
          <dl class="kv" style="margin-top: 10px; grid-template-columns: 120px 1fr">
            <template v-for="f in doc.fields" :key="f.label">
              <dt>{{ f.label }}</dt>
              <dd>{{ f.value }}</dd>
            </template>
          </dl>
        </div>
        <div v-if="doc.description">
          <h4>Описание</h4>
          <p style="margin-top: 10px; font-size: var(--fs-14)">{{ doc.description }}</p>
        </div>
        <div v-if="doc.files.length">
          <h4>Файлы</h4>
          <ul class="files">
            <li v-for="f in doc.files" :key="f.id">
              <a href="#" @click.prevent="download(f)">{{ f.name }}</a>
              <span class="small muted">
                {{ fileKind(f.mime) }}<template v-if="f.pages">, {{ f.pages }} {{ plural(f.pages, 'страница', 'страницы', 'страниц') }}</template>, {{ formatSize(f.size) }}
              </span>
            </li>
          </ul>
        </div>
        <div v-if="doc.parts.length">
          <h4>Табличные части</h4>
          <ul class="files">
            <li v-for="p in doc.parts" :key="p.name">«{{ p.name }}» — {{ p.rows }} {{ plural(p.rows, 'строка', 'строки', 'строк') }}</li>
          </ul>
        </div>
        <div v-if="graph?.status_note" class="alert a-warn" role="status">
          <AppIcon name="warn" />
          <div><b>{{ graph.status_note }}</b>Не применяйте требования этого документа без проверки.</div>
        </div>
        <div v-if="links.length" class="rel" data-testid="doc-links">
          <h4>Связи</h4>
          <RouterLink
            v-for="(l, i) in links"
            :key="i"
            :to="linkTarget(l)"
            :title="l.quote"
          >
            <em>{{ kindTitle(l) }}</em>
            <span class="row" style="gap: 4px">
              <LegalStatus v-if="l.document" :status="l.document.legal_status" />
              <span v-else class="badge b-outline plain" title="Документа нет в системе или он вам недоступен">недоступен</span>
            </span>
            <span>{{ l.label }}</span>
          </RouterLink>
        </div>
        <div v-if="graph?.entities.length" data-testid="doc-entities">
          <h4>Упоминаются</h4>
          <div class="row wrap" style="margin-top: 10px">
            <span v-for="e in graph.entities.slice(0, 24)" :key="e.id" class="badge b-outline plain" :title="e.type_label">{{ e.name }}</span>
          </div>
        </div>
      </aside>
    </div>
  </template>

  <div v-else-if="loading" class="page"><div class="card card-b"><div class="skel" style="width: 40%"></div></div></div>
</template>

<style scoped>
.doc-title {
  font-size: var(--fs-18);
  font-weight: 700;
  margin: 0;
}
.doc-title .muted {
  font-weight: 400;
}
.chunk-view {
  display: grid;
  gap: 10px;
  scroll-margin: 80px;
}
.chunk-view h3 {
  font-size: 15px;
  font-weight: 700;
  font-family: inherit;
}
.chunk-view p {
  margin: 0;
  white-space: pre-wrap;
}
.from-source {
  font-family: var(--font-sans);
  font-size: var(--fs-14);
  border-left: 3px solid var(--line);
  padding-left: 12px;
}
.files {
  list-style: none;
  padding: 0;
  margin: 10px 0 0;
  display: grid;
  gap: 8px;
  font-size: var(--fs-14);
}
.files li {
  display: grid;
}
</style>
