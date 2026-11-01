<script setup lang="ts">
import LegalStatus from '@/components/LegalStatus.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import AppPager from '@/components/ui/AppPager.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { documentsApi } from '@/api/documents'
import type { DocumentFilters, UserDocumentList } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { formatDate, formatNumber } from '@/utils/format'

// «Документы» по макету docs: только документы, которые разрешили источники,
// плюс общие. Фильтры живут в адресе страницы, чтобы ссылкой можно было поделиться.
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const PERIODS = [
  { value: '', label: 'За всё время' },
  { value: '1', label: 'За последний год' },
  { value: '3', label: 'За 3 года' },
  { value: '5', label: 'За 5 лет' },
]

const str = (v: unknown) => (typeof v === 'string' ? v : '')
const q = ref(str(route.query.q))
const source = ref(str(route.query.source))
const docType = ref(str(route.query.type))
const period = ref(str(route.query.period))
const page = ref(Number(route.query.page) || 1)

const data = ref<UserDocumentList | null>(null)
const filters = ref<DocumentFilters | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)

function dateFrom(years: string): string | null {
  if (!years) return null
  const d = new Date()
  d.setFullYear(d.getFullYear() - Number(years))
  return d.toISOString().slice(0, 10)
}

async function load() {
  loading.value = true
  error.value = null
  try {
    const [list, f] = await Promise.all([
      documentsApi.list({
        q: q.value.trim() || undefined,
        source: source.value || null,
        doc_type: docType.value || null,
        date_from: dateFrom(period.value),
        page: page.value,
        page_size: 30,
      }),
      filters.value ? Promise.resolve(filters.value) : documentsApi.filters(),
    ])
    data.value = list
    filters.value = f
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить документы'
  } finally {
    loading.value = false
  }
}
onMounted(load)

function syncUrl() {
  const query: Record<string, string> = {}
  if (q.value.trim()) query.q = q.value.trim()
  if (source.value) query.source = source.value
  if (docType.value) query.type = docType.value
  if (period.value) query.period = period.value
  if (page.value > 1) query.page = String(page.value)
  void router.replace({ query })
}

watch([source, docType, period], () => {
  page.value = 1
  syncUrl()
  void load()
})
let timer: number | undefined
watch(q, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => {
    page.value = 1
    syncUrl()
    void load()
  }, 300)
})

function go(p: number) {
  page.value = p
  syncUrl()
  void load()
}

const filtered = computed(() => !!(q.value.trim() || source.value || docType.value || period.value))
function reset() {
  q.value = ''
  source.value = ''
  docType.value = ''
  period.value = ''
}

const unavailable = computed(() => data.value?.unavailable_sources ?? [])
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Документы</h1>
        <p v-if="data">
          Доступно вам: <span class="num">{{ formatNumber(data.total) }}</span>
          <template v-if="!filtered"> из {{ formatNumber(data.total_in_system) }} в системе</template>
          <template v-else> по выбранным фильтрам</template>
        </p>
      </div>
    </div>

    <div v-if="auth.isGuest" class="alert a-info" style="margin-bottom: 16px">
      <AppIcon name="info" />
      <div>
        <b>Вы вошли как гость</b>Доступны только общие документы.
        <RouterLink :to="{ name: 'login', query: { next: route.fullPath } }">Войти</RouterLink>
      </div>
    </div>
    <div v-for="u in unavailable" :key="u.id" class="alert a-warn" style="margin-bottom: 16px" role="status">
      <AppIcon name="plug" />
      <div>
        <b>{{ u.message }}</b>
        Система не смогла проверить ваши права в «{{ u.name }}», поэтому его документы скрыты.
        Остальные источники работают. Попробуйте обновить страницу через минуту.
      </div>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />

    <div class="card" style="margin-bottom: 16px">
      <div class="card-b doc-filters">
        <div class="field">
          <label class="label" for="doc-q">Поиск</label>
          <div class="input-search">
            <AppIcon name="search" />
            <input id="doc-q" v-model="q" class="input" placeholder="Номер или название" />
          </div>
        </div>
        <div class="field">
          <label class="label" for="doc-source">Источник</label>
          <select id="doc-source" v-model="source" class="select">
            <option value="">Все</option>
            <option v-for="s in filters?.sources ?? []" :key="s.value" :value="s.value">{{ s.label }}</option>
          </select>
        </div>
        <div class="field">
          <label class="label" for="doc-type">Тип</label>
          <select id="doc-type" v-model="docType" class="select">
            <option value="">Все</option>
            <option v-for="t in filters?.doc_types ?? []" :key="t" :value="t">{{ t }}</option>
          </select>
        </div>
        <div class="field">
          <label class="label" for="doc-period">Дата</label>
          <select id="doc-period" v-model="period" class="select">
            <option v-for="p in PERIODS" :key="p.value" :value="p.value">{{ p.label }}</option>
          </select>
        </div>
        <button class="btn btn-ghost" type="button" :disabled="!filtered" @click="reset">Сбросить</button>
      </div>
    </div>

    <div class="card">
      <table class="tbl" :aria-busy="loading">
        <thead>
          <tr>
            <th style="width: 120px">Номер</th>
            <th>Название</th>
            <th>Тип</th>
            <th>Источник</th>
            <th>Дата ↓</th>
            <th>Статус</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && !data">
            <td colspan="7"><div class="skel" style="width: 60%"></div></td>
          </tr>
          <tr v-for="d in data?.items ?? []" :key="d.id">
            <td class="num">{{ d.number ?? '—' }}</td>
            <td class="doc">
              <RouterLink :to="{ name: 'document', params: { id: d.id } }">{{ d.title || d.caption }}</RouterLink>
            </td>
            <td>{{ d.doc_type ?? '—' }}</td>
            <td>{{ d.source_name }}</td>
            <td class="num" style="white-space: nowrap">{{ formatDate(d.doc_date) }}</td>
            <td>
              <LegalStatus :status="d.legal_status" />
            </td>
            <td><span v-if="d.is_common" class="badge b-outline plain">Общий</span></td>
          </tr>
        </tbody>
      </table>
      <div v-if="data && !data.items.length" style="display: grid; min-height: 240px">
        <StateBlock
          v-if="filtered"
          icon="empty"
          tone="plain"
          title="Ничего не найдено"
          text="В доступных вам документах нет подходящих под фильтры. Измените запрос или сбросьте фильтры."
        >
          <button class="btn btn-ghost" type="button" @click="reset">Сбросить фильтры</button>
        </StateBlock>
        <StateBlock
          v-else
          icon="lock"
          title="Нет доступных документов"
          text="Источники пока не выдали вам права ни на один документ. Если документы нужны для работы, обратитесь к руководителю подразделения."
        />
      </div>
      <AppPager
        v-if="data && data.items.length"
        :page="data.page"
        :page-size="data.page_size"
        :total="data.total"
        :shown="data.items.length"
        @go="go"
      />
    </div>
  </div>
</template>

<style scoped>
.doc-filters {
  display: grid;
  grid-template-columns: 2fr repeat(3, 1fr) auto;
  gap: 12px;
  align-items: end;
  padding: 16px;
}
</style>
