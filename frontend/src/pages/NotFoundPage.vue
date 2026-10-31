<script setup lang="ts">
import StateBlock from '@/components/StateBlock.vue'
</script>

<template>
  <div class="page" style="display: grid; place-items: center; min-height: 60vh">
    <StateBlock icon="empty" tone="plain" title="Страница не найдена" text="Проверьте адрес или вернитесь на главную.">
      <RouterLink class="btn btn-secondary" to="/">На главную</RouterLink>
    </StateBlock>
  </div>
</template>
