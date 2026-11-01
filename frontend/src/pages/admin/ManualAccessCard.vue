<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import CheckList from '@/components/ui/CheckList.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { accessApi } from '@/api/sources'
import type { ManualDocumentAccess } from '@/api/types'

// Кому открыта ручная загрузка (ТЗ, п. 6): людям по табельному номеру и группам.
// Документам источника права задаёт сам источник, поэтому карточка только для ручных.
const props = defineProps<{ documentId: number; isCommon: boolean }>()

const access = ref<ManualDocumentAccess | null>(null)
const people = ref<string[]>([])
const groups = ref<number[]>([])
const newNumber = ref('')
const error = ref<string | null>(null)
const saved = ref(false)
const busy = ref(false)

function apply(a: ManualDocumentAccess) {
  access.value = a
  people.value = a.users.map((u) => u.personnel_number)
  groups.value = a.groups.map((g) => g.id)
}

onMounted(async () => {
  try {
    apply(await accessApi.manual(props.documentId))
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить доступ'
  }
})

const names = computed(() => Object.fromEntries((access.value?.users ?? []).map((u) => [u.personnel_number, u.full_name])))

function add() {
  const n = newNumber.value.trim()
  if (n && !people.value.includes(n)) people.value = [...people.value, n]
  newNumber.value = ''
  saved.value = false
}
function remove(n: string) {
  people.value = people.value.filter((x) => x !== n)
  saved.value = false
}

async function save() {
  busy.value = true
  error.value = null
  try {
    apply(await accessApi.setManual(props.documentId, { personnel_numbers: people.value, group_ids: groups.value }))
    saved.value = true
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить доступ'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card">
    <div class="card-h"><h3>Кому доступен</h3></div>
    <div class="card-b stack">
      <p v-if="isCommon" class="small muted">Документ общий: его видят все, в том числе без входа. Список ниже нужен, если снять признак «Общий».</p>
      <FormError :message="error" />
      <template v-if="access">
        <div class="field">
          <span class="label">Люди</span>
          <div class="row wrap">
            <span v-for="n in people" :key="n" class="badge b-outline plain">
              {{ names[n] || n }}<span v-if="names[n]" class="muted">&nbsp;· {{ n }}</span>
              <button type="button" class="btn btn-ghost btn-sm" style="height: 20px; padding: 0 2px" :aria-label="`Убрать ${n}`" @click="remove(n)">
                <AppIcon name="x" :size="14" />
              </button>
            </span>
            <span v-if="!people.length" class="small muted">Никому лично</span>
          </div>
          <form class="row" style="margin-top: 8px" @submit.prevent="add">
            <input v-model="newNumber" class="input" style="width: 180px" placeholder="Табельный номер" aria-label="Табельный номер" />
            <button class="btn btn-secondary btn-sm" type="submit" :disabled="!newNumber.trim()"><AppIcon name="plus" />Добавить</button>
          </form>
        </div>
        <CheckList v-model="groups" :items="access.all_groups" label="Группы" empty="Групп пока нет" @update:model-value="saved = false" />
        <div class="row">
          <button class="btn btn-primary btn-sm" type="button" :disabled="busy" @click="save">Сохранить доступ</button>
          <span v-if="saved" class="small" style="color: var(--ok)">Сохранено</span>
        </div>
      </template>
    </div>
  </div>
</template>
