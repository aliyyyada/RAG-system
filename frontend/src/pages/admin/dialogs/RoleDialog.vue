<script setup lang="ts">
import { ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import FormError from '@/components/ui/FormError.vue'
import { adminApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import type { Role } from '@/api/types'

// Новая роль (пустая — галочки ставятся в матрице) или переименование/удаление своей роли.
const open = defineModel<boolean>('open', { required: true })
const props = defineProps<{ role: Role | null }>()
const emit = defineEmits<{ saved: [] }>()

const name = ref('')
const description = ref('')
const error = ref<string | null>(null)
const busy = ref(false)
const confirmDelete = ref(false)

watch(open, (v) => {
  if (!v) return
  name.value = props.role?.name ?? ''
  description.value = props.role?.description ?? ''
  error.value = null
  confirmDelete.value = false
})

async function save() {
  error.value = null
  if (!name.value.trim()) {
    error.value = 'Укажите название роли'
    return
  }
  busy.value = true
  try {
    if (props.role) {
      await adminApi.updateRole(props.role.id, { name: name.value.trim(), description: description.value })
    } else {
      await adminApi.createRole({ name: name.value.trim(), description: description.value, permissions: [] })
    }
    open.value = false
    emit('saved')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить'
  } finally {
    busy.value = false
  }
}

async function remove() {
  if (!props.role) return
  if (!confirmDelete.value) {
    confirmDelete.value = true
    return
  }
  busy.value = true
  try {
    await adminApi.deleteRole(props.role.id)
    open.value = false
    emit('saved')
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось удалить'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog v-model:open="open" :title="role ? 'Роль' : 'Новая роль'">
    <FormError :message="error" />
    <div class="field">
      <label class="label" for="rd-name">Название</label>
      <input id="rd-name" v-model="name" class="input" placeholder="Например, Специалист ОТ" />
    </div>
    <div class="field">
      <label class="label" for="rd-desc">Описание</label>
      <textarea id="rd-desc" v-model="description" class="textarea" rows="2"></textarea>
    </div>
    <p v-if="!role" class="small muted">После создания отметьте права роли галочками в таблице и нажмите «Сохранить».</p>
    <p v-else class="small muted">
      Назначена людям: {{ role.users_count }}, группам: {{ role.groups_count }}. При удалении роль
      пропадёт у всех.
    </p>
    <template #footer>
      <button v-if="role" type="button" class="btn btn-danger" style="margin-right: auto" :disabled="busy" @click="remove">
        {{ confirmDelete ? 'Точно удалить?' : 'Удалить роль' }}
      </button>
      <button type="button" class="btn btn-secondary" @click="open = false">Отмена</button>
      <button type="button" class="btn btn-primary" :disabled="busy" @click="save">
        {{ role ? 'Сохранить' : 'Создать' }}
      </button>
    </template>
  </AppDialog>
</template>
