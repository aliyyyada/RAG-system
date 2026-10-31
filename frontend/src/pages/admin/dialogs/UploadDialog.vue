<script setup lang="ts">
import { ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { sourcesApi } from '@/api/sources'

// Ручная загрузка (для отладки): PDF или DOCX в коллекцию «Ручные загрузки».
const open = defineModel<boolean>('open', { required: true })
const emit = defineEmits<{ uploaded: [id: number] }>()

const file = ref<File | null>(null)
const title = ref('')
const number = ref('')
const docType = ref('')
const isCommon = ref(false)
const error = ref<string | null>(null)
const busy = ref(false)

watch(open, (v) => {
  if (!v) return
  file.value = null
  title.value = number.value = docType.value = ''
  isCommon.value = false
  error.value = null
})

function pick(e: Event) {
  const f = (e.target as HTMLInputElement).files?.[0] ?? null
  file.value = f
  if (f && !title.value) title.value = f.name.replace(/\.(pdf|docx)$/i, '')
}

async function submit() {
  if (!file.value) {
    error.value = 'Выберите файл'
    return
  }
  busy.value = true
  error.value = null
  const form = new FormData()
  form.append('file', file.value)
  form.append('title', title.value.trim())
  if (number.value.trim()) form.append('number', number.value.trim())
  if (docType.value.trim()) form.append('doc_type', docType.value.trim())
  form.append('is_common', String(isCommon.value))
  try {
    const r = await sourcesApi.upload(form)
    open.value = false
    emit('uploaded', r.id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить файл'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog v-model:open="open" title="Загрузить документ">
    <form id="upload-form" class="stack" @submit.prevent="submit">
      <div class="field">
        <label class="label" for="up-file">Файл PDF или DOCX</label>
        <input id="up-file" type="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" @change="pick" />
        <span class="hint">Документ попадёт в коллекцию «Ручные загрузки» и через несколько секунд будет разобран.</span>
      </div>
      <div class="field">
        <label class="label" for="up-title">Название</label>
        <input id="up-title" v-model="title" class="input" required maxlength="1000" />
      </div>
      <div class="row" style="gap: 16px">
        <div class="field grow">
          <label class="label" for="up-type">Вид документа</label>
          <input id="up-type" v-model="docType" class="input" maxlength="100" placeholder="Приказ" />
        </div>
        <div class="field grow">
          <label class="label" for="up-number">Номер</label>
          <input id="up-number" v-model="number" class="input" maxlength="100" placeholder="112" />
        </div>
      </div>
      <label class="check"><input v-model="isCommon" type="checkbox" /> Общий документ — доступен всем, в том числе без входа</label>
      <FormError :message="error" />
    </form>
    <template #footer>
      <button class="btn btn-secondary" type="button" @click="open = false">Отмена</button>
      <button class="btn btn-primary" type="submit" form="upload-form" :disabled="busy">Загрузить</button>
    </template>
  </AppDialog>
</template>
