<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { checkApi, type CheckConfig, type CheckResult, type CheckRun, type CheckStatus } from '@/api/epozCheck'
import { formatDateTime } from '@/utils/format'

// Проверка закупочного пакета по ЕПоЗ. Пользователь загружает файлы, у каждого видна роль
// (подсказана по имени, её можно поменять), у КП — переключатель «победитель». Данные живут
// только в сессии: при выходе, по простою или кнопкой «Очистить» всё стирается.
// Результат носит рекомендательный характер: решение остаётся за специалистом.
interface Row {
  key: number
  file: File
  role: string
}
let seq = 1
const cfg = ref<CheckConfig | null>(null)
const rows = ref<Row[]>([])
const winnerKey = ref<number | null>(null)
const run = ref<CheckRun | null>(null)
const error = ref<string | null>(null)
const busy = ref(false)
const dragging = ref(false)
const showResults = ref(false)
const onlyIssues = ref(false)
const openCode = ref<string | null>(null)
let timer: number | undefined

const STATUS = {
  ok: { cls: 'b-ok', text: 'Соответствует', icon: '✓' },
  fail: { cls: 'b-danger', text: 'Не соответствует', icon: '✕' },
  warn: { cls: 'b-warn', text: 'Требует внимания', icon: '!' },
  manual: { cls: 'b-info', text: 'Нужна ручная проверка', icon: '?' },
  none: { cls: 'b-neutral', text: 'Ожидает', icon: '…' },
} as const
const st = (s: CheckStatus | null) => STATUS[s ?? 'none']

const active = computed(() => run.value?.status === 'queued' || run.value?.status === 'running')
const percent = computed(() => {
  const r = run.value
  if (!r) return 0
  if (r.status === 'done') return 100
  const files = r.files.length
  const crit = r.progress.criteria_total ?? 20
  const done = r.progress.stage === 'criteria' ? files + (r.progress.done ?? 0) : (r.progress.done ?? 0)
  return Math.min(99, Math.round((done / Math.max(1, files + crit)) * 100))
})
const stageText = computed(() => {
  const r = run.value
  if (!r) return ''
  if (r.status === 'queued') return 'В очереди на проверку…'
  if (r.status === 'error') return 'Проверка не удалась'
  if (r.status === 'done') return 'Проверка завершена'
  return r.progress.stage === 'criteria'
    ? `Проверяю критерии: ${r.progress.done ?? 0} из ${r.progress.criteria_total ?? 20}${r.progress.current ? ' · ' + r.progress.current : ''}`
    : `Читаю файлы: ${r.progress.done ?? 0} из ${r.files.length} (сканы распознаются дольше)`
})

async function loadRun(announce = false) {
  const prev = run.value?.status
  try {
    run.value = (await checkApi.current()).run
    if (announce && prev && prev !== 'done' && run.value?.status === 'done') showResults.value = true
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось получить ход проверки'
  }
}
function schedule() {
  window.clearTimeout(timer)
  timer = window.setTimeout(async () => {
    await loadRun(true)
    if (active.value) schedule()
  }, 2500)
}

onMounted(async () => {
  try {
    cfg.value = await checkApi.config()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить настройки проверки'
  }
  await loadRun()
  if (active.value) schedule()
})
onUnmounted(() => window.clearTimeout(timer))

async function add(files: File[]) {
  if (!files.length || !cfg.value) return
  const room = cfg.value.limits.max_files - rows.value.length
  if (files.length > room) error.value = `В пакете не больше ${cfg.value.limits.max_files} файлов`
  const take = files.slice(0, Math.max(0, room))
  let guess: Record<string, string> = {}
  try {
    guess = await checkApi.guessRoles(take.map((f) => f.name))
  } catch {
    /* роли выберет пользователь */
  }
  for (const f of take) rows.value.push({ key: seq++, file: f, role: guess[f.name] ?? 'other' })
  rows.value.sort((a, b) => a.file.name.localeCompare(b.file.name, 'ru', { numeric: true }))
}
const onPick = (e: Event) => {
  const input = e.target as HTMLInputElement
  void add(Array.from(input.files ?? []))
  input.value = ''
}
const onDrop = (e: DragEvent) => {
  dragging.value = false
  void add(Array.from(e.dataTransfer?.files ?? []))
}
function remove(key: number) {
  rows.value = rows.value.filter((r) => r.key !== key)
  if (winnerKey.value === key) winnerKey.value = null
}
function setRole(r: Row, role: string) {
  r.role = role
  if (role !== 'kp' && winnerKey.value === r.key) winnerKey.value = null
}
const kpRows = computed(() => rows.value.filter((r) => r.role === 'kp'))

