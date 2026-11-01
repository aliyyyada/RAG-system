<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { graphAdminApi } from '@/api/graph'
import type { GraphAdmin } from '@/api/types'
import { formatDateTime, formatNumber } from '@/utils/format'

// Граф знаний (этап 5): связи документов по правилам и извлечение сущностей моделью.
// Извлечение моделью выключено, пока администратор не посмотрит прогноз времени и не включит его:
// на тысячах документов видеокарта может быть занята несколько суток.
const st = ref<GraphAdmin | null>(null)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const sample = ref(20)

async function load() {
  try {
    st.value = await graphAdminApi.status()
    error.value = null
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить состояние графа'
  }
}
let timer: number | undefined
onMounted(() => {
  void load()
  timer = window.setInterval(load, 5000)
})
onUnmounted(() => window.clearInterval(timer))

async function act(fn: () => Promise<string>) {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    notice.value = await fn()
    await load()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось выполнить действие'
  } finally {
    busy.value = false
  }
}
const toggle = () =>
  act(async () => {
    const r = await graphAdminApi.setLlm(!st.value?.llm_enabled)
    return r.enabled
      ? `Извлечение включено. В очередь поставлено документов: ${r.queued_documents}. Пользователи остаются в приоритете.`
      : 'Извлечение выключено. Уже извлечённое остаётся в графе.'
  })
const estimate = () =>
  act(async () => {
    await graphAdminApi.estimate(sample.value)
    return `Замер поставлен в очередь: ${sample.value} фрагментов. Результат появится ниже через несколько минут.`
  })
const rebuild = () =>
  act(async () => `Связи пересобираются для ${(await graphAdminApi.rebuildLinks()).queued_documents} документов.`)
const retry = () => act(async () => `Повторно в очередь: ${(await graphAdminApi.retryErrors()).chunks} фрагментов.`)

