<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import AppPager from '@/components/ui/AppPager.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { sourcesApi } from '@/api/sources'
import type { CollectionInfo, DocumentPage } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import {
  DELETED_REASON,
  DOC_STATUS_BADGE,
  DOC_STATUS_LABEL,
  formatDateTime,
  formatNumber,
  shortId,
} from '@/utils/format'
import UploadDialog from './dialogs/UploadDialog.vue'

// «Коллекции и документы»: все документы реестра с фильтрами, вход в карточку документа.
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const collections = ref<CollectionInfo[]>([])
const data = ref<DocumentPage | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)
const uploadOpen = ref(false)

const collection = ref<number | null>(route.query.collection ? Number(route.query.collection) : null)
const status = ref<string | null>((route.query.status as string) || null)
const q = ref('')
const page = ref(1)

async function load() {
  loading.value = true
  error.value = null
  try {
    const [c, d] = await Promise.all([
      sourcesApi.collections(),
      sourcesApi.documents({
        collection_id: collection.value,
        status: status.value,
        q: q.value.trim() || undefined,
        page: page.value,
        page_size: 30,
      }),
    ])
    collections.value = c
    data.value = d
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить документы'
  } finally {
    loading.value = false
  }
}
onMounted(load)

watch([collection, status], () => {
  page.value = 1
  void router.replace({
    query: { ...(collection.value ? { collection: collection.value } : {}), ...(status.value ? { status: status.value } : {}) },
  })
  void load()
})
let timer: number | undefined
watch(q, () => {
  window.clearTimeout(timer)
  timer = window.setTimeout(() => {
    page.value = 1
    void load()
  }, 300)
})

function go(p: number) {
  page.value = p
  void load()
}

const total = computed(() => collections.value.reduce((n, c) => n + c.documents, 0))
const canUpload = computed(() => auth.can('documents.upload_manual'))

async function uploaded(docId: number) {
  await router.push({ name: 'admin-document', params: { id: docId } })
}
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Коллекции и документы</h1>
        <p>Реестр всех загруженных документов: статус обработки, фрагменты, исключение из поиска.</p>
      </div>
      <button v-if="canUpload" class="btn btn-primary" type="button" @click="uploadOpen = true">
        <AppIcon name="plus" />Загрузить документ
      </button>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />

    <div class="row wrap" style="margin-bottom: 12px">
      <button type="button" class="chip" :class="{ on: collection === null }" @click="collection = null">
        Все <b class="num">{{ formatNumber(total) }}</b>
      </button>
      <button
        v-for="c in collections"
        :key="c.id"
        type="button"
        class="chip"
        :class="{ on: collection === c.id }"
        @click="collection = c.id"
      >
        {{ c.name }} <b class="num">{{ formatNumber(c.documents) }}</b>
        <span v-if="c.errors" class="badge b-danger plain" style="height: 20px">{{ c.errors }}</span>
      </button>
    </div>
    <div class="row" style="margin-bottom: 12px">
      <div class="input-search" style="width: 360px">
        <AppIcon name="search" />
        <input v-model="q" class="input" placeholder="Номер, название, id источника, текст ошибки" aria-label="Поиск документа" />
      </div>
      <select v-model="status" class="select" style="width: 220px" aria-label="Статус">
        <option :value="null">Все статусы</option>
        <option value="ready">Готово</option>
        <option value="queued">В очереди</option>
        <option value="processing">Обрабатывается</option>
        <option value="error">Ошибка</option>
        <option value="excluded">Исключены из поиска</option>
        <option value="deleted">Удалены в источнике</option>
      </select>
    </div>

    <div class="card">
      <table class="tbl" :aria-busy="loading">
        <thead>
          <tr>
            <th>Документ</th>
            <th>Коллекция</th>
            <th>Версия</th>
            <th>Статус</th>
            <th class="r">Фрагментов</th>
            <th>Доступ</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && !data">
            <td colspan="6"><div class="skel" style="width: 60%"></div></td>
          </tr>
          <tr v-for="d in data?.items ?? []" :key="d.id">
            <td class="doc">
              <RouterLink :to="{ name: 'admin-document', params: { id: d.id } }">{{ d.caption }}</RouterLink>
              <div class="small muted mono">D-{{ String(d.id).padStart(6, '0') }}<template v-if="d.external_id"> · {{ shortId(d.external_id) }}</template></div>
            </td>
            <td>{{ d.source_name ?? 'Ручные загрузки' }}</td>
            <td class="num" style="white-space: nowrap">{{ formatDateTime(d.version ?? d.indexed_version) }}</td>
            <td>
              <span v-if="d.deleted_at" class="badge b-outline">Удалён</span>
              <span v-else class="badge" :class="DOC_STATUS_BADGE[d.status]">{{ DOC_STATUS_LABEL[d.status] }}</span>
              <div v-if="d.deleted_at" class="small muted">{{ DELETED_REASON[d.deleted_reason ?? ''] }}</div>
              <div v-else-if="d.status === 'error'" class="small muted">{{ d.error_stage_title }}</div>
              <div v-if="d.is_excluded" class="small muted">исключён из поиска</div>
            </td>
            <td class="r num">{{ d.chunk_count }}</td>
            <td style="white-space: nowrap"><span v-if="d.is_common" class="badge b-info plain">Общий</span><span v-else class="muted small">по правам</span></td>
          </tr>
        </tbody>
      </table>
      <div v-if="data && !data.items.length" style="display: grid; min-height: 240px">
        <StateBlock icon="empty" tone="plain" title="Документов не найдено" text="Измените фильтры или дождитесь синхронизации с источником." />
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
    <UploadDialog v-model:open="uploadOpen" @uploaded="uploaded" />
  </div>
</template>
