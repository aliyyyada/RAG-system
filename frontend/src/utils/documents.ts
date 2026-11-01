// Помощники для просмотра документа: статус, разбор текста фрагмента, содержание.
import type { ViewChunk } from '@/api/types'

/** Бейдж статуса документа так, как его назвал источник: «Действует», «Отменён». */
export function legalStatusBadge(status: string): string {
  const s = status.toLowerCase()
  if (s.startsWith('действ')) return 'b-ok'
  if (s.startsWith('отмен') || s.startsWith('утрат')) return 'b-neutral'
  return 'b-outline'
}

export type TextBlock =
  | { type: 'h'; text: string }
  | { type: 'p'; text: string }
  | { type: 'table'; head: string[]; rows: string[][] }

const cells = (line: string) =>
  line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim())

/**
 * Текст фрагмента → абзацы, заголовки и таблицы. Таблицы во фрагментах хранятся
 * в Markdown («| Группа | Кто |»), здесь они снова становятся таблицами.
 * HTML не собираем строкой (никакого v-html): текст документа выводится как текст.
 */
export function parseChunkText(text: string): TextBlock[] {
  const out: TextBlock[] = []
  let table: string[][] | null = null
  const flush = () => {
    if (table && table.length) out.push({ type: 'table', head: table[0] ?? [], rows: table.slice(1) })
    table = null
  }
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (line.startsWith('|')) {
      if (/^\|[\s:|-]+\|$/.test(line)) continue // строка-разделитель |---|---|
      table = table ?? []
      table.push(cells(line))
      continue
    }
    flush()
    if (!line) continue
    const h = /^#{1,6}\s+(.*)$/.exec(line)
    out.push(h ? { type: 'h', text: h[1] ?? '' } : { type: 'p', text: line })
  }
  flush()
  return out
}

export interface TocItem {
  chunkId: number
  title: string
  depth: number
}

/** Содержание: разделы в порядке появления. «4. Порядок › 4.2 Наряд» → глубина 1. */
export function buildToc(chunks: ViewChunk[]): TocItem[] {
  const seen = new Set<string>()
  const toc: TocItem[] = []
  for (const c of chunks) {
    if (!c.section || seen.has(c.section)) continue
    seen.add(c.section)
    const parts = c.section.split('›').map((p) => p.trim())
    toc.push({ chunkId: c.id, title: parts[parts.length - 1] ?? c.section, depth: parts.length - 1 })
  }
  return toc
}

/** Где фрагмент в документе: «стр. 3», «стр. 3–4», «шапка источника». */
export function chunkPlace(c: Pick<ViewChunk, 'kind' | 'page_from' | 'page_to'>): string {
  if (c.kind === 'header') return 'реквизиты и табличные части из источника'
  if (c.page_from === null) return ''
  return c.page_from === c.page_to || c.page_to === null ? `стр. ${c.page_from}` : `стр. ${c.page_from}–${c.page_to}`
}
