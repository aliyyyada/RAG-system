/**
 * Разбор текста ответа модели для показа без v-html.
 *
 * Модель пишет простым Markdown: абзацы, списки «1. …» / «- …», **жирный**, ссылки на
 * источники [1][2]. Превращаем это в структуру, которую шаблон рисует обычными тегами:
 * так ни ответ модели, ни текст документа не могут внедрить на страницу свою разметку.
 */

export type Inline = { t: 'text'; v: string } | { t: 'b'; v: string } | { t: 'cite'; n: number }
export type Block = { type: 'p'; inl: Inline[] } | { type: 'ol' | 'ul'; items: Inline[][] }

const CITE_OR_BOLD = /\[(\d{1,2})\]|\*\*(.+?)\*\*/g

export function parseInline(text: string, maxCite: number): Inline[] {
  const out: Inline[] = []
  let last = 0
  for (const m of text.matchAll(CITE_OR_BOLD)) {
    const i = m.index ?? 0
    if (i > last) out.push({ t: 'text', v: text.slice(last, i) })
    if (m[1] !== undefined) {
      const n = Number(m[1])
      // Номер, которого нет среди источников, показываем как текст.
      out.push(n >= 1 && n <= maxCite ? { t: 'cite', n } : { t: 'text', v: m[0] })
    } else {
      out.push({ t: 'b', v: m[2] ?? '' })
    }
    last = i + m[0].length
  }
  if (last < text.length) out.push({ t: 'text', v: text.slice(last) })
  return out
}

const OL = /^\s*\d+[.)]\s+/
const UL = /^\s*[-*•]\s+/

export function parseAnswer(text: string, maxCite: number): Block[] {
  const blocks: Block[] = []
  let para: string[] = []
  const flush = () => {
    if (para.length) blocks.push({ type: 'p', inl: parseInline(para.join(' '), maxCite) })
    para = []
  }
  for (const raw of text.split('\n')) {
    const line = raw.replace(/^#+\s*/, '')
    if (!line.trim()) {
      flush()
      continue
    }
    const kind = OL.test(line) ? 'ol' : UL.test(line) ? 'ul' : null
    if (kind) {
      flush()
      const item = parseInline(line.replace(kind === 'ol' ? OL : UL, ''), maxCite)
      const prev = blocks[blocks.length - 1]
      if (prev && prev.type === kind) prev.items.push(item)
      else blocks.push({ type: kind, items: [item] })
    } else {
      para.push(line.trim())
    }
  }
  flush()
  return blocks
}

/** Отрывок с найденными словами: «…по ⟦наряду⟧-допуску…» → части с флагом подсветки. */
export function splitHighlight(snippet: string): { v: string; hl: boolean }[] {
  const out: { v: string; hl: boolean }[] = []
  const re = /⟦(.*?)⟧/g
  let last = 0
  for (const m of snippet.matchAll(re)) {
    const i = m.index ?? 0
    if (i > last) out.push({ v: snippet.slice(last, i), hl: false })
    out.push({ v: m[1] ?? '', hl: true })
    last = i + m[0].length
  }
  if (last < snippet.length) out.push({ v: snippet.slice(last), hl: false })
  return out
}

/** Текст ответа без ссылок [n] — для кнопки «Скопировать». */
export function plainAnswer(text: string): string {
  return text.replace(/\[(\d{1,2})\]/g, '').replace(/\*\*(.+?)\*\*/g, '$1').trim()
}
