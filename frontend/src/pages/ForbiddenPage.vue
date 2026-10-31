<script setup lang="ts">
import StateBlock from '@/components/StateBlock.vue'
</script>

<template>
  <div class="page" style="display: grid; place-items: center; min-height: 60vh">
    <StateBlock
      icon="lock"
      title="Нет доступа к этому разделу"
      text="У вашей роли нет прав на этот раздел. Если он нужен для работы, обратитесь к администратору системы."
    >
      <RouterLink class="btn btn-secondary" to="/">На главную</RouterLink>
    </StateBlock>
  </div>
</template>
