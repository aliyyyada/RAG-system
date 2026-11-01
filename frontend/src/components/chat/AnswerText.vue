<script setup lang="ts">
import { computed } from 'vue'
import { parseAnswer, type Inline } from '@/utils/answer'

// Ответ модели: абзацы, списки, жирный и ссылки на источники [n] — без v-html.
const props = defineProps<{ text: string; sources: number; streaming?: boolean }>()
const emit = defineEmits<{ cite: [n: number]; hover: [n: number | null] }>()
const blocks = computed(() => parseAnswer(props.text, props.sources))

function key(i: Inline, j: number) {
  return `${i.t}-${j}`
}
</script>

<template>
  <div class="answer-text">
    <template v-for="(b, i) in blocks" :key="i">
      <p v-if="b.type === 'p'">
        <template v-for="(x, j) in b.inl" :key="key(x, j)">
          <b v-if="x.t === 'b'">{{ x.v }}</b>
          <button
            v-else-if="x.t === 'cite'"
            type="button"
            class="cite"
            :aria-label="`Источник ${x.n}`"
            @click="emit('cite', x.n)"
            @mouseenter="emit('hover', x.n)"
            @mouseleave="emit('hover', null)"
          >{{ x.n }}</button>
          <template v-else>{{ x.v }}</template>
        </template>
      </p>
      <component :is="b.type" v-else>
        <li v-for="(item, k) in b.items" :key="k">
          <template v-for="(x, j) in item" :key="key(x, j)">
            <b v-if="x.t === 'b'">{{ x.v }}</b>
            <button
              v-else-if="x.t === 'cite'"
              type="button"
              class="cite"
              :aria-label="`Источник ${x.n}`"
              @click="emit('cite', x.n)"
              @mouseenter="emit('hover', x.n)"
              @mouseleave="emit('hover', null)"
            >{{ x.n }}</button>
            <template v-else>{{ x.v }}</template>
          </template>
        </li>
      </component>
    </template>
    <span v-if="streaming" class="caret" aria-hidden="true"></span>
  </div>
</template>

<style scoped>
.answer-text {
  display: grid;
  gap: var(--s-3);
}
.answer-text p {
  margin: 0;
}
.answer-text ol,
.answer-text ul {
  margin: 0;
  padding-left: 22px;
  display: grid;
  gap: 6px;
}
button.cite {
  font-family: inherit;
  line-height: 1;
}
.caret {
  display: inline-block;
  width: 8px;
  height: 18px;
  background: var(--brand-orange);
  animation: pulse 1s infinite;
  vertical-align: -3px;
}
</style>
