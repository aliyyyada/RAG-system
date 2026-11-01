<script setup lang="ts">
import { computed } from 'vue'
import { statusBadges } from '@/utils/documents'

// Статус документа: «Действует», «Отменён», «Действует» + «Есть изменения».
const props = defineProps<{ status: string | null | undefined }>()
const badges = computed(() => statusBadges(props.status))
</script>

<template>
  <span v-for="b in badges" :key="b.text" class="badge" :class="b.cls">{{ b.text }}</span>
</template>
