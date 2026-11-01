<script setup lang="ts">
import { computed } from 'vue'
import type { AnswerSource } from '@/api/types'
import { splitHighlight } from '@/utils/answer'
import { legalStatusBadge } from '@/utils/documents'

// Карточка источника ответа справа: номер, документ, статус, место, отрывок с подсветкой.
// Нажатие открывает документ на этом фрагменте: /documents/12?chunk=345.
const props = defineProps<{ source: AnswerSource; active?: boolean }>()
const parts = computed(() => splitHighlight(props.source.snippet ?? ''))
const to = computed(() =>
  props.source.document_id
    ? { name: 'document', params: { id: props.source.document_id }, query: { chunk: props.source.chunk_id, from: 'chat' } }
    : null,
)
</script>

<template>
  <RouterLink v-if="to && source.available !== false" :to="to" class="src" :class="{ on: active }" :data-n="source.n">
    <div class="src-t"><span class="cite">{{ source.n }}</span>{{ source.caption }}</div>
    <div class="src-meta">
      <span v-if="source.legal_status" class="badge" :class="legalStatusBadge(source.legal_status)">{{ source.legal_status }}</span>
      <span v-if="source.place">{{ source.place }}</span>
      <span v-if="source.source_name">{{ source.source_name }}</span>
    </div>
    <div v-if="parts.length" class="src-q">
      <template v-for="(p, i) in parts" :key="i"><mark v-if="p.hl">{{ p.v }}</mark><template v-else>{{ p.v }}</template></template>
    </div>
  </RouterLink>
  <div v-else class="src src-off" :data-n="source.n">
    <div class="src-t"><span class="cite">{{ source.n }}</span>{{ source.caption }}</div>
    <div class="src-meta">Права на документ отозваны источником или документ удалён</div>
  </div>
</template>

<style scoped>
.src {
  text-decoration: none;
  color: inherit;
}
.src-off {
  cursor: default;
  color: var(--ink-500);
  background: var(--surface-1);
}
.src-q mark {
  background: var(--accent-tint);
  color: inherit;
  padding: 0 1px;
}
</style>