const llm = computed(() => st.value?.llm)
const totalChunks = computed(() => {
  const c = llm.value?.chunks
  return c ? c.pending + c.done + c.skipped + c.error : 0
})
const processedPct = computed(() => {
  const c = llm.value?.chunks
  if (!c || !totalChunks.value) return 0
  return Math.round(((c.done + c.skipped + c.error) / totalChunks.value) * 100)
})
const BASIS = {
  measured: 'по замеру на обработанных фрагментах',
  estimate: 'по пробному замеру',
  assumption: 'оценка до замера: 12 с на фрагмент',
}
const dec = (n: number | null | undefined) => (n === null || n === undefined ? '—' : String(n).replace('.', ','))
function duration(seconds: number): string {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} с`
  const h = seconds / 3600
  if (h < 1) return `${Math.round(seconds / 60)} мин`
  if (h < 48) return `${h.toFixed(1).replace('.', ',')} ч`
  return `${(h / 24).toFixed(1).replace('.', ',')} суток`
}
const kinds = computed(() =>
  st.value ? (['cancels', 'amends', 'executes', 'refers'] as const).map((k) => ({ k, label: st.value!.labels.kinds[k], n: st.value!.links.by_kind[k] ?? 0 })) : [],
)
const entityTypes = computed(() =>
  st.value
    ? Object.entries(st.value.labels.entities).map(([k, label]) => ({ k, label, n: st.value!.entities.by_type[k] ?? 0 }))
    : [],
)
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Граф знаний</h1>
        <p>Связи между документами («отменяет», «изменяет») и сущности: люди, должности, подразделения, оборудование, участки.</p>
      </div>
      <button class="btn btn-secondary" type="button" :disabled="busy" @click="rebuild"><AppIcon name="refresh" />Пересобрать связи</button>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />
    <div v-if="notice" class="alert a-ok" style="margin-bottom: 12px" role="status"><AppIcon name="check" /><div>{{ notice }}</div></div>

    <template v-if="st && llm">
      <div class="tiles" style="margin-bottom: 16px">
        <div class="tile">
          <div class="l">Связей документов</div>
          <div class="n">{{ formatNumber(Object.values(st.links.by_kind).reduce((a, b) => a + b, 0)) }}</div>
          <div class="d">без цели в системе: {{ formatNumber(st.links.unresolved) }}</div>
        </div>
        <div class="tile t-ok">
          <div class="l">Документы со связями</div>
          <div class="n">{{ formatNumber(st.links.documents_done) }}</div>
          <div class="d">из {{ formatNumber(st.links.documents_indexed) }} загруженных</div>
        </div>
        <div class="tile">
          <div class="l">Сущностей</div>
          <div class="n">{{ formatNumber(st.entities.total) }}</div>
          <div class="d">связей между ними: {{ formatNumber(st.relations) }}</div>
        </div>
        <div class="tile t-accent">
          <div class="l">Осталось моделью</div>
          <div class="n">{{ llm.chunks.pending ? duration(llm.forecast_seconds) : 'Готово' }}</div>
          <div class="d">{{ llm.chunks.pending ? BASIS[llm.seconds_basis] : 'все фрагменты обработаны' }}</div>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h">
          <h3 class="grow">Шаг 1. Связи по правилам</h3>
          <span class="small muted">без модели, сразу после загрузки документа</span>
        </div>
        <table class="tbl">
          <thead><tr><th>Вид связи</th><th class="r">Связей</th></tr></thead>
          <tbody>
            <tr v-for="r in kinds" :key="r.k"><td>{{ r.label }}</td><td class="r num">{{ formatNumber(r.n) }}</td></tr>
          </tbody>
        </table>
        <div class="card-b small muted">
          Статус по связям:
          <template v-for="(n, s) in st.links.statuses" :key="s">«{{ s }}» — {{ formatNumber(n) }}; </template>
          <template v-if="!Object.keys(st.links.statuses).length">ещё не вычислен.</template>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h">
          <h3 class="grow">Шаг 2. Сущности и связи моделью</h3>
          <span v-if="st.llm_enabled" class="badge b-run">Включено</span>
          <span v-else class="badge b-neutral">Выключено</span>
          <button
            class="btn btn-sm"
            :class="st.llm_enabled ? 'btn-secondary' : 'btn-primary'"
            type="button"
            :disabled="busy"
            data-testid="graph-llm-toggle"
            @click="toggle"
          >
            {{ st.llm_enabled ? 'Выключить' : 'Включить извлечение' }}
          </button>
        </div>
        <div class="card-b" style="display: grid; gap: 12px">
          <p class="small muted" style="margin: 0">
            Модель {{ st.model }} разбирает фрагменты по одному в фоне, когда никто из пользователей не ждёт ответа.
            Списки ознакомления, короткие фрагменты и таблицы из одних чисел в модель не отправляются.
          </p>
          <div class="row">
            <b>Обработано</b><span class="grow"></span>
            <span class="num small muted">{{ formatNumber(totalChunks - llm.chunks.pending) }} из {{ formatNumber(totalChunks) }} фрагментов · {{ processedPct }} %</span>
          </div>
          <div class="progress" role="progressbar" :aria-valuenow="processedPct" aria-valuemin="0" aria-valuemax="100"><i :style="{ width: `${processedPct}%` }"></i></div>
          <dl class="kv" style="grid-template-columns: 240px 1fr">
            <dt>Разобрано моделью</dt><dd class="num">{{ formatNumber(llm.chunks.done) }}</dd>
            <dt>Пропущено фильтром</dt><dd class="num">{{ formatNumber(llm.chunks.skipped) }}</dd>
            <dt>Ошибка ответа модели</dt>
            <dd class="num">
              {{ formatNumber(llm.chunks.error) }}
              <button v-if="llm.chunks.error" class="btn btn-ghost btn-sm" type="button" :disabled="busy" @click="retry">Повторить</button>
            </dd>
            <dt>В очереди</dt><dd class="num">{{ formatNumber(llm.chunks.pending) }}</dd>
            <dt>Время на фрагмент</dt><dd class="num">{{ dec(llm.seconds_per_chunk) }} с — {{ BASIS[llm.seconds_basis] }}</dd>
            <dt>Доля фрагментов для модели</dt><dd class="num">{{ Math.round(llm.eligible_share * 100) }} %</dd>
            <dt>Прогноз до конца</dt>
            <dd class="num">
              ≈ {{ duration(llm.forecast_seconds) }} работы видеокарты
              <span class="small muted">(днём пользователи в приоритете, поэтому по календарю дольше)</span>
            </dd>
          </dl>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h">
          <h3 class="grow">Пробный замер</h3>
          <label class="row small" style="gap: 6px">
            Фрагментов
            <input v-model.number="sample" class="input" type="number" min="3" max="200" style="width: 80px; height: 32px" />
          </label>
          <button class="btn btn-secondary btn-sm" type="button" :disabled="busy || st.estimate_running" data-testid="graph-estimate" @click="estimate">
            {{ st.estimate_running ? 'Замер идёт…' : 'Оценить время' }}
          </button>
        </div>
        <div class="card-b">
          <p v-if="!st.last_estimate" class="small muted" style="margin: 0">
            Замера ещё не было. Он отправит выборку фрагментов в модель (в граф ничего не пишет) и покажет время на фрагмент и прогноз на весь архив.
          </p>
          <template v-else>
            <dl class="kv" style="grid-template-columns: 240px 1fr">
              <dt>Когда</dt><dd class="num">{{ formatDateTime(st.last_estimate.at) }}</dd>
              <dt>Выборка</dt><dd class="num">{{ st.last_estimate.sample }} фрагментов<template v-if="st.last_estimate.errors">, ошибок {{ st.last_estimate.errors }}</template></dd>
              <dt>Время на фрагмент</dt>
              <dd class="num">
                <template v-if="st.last_estimate.avg_seconds !== null">
                  {{ dec(st.last_estimate.avg_seconds) }} с (от {{ dec(st.last_estimate.min_seconds) }} до {{ dec(st.last_estimate.max_seconds) }})<template v-if="st.last_estimate.tokens_per_second">, {{ dec(st.last_estimate.tokens_per_second) }} токенов/с</template>
                </template>
                <template v-else>модель не ответила</template>
              </dd>
              <dt>К обработке</dt><dd class="num">≈ {{ formatNumber(st.last_estimate.chunks_to_process) }} фрагментов ({{ Math.round(st.last_estimate.eligible_share * 100) }} % пройдут фильтр)</dd>
              <dt>Прогноз</dt><dd class="num">≈ {{ dec(st.last_estimate.forecast_hours) }} ч работы видеокарты</dd>
            </dl>
            <div v-for="ex in st.last_estimate.examples" :key="ex.chunk_id" class="small" style="margin-top: 12px">
              <b>{{ ex.document }}</b>
              <div class="row wrap" style="margin-top: 6px">
                <span v-for="e in ex.entities" :key="e" class="badge b-outline plain">{{ e }}</span>
                <span v-if="!ex.entities.length" class="muted">сущностей не найдено</span>
              </div>
            </div>
          </template>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><h3 class="grow">Сущности по типам</h3></div>
        <table class="tbl">
          <thead><tr><th>Тип</th><th class="r">Сущностей</th></tr></thead>
          <tbody>
            <tr v-for="t in entityTypes" :key="t.k"><td>{{ t.label }}</td><td class="r num">{{ formatNumber(t.n) }}</td></tr>
          </tbody>
        </table>
      </div>
    </template>
    <div v-else-if="!error" class="card card-b"><div class="skel" style="width: 50%"></div></div>
  </div>
</template>
