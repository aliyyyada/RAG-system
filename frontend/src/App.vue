<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppHeader from '@/components/AppHeader.vue'
import IconSprite from '@/components/IconSprite.vue'

const route = useRoute()
const bare = computed(() => route.meta.bare === true)
</script>

<template>
  <IconSprite />
  <RouterView v-if="bare" />
  <template v-else>
    <AppHeader />
    <main class="app-main">
      <RouterView />
    </main>
  </template>
</template>
