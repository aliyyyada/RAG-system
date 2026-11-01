<script setup lang="ts">
import { onMounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { accessApi, sourcesApi } from '@/api/sources'
import type { Source } from '@/api/types'
import { formatDateTime, formatNumber } from '@/utils/format'
import SourceDialog from './dialogs/SourceDialog.vue'

// Список подключённых источников. Сейчас источник один, позже появится второй.
const sources = ref<Source[] | null>(null)
const error = ref<string | null>(null)
const createOpen = ref(false)

// Права на документы кэшируются на несколько минут (ACCESS_CACHE_MINUTES). Если в источнике
// поменяли права и человек должен увидеть это сразу — сбрасываем кэш.
const resetting = ref(false)
const resetDone = ref<number | null>(null)
async function resetCache() {
  resetting.value = true
  error.value = null
  try {
    resetDone.value = (await accessApi.resetCache()).reset
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сбросить кэш прав'
  } finally {
    resetting.value = false
  }
}

async function load() {
  error.value = null
  try {
    sources.value = await sourcesApi.list()
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить источники'
  }
}
onMounted(load)
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>Источники</h1>
        <p>Внешние системы, из которых загружаются документы. Права на документы спрашиваем у источника.</p>
      </div>
      <button class="btn btn-secondary" type="button" :disabled="resetting" @click="resetCache">
        <AppIcon name="refresh" />Сбросить кэш прав
      </button>
      <button class="btn btn-primary" type="button" @click="createOpen = true">
        <AppIcon name="plus" />Подключить источник
      </button>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />
    <div v-if="resetDone !== null" class="alert a-ok" style="margin-bottom: 12px" role="status">
      <AppIcon name="check" />
      <div>
        <b>Кэш прав сброшен</b>При следующем запросе каждого пользователя права на документы будут
        запрошены у источников заново (записей сброшено: {{ resetDone }}).
      </div>
    </div>

    <div class="card">
      <table class="tbl">
        <thead>
          <tr>
            <th>Источник</th>
            <th>Состояние</th>
            <th class="r">У источника</th>
            <th class="r">Загружено</th>
            <th class="r">В очереди</th>
            <th class="r">С ошибкой</th>
            <th>Последняя синхронизация</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!sources">
            <td colspan="7"><div class="skel" style="width: 60%"></div></td>
          </tr>
          <tr v-for="s in sources ?? []" :key="s.id">
            <td class="doc">
              <RouterLink :to="{ name: 'admin-source', params: { id: s.id } }">{{ s.name }}</RouterLink>
              <div class="small muted mono">{{ s.code }}</div>
            </td>
            <td>
              <span v-if="!s.enabled" class="badge b-neutral">Выключен</span>
              <span v-else-if="s.available === false" class="badge b-danger">Не отвечает</span>
              <span v-else-if="s.available" class="badge b-ok">Отвечает</span>
              <span v-else class="badge b-neutral">Ещё не опрашивался</span>
            </td>
            <td class="r num">{{ formatNumber(s.report.at_source) }}</td>
            <td class="r num">{{ formatNumber(s.report.ready) }}</td>
            <td class="r num">{{ formatNumber(s.report.queued + s.report.processing) }}</td>
            <td class="r num">
              <span v-if="s.report.error" class="badge b-danger plain">{{ formatNumber(s.report.error) }}</span>
              <template v-else>0</template>
            </td>
            <td class="num">{{ formatDateTime(s.last_sync_at) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="sources && !sources.length" style="display: grid; min-height: 240px">
        <StateBlock
          icon="db"
          tone="plain"
          title="Источников пока нет"
          text="Подключите систему, из которой загружать документы: укажите её код и адрес API."
        />
      </div>
    </div>
    <SourceDialog v-model:open="createOpen" :source="null" @saved="load" />
  </div>
</template>
