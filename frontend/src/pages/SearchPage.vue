<script setup lang="ts">
import { RouterLink } from 'vue-router'
import AppIcon from '@/components/AppIcon.vue'
import StateBlock from '@/components/StateBlock.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
</script>

<template>
  <div class="page" style="max-width: 960px; margin: 0 auto">
    <div v-if="auth.isGuest" class="alert a-info" style="margin-bottom: 20px">
      <AppIcon name="info" />
      <div><b>Вы вошли как гость</b>Доступны только общие документы. <RouterLink to="/login">Войти</RouterLink></div>
    </div>
    <div class="page-h">
      <div class="grow">
        <h1>Поиск по документам</h1>
        <p>Задайте вопрос — система найдёт ответ в документах, доступных вам.</p>
      </div>
    </div>
    <div class="card" style="min-height: 320px; display: grid">
      <StateBlock
        icon="search"
        tone="plain"
        title="Поиск и чат появятся на этапе 4"
        text="Сейчас работают вход, роли и права, управление пользователями и группами."
      />
    </div>
  </div>
</template>
