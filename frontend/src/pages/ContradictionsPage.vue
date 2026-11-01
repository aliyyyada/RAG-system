<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import LegalStatus from '@/components/LegalStatus.vue'
import StateBlock from '@/components/StateBlock.vue'
import AppPager from '@/components/ui/AppPager.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import {
  contradictionsApi,
  type ActiveFilter,
  type Contradiction,
  type ContradictionList,
  type ReviewStatus,
} from '@/api/contradictions'
import { useAuthStore } from '@/stores/auth'
import { useContradictionCount } from '@/stores/contradictions'
import { formatDate } from '@/utils/format'

// Противоречия (этап 6). Пары найдены моделью в фоне; backend показывает только те, где
// человеку доступны оба документа. Подтверждать и отклонять — по праву contradictions.manage
// (кнопки скрыты без права, но проверка всё равно на backend).
const auth = useAuthStore()
const counter = useContradictionCount()
const canManage = computed(() => auth.can('contradictions.manage'))

const tab = ref<ReviewStatus>('new')
const activeFilter = ref<ActiveFilter>('active')
const topic = ref<string>('')
const page = ref(1)
const data = ref<ContradictionList | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const busy = ref<number | null>(null)

const TABS: { key: ReviewStatus; label: string }[] = [
  { key: 'new', label: 'Новые' },
  { key: 'confirmed', label: 'Подтверждённые' },
  { key: 'rejected', label: 'Отклонённые' },
]

async function load() {
  loading.value = true
  error.value = null
  try {
    data.value = await contradictionsApi.list({
      status: tab.value,
      active: activeFilter.value,
      topic: topic.value || null,
      page: page.value,
      page_size: 20,
    })
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить противоречия'
  } finally {
    loading.value = false
  }
}
onMounted(load)
watch([tab, activeFilter, topic], () => {
  page.value = 1
  void load()
})

function go(p: number) {
  page.value = p
  void load()
}

async function review(item: Contradiction, status: ReviewStatus) {
  busy.value = item.id
  error.value = null
  try {
    await contradictionsApi.review(item.id, status)
    await load()
    void counter.refresh()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить решение'
  } finally {
    busy.value = null
  }
}

/** Отрывок с подсвеченной цитатой: [до, цитата, после]. Без v-html — текст документа не размечаем. */
function parts(excerpt: string, quote: string): [string, string, string] {
  if (!quote) return [excerpt, '', '']
  const i = excerpt.toLowerCase().indexOf(quote.toLowerCase())
  if (i < 0) return [excerpt, '', '']
  return [excerpt.slice(0, i), excerpt.slice(i, i + quote.length), excerpt.slice(i + quote.length)]
}

const emptyText = computed(() => {
  if (activeFilter.value === 'active' && tab.value === 'new')
    return 'Новых противоречий в доступных вам документах нет. Проверка идёт в фоне, новые пары появятся здесь.'
  return 'В этом разделе пока пусто.'
})
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Противоречия</h1>
        <p>Найдены автоматически. Показаны только пары, где оба документа доступны вам.</p>
      </div>
      <select v-model="activeFilter" class="select" style="width: 220px" aria-label="Активность" data-testid="contra-active">
        <option value="active">Только активные</option>
        <option value="inactive">Неактивные (отменён, изменён)</option>
        <option value="all">Все</option>
      </select>
      <select v-model="topic" class="select" style="width: 220px" aria-label="Тема">
        <option value="">Все темы</option>
        <option v-for="t in data?.topics ?? []" :key="t" :value="t">{{ t }}</option>
      </select>
    </div>

    <div v-for="u in data?.unavailable_sources ?? []" :key="u.id" class="alert a-warn" style="margin-bottom: 16px" role="status">
      <AppIcon name="warn" />
      <div><b>{{ u.message }}</b>Пары с документами этой системы сейчас не показываются.</div>
    </div>

    <div class="tabs" role="tablist" style="margin-bottom: 20px">
      <a
        v-for="t in TABS"
        :key="t.key"
        href="#"
        role="tab"
        :aria-selected="tab === t.key"
        :class="{ on: tab === t.key }"
        @click.prevent="tab = t.key"
        >{{ t.label }}<span class="cnt">{{ data?.counts[t.key] ?? '' }}</span></a
      >
    </div>

    <FormError :message="error" style="margin-bottom: 12px" />

    <div v-if="loading && !data" class="card card-b"><div class="skel" style="width: 60%"></div></div>
    <div v-else-if="data && !data.items.length" class="card" style="min-height: 280px; display: grid">
      <StateBlock icon="check" tone="plain" title="Ничего не найдено" :text="emptyText" />
    </div>

    <div v-else-if="data" class="stack" style="gap: 16px" data-testid="contra-list">
      <article
        v-for="item in data.items"
        :key="item.id"
        class="cpair"
        :class="{ mid: item.verdict === 'possible' || !item.active }"
        :data-testid="`contra-${item.id}`"
      >
        <div class="cpair-h">
          <span class="badge" :class="item.verdict === 'contradiction' ? 'b-danger' : 'b-warn'">{{ item.verdict === 'contradiction' ? 'Прямое противоречие' : 'Возможное' }}</span>
          <b>{{ item.topic }}</b>
          <span class="grow"></span>
          <span v-if="item.review_status !== 'new'" class="small muted">{{ item.review_label }}<template v-if="item.reviewed_by">: {{ item.reviewed_by }}</template>, {{ formatDate(item.reviewed_at) }}</span>
          <span v-else class="small muted">Найдено {{ formatDate(item.found_at) }}</span>
        </div>
        <div class="cpair-b">
          <div v-for="s in item.sides" :key="s.chunk_id">
            <RouterLink
              :to="{ name: 'document', params: { id: s.document.id }, query: { chunk: s.chunk_id } }"
              style="font-weight: 600; color: var(--ink-900)"
              >{{ s.document.caption }}<template v-if="s.place">, {{ s.place }}</template></RouterLink
            >
            <span class="row"><LegalStatus :status="s.document.legal_status" /><span class="small muted">{{ formatDate(s.document.doc_date) }}</span></span>
            <blockquote>{{ parts(s.excerpt, s.quote)[0] }}<mark v-if="parts(s.excerpt, s.quote)[1]">{{ parts(s.excerpt, s.quote)[1] }}</mark>{{ parts(s.excerpt, s.quote)[2] }}</blockquote>
          </div>
        </div>
        <div v-if="item.inactive_reason" class="cpair-why" data-testid="contra-inactive">
          <AppIcon name="info" /><span><b>Не активно.</b> {{ item.inactive_reason }}.</span>
        </div>
        <div v-if="item.explanation" class="cpair-why"><AppIcon name="info" /><span>{{ item.explanation }}</span></div>
        <div v-if="item.review_note" class="cpair-why"><AppIcon name="check" /><span>Комментарий: {{ item.review_note }}</span></div>
        <div v-if="canManage" class="cpair-f">
          <template v-if="item.review_status === 'new'">
            <button class="btn btn-dark btn-sm" type="button" :disabled="busy === item.id" @click="review(item, 'confirmed')">Подтвердить</button>
            <button class="btn btn-secondary btn-sm" type="button" :disabled="busy === item.id" @click="review(item, 'rejected')">Отклонить</button>
          </template>
          <button v-else class="btn btn-secondary btn-sm" type="button" :disabled="busy === item.id" @click="review(item, 'new')">Вернуть в новые</button>
        </div>
      </article>
      <AppPager v-if="data.total > data.page_size" :page="data.page" :page-size="data.page_size" :total="data.total" :shown="data.items.length" @go="go" />
    </div>
  </div>
</template>