async function start() {
  if (!rows.value.length) return
  if (run.value && !window.confirm('Прежняя проверка и её данные будут удалены. Продолжить?')) return
  busy.value = true
  error.value = null
  try {
    const form = new FormData()
    rows.value.forEach((r) => {
      form.append('files', r.file)
      form.append('roles', r.role)
    })
    form.append('winner', String(rows.value.findIndex((r) => r.key === winnerKey.value)))
    run.value = await checkApi.start(form)
    rows.value = []
    winnerKey.value = null
    showResults.value = false
    schedule()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось запустить проверку'
  } finally {
    busy.value = false
  }
}
async function clear() {
  if (!run.value || !window.confirm('Удалить результаты и загруженные файлы?')) return
  busy.value = true
  try {
    await checkApi.clear(run.value.id)
    run.value = null
    showResults.value = false
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось очистить данные'
  } finally {
    busy.value = false
  }
}

const blocks = computed(() => {
  const out: { block: number; title: string; items: CheckResult[] }[] = []
  for (const r of run.value?.results ?? []) {
    if (onlyIssues.value && r.status === 'ok') continue
    let b = out.find((x) => x.block === r.block)
    if (!b) out.push((b = { block: r.block, title: r.block_title, items: [] }))
    b.items.push(r)
  }
  return out
})
const toggle = (code: string) => (openCode.value = openCode.value === code ? null : code)
const pageText = (p: number | null) => (p ? `, стр. ${p}` : '')
</script>

