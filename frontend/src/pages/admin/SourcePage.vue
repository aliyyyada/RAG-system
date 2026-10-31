<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import AppPager from '@/components/ui/AppPager.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { sourcesApi } from '@/api/sources'
import type { DocumentPage, Source } from '@/api/types'
import { formatDate, formatDateTime, formatEta, formatNumber, shortId } from '@/utils/format'
import SourceDialog from './dialogs/SourceDialog.vue'

// «Источники → Документооборот»: отчёт о полноте загрузки по макету a-sources.
const route = useRoute()
const id = computed(() => Number(route.params.id))

const source = ref<Source | null>(null)
const errors = ref<DocumentPage | null>(null)
const errorsPage = ref(1)
const loadError = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const settingsOpen = ref(false)
const tokenOpen = ref(false)
const token = ref<string | null>(null)

async function load() {
  try {
    const [s, e] = await Promise.all([
      sourcesApi.get(id.value),
      sourcesApi.documents({ source_id: id.value, status: 'error', page: errorsPage.value, page_size: 20 }),
    ])
    source.value = s
    errors.value = e
    loadError.value = null
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Не удалось загрузить источник'
  }
}

// Пока идёт индексация или синхронизация, обновляем отчёт каждые 5 секунд.
let timer: number | undefined
function schedule() {
  window.clearTimeout(timer)
  timer = window.setTimeout(async () => {
    await load()
    schedule()
  }, active.value ? 5000 : 30000)
}
onMounted(async () => {
  await load()
  schedule()
})
onBeforeUnmount(() => window.clearTimeout(timer))
watch(id, load)

const rep = computed(() => source.value?.report)
const pending = computed(() => (rep.value ? rep.value.queued + rep.value.processing : 0))
const running = computed(() => rep.value?.runs.some((r) => r.status === 'running') ?? false)
const active = computed(() => pending.value > 0 || running.value)
const total = computed(() => Math.max(rep.value?.at_source ?? 0, rep.value?.known ?? 0))
const percent = computed(() => Math.round((rep.value?.progress ?? 0) * 100))

const subtitle = computed(() => {
  const s = source.value
  if (!s) return ''
  const parts = [`Подключён ${formatDate(s.created_at)}`]
  parts.push(s.enabled ? `синхронизация каждые ${s.sync_interval_minutes} мин` : 'синхронизация выключена')
  parts.push(`ночная сверка в ${String(s.reconcile_hour).padStart(2, '0')}:00`)
  return parts.join(' · ')
})

async function run(action: () => Promise<string>) {
  busy.value = true
  notice.value = null
  loadError.value = null
  try {
    notice.value = await action()
    await load()
    schedule()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Не удалось выполнить действие'
  } finally {
    busy.value = false
  }
}

const syncNow = (reconcile: boolean) =>
  run(async () => {
    const r = await sourcesApi.sync(id.value, reconcile)
    const what = reconcile ? 'Полная сверка' : 'Синхронизация'
    return r.queued ? `${what} запущена` : `${what} уже стоит в очереди`
  })
const retryAll = () =>
  run(async () => {
    const r = await sourcesApi.retryFailed(id.value)
    return `Повторная обработка: ${r.count}`
  })
const retryOne = (docId: number) =>
  run(async () => {
    await sourcesApi.retry(docId)
    return 'Документ поставлен в очередь'
  })

async function issueToken() {
  busy.value = true
  try {
    token.value = (await sourcesApi.callbackToken(id.value)).token
    tokenOpen.value = true
    await load()
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : 'Не удалось выпустить токен'
  } finally {
    busy.value = false
  }
}

function goErrors(p: number) {
  errorsPage.value = p
  void load()
}

const RUN_KIND = { incremental: 'Синхронизация', reconcile: 'Полная сверка' }
function runSummary(stats: Record<string, number>): string {
  const parts = []
  if (stats.new) parts.push(`новых ${stats.new}`)
  if (stats.changed) parts.push(`изменённых ${stats.changed}`)
  if (stats.deleted) parts.push(`удалённых ${stats.deleted}`)
  if (stats.missing) parts.push(`пропавших ${stats.missing}`)
  if (stats.restored) parts.push(`восстановленных ${stats.restored}`)
  return parts.join(', ') || (stats.seen !== undefined ? 'без изменений' : '—')
}
</script>

