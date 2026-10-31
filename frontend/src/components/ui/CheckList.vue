<script setup lang="ts">
// Список флажков для выбора ролей, групп или участников.
const model = defineModel<number[]>({ required: true })
defineProps<{
  items: { id: number; name: string; hint?: string; disabled?: boolean }[]
  label: string
  empty?: string
}>()

function toggle(id: number, on: boolean) {
  model.value = on ? [...model.value, id] : model.value.filter((x) => x !== id)
}
</script>

<template>
  <fieldset class="field" style="border: 0; padding: 0; margin: 0">
    <legend class="label" style="margin-bottom: 6px">{{ label }}</legend>
    <div class="checklist">
      <p v-if="!items.length" class="small muted">{{ empty ?? 'Список пуст' }}</p>
      <label v-for="it in items" :key="it.id" class="check">
        <input
          type="checkbox"
          :checked="model.includes(it.id)"
          :disabled="it.disabled"
          @change="toggle(it.id, ($event.target as HTMLInputElement).checked)"
        />
        <span>{{ it.name }}<span v-if="it.hint" class="small muted"> · {{ it.hint }}</span></span>
      </label>
    </div>
  </fieldset>
</template>
