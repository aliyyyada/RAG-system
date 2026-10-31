<script setup lang="ts">
import StateBlock from '@/components/StateBlock.vue'

// Раздел, который появится на следующем этапе (см. CLAUDE.md, п. 13).
defineProps<{ title: string; stage: number; embedded?: boolean }>()
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow"><h1>{{ title }}</h1></div>
    </div>
    <div class="card" style="min-height: 320px; display: grid">
      <StateBlock icon="puzzle" tone="plain" :title="`Раздел появится на этапе ${stage}`" text="Сейчас готовы вход, роли и управление пользователями." />
    </div>
  </div>
</template>