<template>
  <div class="page">
    <div class="crumbs"><RouterLink :to="{ name: 'admin-sources' }">Источники</RouterLink> / {{ source?.name ?? '…' }}</div>
    <FormError :message="loadError" style="margin-bottom: 12px" />

    <template v-if="source && rep">
      <div class="page-h">
        <div class="grow">
          <h1>{{ source.name }}</h1>
          <p>{{ subtitle }}</p>
        </div>
        <span v-if="!source.enabled" class="badge b-neutral">Выключен</span>
        <span v-else-if="source.available === false" class="badge b-danger">Источник не отвечает</span>
        <span v-else-if="source.available" class="badge b-ok">Источник отвечает</span>
        <button class="btn btn-secondary" type="button" @click="settingsOpen = true">Настройки</button>
        <button class="btn btn-secondary" type="button" :disabled="busy || !source.enabled" @click="syncNow(true)">
          Полная сверка
        </button>
        <button class="btn btn-primary" type="button" :disabled="busy || !source.enabled" @click="syncNow(false)">
          <AppIcon name="refresh" />Синхронизировать сейчас
        </button>
      </div>

      <div v-if="source.available === false" class="alert a-danger" style="margin-bottom: 16px" role="alert">
        <AppIcon name="plug" />
        <div>
          <b>Источник сейчас недоступен</b>
          {{ source.last_error }}. Уже загруженные документы остаются в поиске. Синхронизация повторится по расписанию.
        </div>
      </div>
      <div v-if="notice" class="alert a-ok" style="margin-bottom: 16px" role="status">
        <AppIcon name="check" /><div>{{ notice }}</div>
      </div>

      <div class="tiles" style="margin-bottom: 16px">
        <div class="tile">
          <div class="l">У источника</div>
          <div class="n">{{ formatNumber(rep.at_source) }}</div>
          <div class="d">без удалённых</div>
        </div>
        <div class="tile t-ok">
          <div class="l">Загружено</div>
          <div class="n">{{ formatNumber(rep.ready) }}</div>
          <div class="d">{{ percent }} % · последняя {{ formatDateTime(source.last_sync_at) }}</div>
        </div>
        <div class="tile t-accent">
          <div class="l">В очереди</div>
          <div class="n">{{ formatNumber(rep.queued) }}</div>
          <div class="d">обрабатывается {{ rep.processing }}</div>
        </div>
        <div class="tile t-danger">
          <div class="l">С ошибкой</div>
          <div class="n">{{ formatNumber(rep.error) }}</div>
          <div class="d">повторы исчерпаны</div>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-b" style="display: grid; gap: 10px">
          <div class="row">
            <b>{{ source.last_reconcile_at ? 'Индексация' : 'Первичная индексация' }}</b>
            <span class="grow"></span>
            <span class="num small muted">
              {{ formatNumber(rep.ready) }} из {{ formatNumber(total) }}
              <template v-if="pending">
                · ≈ {{ rep.rate_per_minute.toLocaleString('ru-RU') }} документа в минуту · осталось
                {{ formatEta(rep.eta_minutes) }}
              </template>
              <template v-else> · очередь пуста</template>
            </span>
          </div>
          <div class="progress" :class="{ ok: !pending && !rep.error }" style="height: 12px">
            <i :style="{ width: percent + '%' }"></i>
          </div>
          <p class="small muted">
            Продолжится с того же места после перезапуска сервера. Пока пользователи задают вопросы,
            фоновая обработка уступает им видеокарту. Удалено в источнике: {{ formatNumber(rep.deleted) }},
            исключено из поиска: {{ formatNumber(rep.excluded) }}.
          </p>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h">
          <h3>Документы с ошибкой</h3>
          <span class="badge b-danger plain">{{ rep.error }}</span>
          <span class="grow"></span>
          <button class="btn btn-secondary btn-sm" type="button" :disabled="busy || !rep.error" @click="retryAll">
            <AppIcon name="refresh" />Повторить все
          </button>
        </div>
        <table v-if="errors?.items.length" class="tbl">
          <thead>
            <tr>
              <th>Документ</th>
              <th>Версия источника</th>
              <th>Этап</th>
              <th>Причина</th>
              <th class="r">Попыток</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="d in errors.items" :key="d.id">
              <td class="doc">
                <RouterLink :to="{ name: 'admin-document', params: { id: d.id } }">{{ d.caption }}</RouterLink>
                <div class="small muted mono" :title="d.external_id ?? ''">{{ shortId(d.external_id) }}</div>
              </td>
              <td class="num" style="white-space: nowrap">{{ formatDateTime(d.version) }}</td>
              <td>{{ d.error_stage_title ?? '—' }}</td>
              <td>{{ d.error_message }}</td>
              <td class="r num">{{ d.attempts }}</td>
              <td>
                <button class="btn btn-secondary btn-sm" type="button" :disabled="busy" @click="retryOne(d.id)">Повторить</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else style="display: grid; min-height: 160px">
          <StateBlock icon="check" tone="plain" title="Ошибок нет" text="Все полученные документы прошли проверку полноты и разбор." />
        </div>
        <AppPager
          v-if="errors && errors.total > errors.page_size"
          :page="errors.page"
          :page-size="errors.page_size"
          :total="errors.total"
          :shown="errors.items.length"
          @go="goErrors"
        />
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h"><h3>Журнал синхронизаций</h3></div>
        <table class="tbl">
          <thead>
            <tr>
              <th>Начало</th>
              <th>Вид</th>
              <th>Результат</th>
              <th class="r">Просмотрено</th>
              <th>Изменения</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rep.runs" :key="r.id">
              <td class="num" style="white-space: nowrap">{{ formatDateTime(r.started_at) }}</td>
              <td>{{ RUN_KIND[r.kind] }}</td>
              <td>
                <span v-if="r.status === 'running'" class="badge b-info b-run">Идёт</span>
                <span v-else-if="r.status === 'ok'" class="badge b-ok">Успешно</span>
                <template v-else>
                  <span class="badge b-danger">Ошибка</span>
                  <div class="small muted">{{ r.error }}</div>
                </template>
              </td>
              <td class="r num">{{ formatNumber(r.stats.seen ?? null) }}</td>
              <td>{{ runSummary(r.stats) }}</td>
            </tr>
            <tr v-if="!rep.runs.length">
              <td colspan="5" class="muted">Синхронизаций ещё не было — первая начнётся в течение минуты.</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card">
        <div class="card-h">
          <h3>API для источника</h3>
          <span class="grow"></span>
          <button class="btn btn-secondary btn-sm" type="button" :disabled="busy" @click="issueToken">
            <AppIcon name="key" />{{ source.has_callback_token ? 'Выпустить новый токен' : 'Выпустить токен' }}
          </button>
        </div>
        <div class="card-b small">
          Источник может сам спросить, какие его документы загружены и в какой версии:
          <span class="mono">GET /api/integration/v1/sources/{{ source.code }}/documents</span> с заголовком
          <span class="mono">Authorization: Bearer &lt;токен&gt;</span>. Описание — в
          <a href="/api/docs" target="_blank" rel="noopener">документации API</a>.
          <span v-if="source.has_callback_token" class="muted"> Токен выпущен.</span>
        </div>
      </div>
    </template>

    <div v-else-if="!loadError" class="card card-b"><div class="skel" style="width: 40%"></div></div>

    <SourceDialog v-model:open="settingsOpen" :source="source" @saved="load" />
    <AppDialog v-model:open="tokenOpen" title="Токен для источника">
      <div class="stack">
        <p class="small">Скопируйте токен и передайте администраторам источника. Больше он показан не будет; предыдущий токен перестал действовать.</p>
        <input class="input mono" readonly :value="token ?? ''" aria-label="Токен" @focus="($event.target as HTMLInputElement).select()" />
      </div>
      <template #footer>
        <button class="btn btn-primary" type="button" @click="tokenOpen = false">Готово</button>
      </template>
    </AppDialog>
  </div>
</template>
