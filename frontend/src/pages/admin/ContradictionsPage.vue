<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { contradictionsAdminApi, type ContradictionsAdmin } from '@/api/contradictions'
import { formatNumber } from '@/utils/format'

// Поиск противоречий (этап 6): кандидаты без модели, проверка пар моделью в фоне.
// Выключен, пока администратор не посмотрит прогноз: на тысячах документов это часы работы видеокарты.
const st = ref<ContradictionsAdmin | null>(null)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)

async function load() {
  try {
    st.value = await contradictionsAdminApi.status()
    error.value = null
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить состояние'
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
    const r = await contradictionsAdminApi.setEnabled(!st.value?.enabled)
    return r.enabled
      ? `Поиск включён. В очередь поставлено документов: ${r.queued_documents}. Пользователи остаются в приоритете.`
      : 'Поиск выключен. Найденные противоречия остаются.'
  })
const rescan = () =>
  act(async () => `На повторную проверку: ${(await contradictionsAdminApi.rescan()).queued_documents} документов. Проверенные пары берутся из кэша.`)
const retry = () => act(async () => `Повторно проверим пар: ${(await contradictionsAdminApi.retryErrors()).pairs}.`)

const pct = computed(() => {
  const d = st.value?.documents
  return d && d.indexed ? Math.round((d.done / d.indexed) * 100) : 0
})
const dec = (n: number) => String(n).replace('.', ',')
function duration(seconds: number): string {
  if (seconds < 90) return `${Math.max(1, Math.round(seconds))} с`
  const h = seconds / 3600
  if (h < 1) return `${Math.round(seconds / 60)} мин`
  if (h < 48) return `${h.toFixed(1).replace('.', ',')} ч`
  return `${(h / 24).toFixed(1).replace('.', ',')} суток`
}
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Поиск противоречий</h1>
        <p>Модель сравнивает похожие требования из разных документов. Пары с отменёнными документами не проверяются.</p>
      </div>
      <button class="btn btn-secondary" type="button" :disabled="busy" @click="rescan"><AppIcon name="refresh" />Проверить заново</button>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />
    <div v-if="notice" class="alert a-ok" style="margin-bottom: 12px" role="status"><AppIcon name="check" /><div>{{ notice }}</div></div>

    <template v-if="st">
      <div class="tiles" style="margin-bottom: 16px">
        <div class="tile t-ok">
          <div class="l">Проверено документов</div>
          <div class="n">{{ formatNumber(st.documents.done) }}</div>
          <div class="d">из {{ formatNumber(st.documents.indexed) }} · {{ pct }} %</div>
        </div>
        <div class="tile">
          <div class="l">Найдено противоречий</div>
          <div class="n">{{ formatNumber(st.checks.contradiction + st.checks.possible) }}</div>
          <div class="d">прямых {{ formatNumber(st.checks.contradiction) }}, возможных {{ formatNumber(st.checks.possible) }}</div>
        </div>
        <div class="tile">
          <div class="l">Решения специалистов</div>
          <div class="n">{{ formatNumber(st.reviews.confirmed) }}</div>
          <div class="d">подтверждено; отклонено {{ formatNumber(st.reviews.rejected) }}, новых {{ formatNumber(st.reviews.new) }}</div>
        </div>
        <div class="tile t-accent">
          <div class="l">Осталось</div>
          <div class="n" data-testid="contra-forecast">{{ st.documents.pending ? duration(st.forecast_seconds) : 'Готово' }}</div>
          <div class="d">{{ st.documents.pending ? 'работы видеокарты, прогноз' : 'все документы проверены' }}</div>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h">
          <h3 class="grow">Проверка моделью</h3>
          <span v-if="st.enabled" class="badge b-run">Включено</span>
          <span v-else class="badge b-neutral">Выключено</span>
          <button class="btn btn-sm" :class="st.enabled ? 'btn-secondary' : 'btn-primary'" type="button" :disabled="busy" data-testid="contra-toggle" @click="toggle">
            {{ st.enabled ? 'Выключить' : 'Включить поиск противоречий' }}
          </button>
        </div>
        <div class="card-b" style="display: grid; gap: 12px">
          <p class="small muted" style="margin: 0">
            Модель {{ st.model }} проверяет пары по одной в фоне, когда никто из пользователей не ждёт ответа.
            Пары: нормативные фрагменты (сроки, периодичность, «обязан», «запрещается») и до {{ st.settings.neighbors }} самых похожих фрагментов других документов
            (близость не ниже {{ dec(st.settings.min_similarity) }}), плюс до {{ st.settings.graph_neighbors }} связанных графом.
            Проверенная пара запоминается: после переиндексации модель второй раз не спрашивается.
          </p>
          <div class="progress" role="progressbar" :aria-valuenow="pct" aria-valuemin="0" aria-valuemax="100"><i :style="{ width: `${pct}%` }"></i></div>
          <dl class="kv" style="grid-template-columns: 260px 1fr">
            <dt>Документов в очереди</dt><dd class="num">{{ formatNumber(st.documents.pending) }} (задач: {{ formatNumber(st.jobs) }})</dd>
            <dt>Отменённых, пропущено</dt><dd class="num">{{ formatNumber(st.documents.cancelled) }}</dd>
            <dt>Пар проверено моделью</dt><dd class="num">{{ formatNumber(st.checks.contradiction + st.checks.possible + st.checks.none) }}</dd>
            <dt>Ошибка ответа модели</dt>
            <dd class="num">
              {{ formatNumber(st.checks.error) }}
              <button v-if="st.checks.error" class="btn btn-ghost btn-sm" type="button" :disabled="busy" @click="retry">Повторить</button>
            </dd>
            <dt>Время на пару</dt><dd class="num">{{ dec(st.seconds_per_pair) }} с — {{ st.seconds_basis === 'measured' ? 'по замеру' : 'оценка до замера' }}</dd>
            <dt>Пар на документ</dt><dd class="num">{{ dec(st.pairs_per_document) }} — {{ st.pairs_basis === 'measured' ? 'по проверенным документам' : 'оценка до замера' }}</dd>
            <dt>Прогноз до конца</dt>
            <dd class="num">≈ {{ duration(st.forecast_seconds) }} <span class="small muted">(сверху: часть пар уже в кэше; днём пользователи в приоритете)</span></dd>
          </dl>
        </div>
      </div>
    </template>
    <div v-else-if="!error" class="card card-b"><div class="skel" style="width: 50%"></div></div>
  </div>
</template>
