<script lang="ts">
export type IconName =
  | 'search'
  | 'doc'
  | 'warn'
  | 'info'
  | 'lock'
  | 'send'
  | 'refresh'
  | 'down'
  | 'x'
  | 'check'
  | 'plus'
  | 'back'
  | 'plug'
  | 'empty'
  | 'cpu'
  | 'users'
  | 'shield'
  | 'db'
  | 'list'
  | 'key'
  | 'puzzle'
  | 'split'
  | 'ext'
</script>

<script setup lang="ts">
withDefaults(defineProps<{ name: IconName; size?: number }>(), { size: 18 })
</script>

<template>
  <svg class="i" :style="size !== 18 ? { width: size + 'px', height: size + 'px' } : undefined" aria-hidden="true">
    <use :href="'#i-' + name" />
  </svg>
</template>