<template>
  <div class="page" style="max-width: 1080px">
    <div class="page-h">
      <div class="grow">
        <h1>Проверка ЕПоЗ</h1>
        <p>Загрузите документы закупочного пакета и нажмите «Проверить»: система сверит их с Единым положением о закупке.</p>
      </div>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />

    <StateBlock v-if="cfg && !cfg.ready" icon="lock" tone="warn" title="Проверка сейчас недоступна" :text="cfg.reason ?? ''" />

    <template v-else-if="cfg">
      <div v-if="!cfg.customer_group" class="alert a-warn" style="margin-bottom: 12px">
        <AppIcon name="warn" />
        <div>Администратор не задал группу заказчика: срок оплаты по Положению будет отмечен «Нужна ручная проверка».</div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h">
          <h3 class="grow">Пакет документов</h3>
          <span class="small muted">Файлы видны только вам и удаляются при выходе из системы</span>
        </div>
        <div class="card-b">
          <label class="dropzone" :class="{ on: dragging }" data-testid="check-drop" @dragover.prevent="dragging = true" @dragleave="dragging = false" @drop.prevent="onDrop">
            <AppIcon name="down" />
            <div>
              <b>Перетащите файлы пакета сюда</b> или выберите на компьютере.
              <div class="small muted">PDF, DOCX, DOC и сканы (TIFF, PNG, JPEG), до {{ cfg.limits.max_files }} файлов по {{ cfg.limits.max_file_mb }} МБ.</div>
            </div>
            <input type="file" multiple accept=".pdf,.docx,.doc,.tif,.tiff,.png,.jpg,.jpeg" style="display: none" data-testid="check-file" @change="onPick" />
          </label>

          <table v-if="rows.length" class="tbl" style="margin-top: 12px" data-testid="check-files">
            <thead><tr><th>Файл</th><th style="width: 280px">Роль в пакете</th><th style="width: 130px">Победитель КП</th><th style="width: 40px"></th></tr></thead>
            <tbody>
              <tr v-for="r in rows" :key="r.key">
                <td>{{ r.file.name }} <span class="small muted">({{ Math.max(1, Math.round(r.file.size / 1024)) }} КБ)</span></td>
                <td>
                  <select class="select" :value="r.role" :aria-label="'Роль файла ' + r.file.name" @change="setRole(r, ($event.target as HTMLSelectElement).value)">
                    <option v-for="o in cfg.roles" :key="o.key" :value="o.key">{{ o.label }}</option>
                  </select>
                </td>
                <td>
                  <label v-if="r.role === 'kp'" class="small"><input v-model="winnerKey" type="radio" name="winner" :value="r.key" /> победитель</label>
                </td>
                <td><button class="icon-btn" type="button" aria-label="Убрать файл" @click="remove(r.key)"><AppIcon name="x" /></button></td>
              </tr>
            </tbody>
          </table>
          <p v-if="kpRows.length > 1" class="small muted" style="margin-top: 8px">
            Отметьте победителя среди коммерческих предложений: от него зависит критерий «Соответствие договора КП».
          </p>
          <div style="margin-top: 14px; display: flex; gap: 12px; align-items: center">
            <button class="btn btn-primary" type="button" data-testid="check-start" :disabled="busy || !rows.length || active" @click="start">
              <AppIcon name="check" />Проверить
            </button>
            <span v-if="cfg.customer_group" class="small muted">Группа заказчика: {{ cfg.customer_group }}</span>
          </div>
        </div>
      </div>

      <div v-if="run" class="card" data-testid="check-run">
        <div class="card-h">
          <h3 class="grow">Ход проверки</h3>
          <span class="small muted">{{ formatDateTime(run.created_at) }}</span>
        </div>
        <div class="card-b">
          <div class="progress" :class="{ ok: run.status === 'done' }" style="margin-bottom: 8px"><i :style="{ width: percent + '%' }"></i></div>
          <div class="small" style="margin-bottom: 12px" data-testid="check-stage">{{ stageText }} · {{ percent }}%</div>
          <FormError :message="run.error" />
          <table class="tbl" style="margin-bottom: 14px">
            <thead><tr><th>Файл</th><th>Роль</th><th>Что распознано</th></tr></thead>
            <tbody>
              <tr v-for="f in run.files" :key="f.id">
                <td>{{ f.name }}<span v-if="f.is_winner" class="badge b-accent" style="margin-left: 6px">победитель</span></td>
                <td>
                  {{ f.role_label }}
                  <span v-if="f.role_detected && f.role_detected !== f.role" class="badge b-warn" :title="'По содержимому похоже на: ' + f.role_detected_label">проверьте роль</span>
                </td>
                <td class="small">
                  <span v-if="f.parse_status === 'error'" class="badge b-danger">{{ f.parse_error }}</span>
                  <template v-else-if="f.parse_status === 'done'">
                    {{ f.pages ? f.pages + ' стр.' : '' }}<span v-if="f.ocr_pages"> · распознано OCR: {{ f.ocr_pages }}</span>
                    <span v-if="f.data.supplier"> · {{ f.data.supplier }}</span>
                    <span v-if="f.data.inn"> · ИНН {{ f.data.inn }}</span>
                    <span v-if="f.data.total"> · итого {{ f.data.total }}</span>
                  </template>
                  <span v-else class="muted">ожидает</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div style="display: flex; gap: 12px; flex-wrap: wrap; align-items: center">
            <button class="btn btn-primary" type="button" data-testid="check-open" :disabled="!run.results.some((r) => r.status)" @click="showResults = true">Показать результат</button>
            <span v-if="run.counts.ok + run.counts.warn + run.counts.fail + run.counts.manual" class="small">
              <span class="badge b-ok">{{ run.counts.ok }}</span> соответствует
              <span class="badge b-warn">{{ run.counts.warn }}</span> внимание
              <span class="badge b-danger">{{ run.counts.fail }}</span> не соответствует
              <span class="badge b-info">{{ run.counts.manual }}</span> вручную
            </span>
            <span class="grow"></span>
            <button class="btn btn-secondary btn-sm" type="button" :disabled="busy" @click="clear">Очистить данные</button>
          </div>
        </div>
      </div>
    </template>

    <AppDialog v-if="run" v-model:open="showResults" title="Результат проверки" description="Сводка по 20 критериям проверки пакета" wide>
      <div data-testid="check-result">
        <div class="small muted" style="margin-bottom: 8px">
          Проверено для заказчика {{ run.params.customer_group ? run.params.customer_group + ' группы' : '(группа не задана)' }}
          · редакция ЕПоЗ №{{ run.epoz.edition_id }}. Результат рекомендательный: решение принимает специалист.
        </div>
        <div style="display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 10px">
          <span class="badge b-ok">{{ run.counts.ok }} соответствует</span>
          <span class="badge b-warn">{{ run.counts.warn }} требует внимания</span>
          <span class="badge b-danger">{{ run.counts.fail }} не соответствует</span>
          <span class="badge b-info">{{ run.counts.manual }} ручная проверка</span>
          <label class="small" style="margin-left: auto"><input v-model="onlyIssues" type="checkbox" /> только замечания</label>
        </div>
        <section v-for="b in blocks" :key="b.block" style="margin-bottom: 14px">
          <h4 style="margin-bottom: 6px">Блок {{ b.block }}. {{ b.title }}</h4>
          <div v-for="r in b.items" :key="r.code" class="crit" :data-testid="'crit-' + r.code">
            <button class="crit-h" type="button" :aria-expanded="openCode === r.code" @click="toggle(r.code)">
              <span class="badge" :class="st(r.status).cls">{{ st(r.status).icon }} {{ st(r.status).text }}</span>
              <span class="grow"><b>{{ r.code }}</b> {{ r.title }}<span class="small muted" style="display: block">{{ r.summary }}</span></span>
              <AppIcon name="down" />
            </button>
            <div v-if="openCode === r.code" class="crit-b">
              <div v-for="(i, n) in r.items" :key="n" class="small" style="margin-bottom: 6px">
                <span class="badge" :class="st(i.status).cls">{{ st(i.status).icon }}</span>
                <b> {{ i.label }}</b><span v-if="i.note">: {{ i.note }}</span>
                <div v-if="i.quote" class="muted" style="margin-left: 26px">«{{ i.quote.text }}» — {{ i.quote.file }}{{ pageText(i.quote.page) }}</div>
              </div>
              <div v-for="q in r.quotes" :key="q.text" class="small muted">«{{ q.text }}» — {{ q.file }}{{ pageText(q.page) }}</div>
              <div v-if="r.epoz.length" class="epoz-box">
                <div class="small"><b>Что говорит ЕПоЗ</b></div>
                <div v-for="e in r.epoz" :key="e.part + e.label" class="small muted">
                  {{ e.part_title }}, п. {{ e.label }}: {{ e.text }}
                </div>
              </div>
            </div>
          </div>
        </section>
        <p v-if="!blocks.length" class="small muted">Замечаний нет.</p>
      </div>
      <template #footer>
        <button class="btn btn-secondary" type="button" @click="showResults = false">Закрыть</button>
        <button class="btn btn-secondary" type="button" @click="clear">Очистить данные</button>
      </template>
    </AppDialog>
  </div>
</template>

<style scoped>
.dropzone { display: flex; gap: 14px; align-items: center; padding: 18px 20px; border: 2px dashed var(--line-strong); border-radius: var(--radius); cursor: pointer; background: var(--surface-2); }
.dropzone.on, .dropzone:hover { border-color: var(--accent); background: #fff; }
.crit { border: 1px solid var(--line); margin-bottom: 6px; }
.crit-h { width: 100%; display: flex; gap: 12px; align-items: center; padding: 8px 12px; background: #fff; border: 0; text-align: left; cursor: pointer; }
.crit-h:hover { background: var(--surface-2); }
.crit-b { padding: 10px 14px 12px; border-top: 1px solid var(--line); background: var(--surface-2); }
.epoz-box { margin-top: 10px; padding: 8px 10px; background: #fff; border-left: 3px solid var(--accent); }
</style>
