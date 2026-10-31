<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { sourcesApi } from '@/api/sources'
import type { Source } from '@/api/types'

// Подключение источника и его настройки: адрес, расписание, ночная сверка.
const open = defineModel<boolean>('open', { required: true })
const props = defineProps<{ source: Source | null }>()
const emit = defineEmits<{ saved: [source: Source] }>()

const form = reactive({
  code: '',
  name: '',
  base_url: 'https://',
  enabled: true,
  sync_interval_minutes: 15,
  overlap_minutes: 60,
  reconcile_hour: 2,
})
const error = ref<string | null>(null)
const busy = ref(false)

watch(open, (v) => {
  if (!v) return
  error.value = null
  const s = props.source
  Object.assign(form, {
    code: s?.code ?? '',
    name: s?.name ?? '',
    base_url: s?.base_url ?? 'https://',
    enabled: s?.enabled ?? true,
    sync_interval_minutes: s?.sync_interval_minutes ?? 15,
    overlap_minutes: s?.overlap_minutes ?? 60,
    reconcile_hour: s?.reconcile_hour ?? 2,
  })
})

async function save() {
  busy.value = true
  error.value = null
  try {
    const { code, ...settings } = form
    const saved = props.source
      ? await sourcesApi.update(props.source.id, settings)
      : await sourcesApi.create({ code: code.trim(), ...settings })
    open.value = false
    emit('saved', saved)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog v-model:open="open" :title="source ? 'Настройки источника' : 'Подключить источник'">
    <form id="source-form" class="stack" @submit.prevent="save">
      <div v-if="!source" class="field">
        <label class="label" for="src-code">Код</label>
        <input id="src-code" v-model="form.code" class="input mono" required pattern="[a-z][a-z0-9_\-]{1,31}" placeholder="docflow" />
        <span class="hint">Латиница, без пробелов. Используется в API для самого источника.</span>
      </div>
      <div class="field">
        <label class="label" for="src-name">Название</label>
        <input id="src-name" v-model="form.name" class="input" required maxlength="200" placeholder="Документооборот" />
      </div>
      <div class="field">
        <label class="label" for="src-url">Адрес API источника</label>
        <input id="src-url" v-model="form.base_url" class="input mono" required pattern="https://.+" />
        <span class="hint">Только https://. Сертификат источника проверяется по CA из папки certs/.</span>
      </div>
      <div class="row" style="gap: 16px; align-items: flex-start">
        <div class="field grow">
          <label class="label" for="src-interval">Синхронизация, каждые N минут</label>
          <input id="src-interval" v-model.number="form.sync_interval_minutes" class="input num" type="number" min="1" max="1440" required />
        </div>
        <div class="field grow">
          <label class="label" for="src-hour">Ночная сверка, час</label>
          <input id="src-hour" v-model.number="form.reconcile_hour" class="input num" type="number" min="0" max="23" required />
        </div>
      </div>
      <div class="field">
        <label class="label" for="src-overlap">Перекрытие окна, минут</label>
        <input id="src-overlap" v-model.number="form.overlap_minutes" class="input num" type="number" min="0" required />
        <span class="hint">Изменения берём с запасом назад, чтобы не потерять документ, записанный источником «задним числом».</span>
      </div>
      <label class="check"><input v-model="form.enabled" type="checkbox" /> Синхронизировать по расписанию</label>
      <FormError :message="error" />
    </form>
    <template #footer>
      <button class="btn btn-secondary" type="button" @click="open = false">Отмена</button>
      <button class="btn btn-primary" type="submit" form="source-form" :disabled="busy">
        {{ source ? 'Сохранить' : 'Подключить' }}
      </button>
    </template>
  </AppDialog>
</template>
