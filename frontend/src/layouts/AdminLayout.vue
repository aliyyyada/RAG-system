<script setup lang="ts">
import { computed } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import { ADMIN_SECTIONS } from '@/router'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

// Левое меню админки по макету; пункт виден, только если есть право на раздел.
const groups = computed(() => {
  const out: { title: string; items: (typeof ADMIN_SECTIONS)[number][] }[] = []
  for (const s of ADMIN_SECTIONS) {
    if (!auth.can(...s.permissions)) continue
    let g = out.find((x) => x.title === s.group)
    if (!g) out.push((g = { title: s.group, items: [] }))
    g.items.push(s)
  }
  return out
})
</script>

<template>
  <div class="admin">
    <nav class="anav" aria-label="Администрирование">
      <template v-for="g in groups" :key="g.title">
        <h4>{{ g.title }}</h4>
        <RouterLink
          v-for="s in g.items"
          :key="s.name"
          :to="{ name: s.name }"
          active-class="on"
        >
          <AppIcon :name="s.icon" />{{ s.title }}
        </RouterLink>
      </template>
    </nav>
    <RouterView />
  </div>
</template>
