<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ page: number; pageSize: number; total: number; shown: number }>()
const emit = defineEmits<{ go: [page: number] }>()

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
const from = computed(() => (props.total === 0 ? 0 : (props.page - 1) * props.pageSize + 1))
const to = computed(() => (props.page - 1) * props.pageSize + props.shown)

// 1 2 3 … 36 — как в макете: первые, текущая с соседями, последняя.
const items = computed<(number | '…')[]>(() => {
  const n = pages.value
  const p = props.page
  const set = new Set([1, 2, 3, p - 1, p, p + 1, n].filter((x) => x >= 1 && x <= n))
  const sorted = [...set].sort((a, b) => a - b)
  const out: (number | '…')[] = []
  sorted.forEach((x, i) => {
    if (i > 0 && x - (sorted[i - 1] ?? x) > 1) out.push('…')
    out.push(x)
  })
  return out
})
</script>

<template>
  <div class="pager">
    <span class="num">{{ from }}–{{ to }} из {{ total }}</span>
    <span class="grow"></span>
    <template v-for="(x, i) in items" :key="i">
      <span v-if="x === '…'">…</span>
      <button
        v-else
        type="button"
        class="pg"
        :class="{ on: x === page }"
        :aria-current="x === page ? 'page' : undefined"
        :disabled="x === page"
        @click="emit('go', x)"
      >
        {{ x }}
      </button>
    </template>
  </div>
</template>
