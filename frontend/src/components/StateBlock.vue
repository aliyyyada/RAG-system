<script setup lang="ts">
import AppIcon, { type IconName } from '@/components/AppIcon.vue'

// Состояния из макета «Состояния»: нет доступа, ничего не найдено, ошибка и т. п.
defineProps<{
  icon: IconName
  title: string
  text?: string
  tone?: 'neutral' | 'danger' | 'warn' | 'plain'
}>()

const tones = {
  neutral: { background: 'var(--neutral-tint)', color: 'var(--ink-700)' },
  danger: { background: 'var(--danger-tint)', color: 'var(--danger)' },
  warn: { background: 'var(--warn-tint)', color: 'var(--warn)' },
  plain: { background: 'var(--surface-2)', color: 'var(--ink-700)' },
}
</script>

<template>
  <div class="state-b">
    <div>
      <div class="ico" :style="tones[tone ?? 'neutral']"><AppIcon :name="icon" :size="28" /></div>
      <h3>{{ title }}</h3>
      <p v-if="text">{{ text }}</p>
      <div v-if="$slots.default" style="margin-top: 16px"><slot /></div>
    </div>
  </div>
</template>
