<script setup lang="ts">
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'
import AppIcon from '@/components/AppIcon.vue'

// Модальное окно на Reka UI (фокус внутри окна, Esc, aria) в стиле дизайн-системы.
const open = defineModel<boolean>('open', { required: true })
defineProps<{ title: string; description?: string; wide?: boolean }>()
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay class="dlg-overlay" />
      <DialogContent class="dlg" :style="wide ? { width: '720px' } : undefined">
        <div class="dlg-h">
          <DialogTitle as="h3">{{ title }}</DialogTitle>
          <DialogClose class="icon-btn" aria-label="Закрыть"><AppIcon name="x" /></DialogClose>
        </div>
        <DialogDescription v-if="description" class="sr-only">{{ description }}</DialogDescription>
        <div class="dlg-b"><slot /></div>
        <div v-if="$slots.footer" class="dlg-f"><slot name="footer" /></div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>
