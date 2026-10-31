<script setup lang="ts">
import AppIcon from '@/components/AppIcon.vue'

defineProps<{ message: string | null }>()
</script>

<template>
  <div v-if="message" class="alert a-danger" role="alert">
    <AppIcon name="warn" />
    <div>{{ message }}</div>
  </div>
</template>
