/** «Иванова Анна Сергеевна» → «Иванова А. С.» (как в шапке макета). */
export function shortName(fullName: string, fallback: string): string {
  const parts = fullName.trim().split(/\s+/).filter(Boolean)
  if (parts.length < 2) return fullName.trim() || fallback
  const [last, ...rest] = parts
  return `${last} ${rest.map((p) => p[0] + '.').join(' ')}`
}

/** Инициалы для кружка: имя + фамилия, «Иванова Анна» → «АИ». */
export function initials(fullName: string, fallback: string): string {
  const parts = fullName.trim().split(/\s+/).filter(Boolean)
  if (parts.length >= 2) return ((parts[1]?.[0] ?? '') + (parts[0]?.[0] ?? '')).toUpperCase()
  return (parts[0] ?? fallback).slice(0, 2).toUpperCase()
}

const dtf = new Intl.DateTimeFormat('ru-RU', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

/** 31.10.2026 14:01 */
export function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  return dtf.format(new Date(iso)).replace(',', '')
}

/** 1 попытка, 2 попытки, 5 попыток */
export function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10
  const m100 = n % 100
  if (m10 === 1 && m100 !== 11) return one
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few
  return many
}

export const ACCOUNT_TYPE_LABEL = { external: 'Внешний', local: 'Локальный', test: 'Тестовый' }
export const ACCOUNT_TYPE_BADGE = { external: 'b-outline', local: 'b-info', test: 'b-warn' }

// ---------- Документы (этап 2) ----------

export const DOC_STATUS_LABEL = {
  queued: 'В очереди',
  processing: 'Обрабатывается',
  ready: 'Готово',
  error: 'Ошибка',
} as const
export const DOC_STATUS_BADGE = {
  queued: 'b-neutral',
  processing: 'b-info b-run',
  ready: 'b-ok',
  error: 'b-danger',
} as const

export const DELETED_REASON = {
  deleted_in_source: 'удалён в источнике',
  missing_in_source: 'не найден у источника при сверке',
} as Record<string, string>

const nf = new Intl.NumberFormat('ru-RU')
/** 4812 → «4 812» */
export function formatNumber(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : nf.format(n)
}

/** 350 → «≈ 5 ч 50 мин» */
export function formatEta(minutes: number | null): string {
  if (minutes === null) return '—'
  if (minutes < 1) return 'меньше минуты'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return '≈ ' + (h ? `${h} ч ` : '') + (m || !h ? `${m} мин` : '')
}

/** 421888 → «412 КБ» */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} КБ`
  return `${(bytes / 1024 / 1024).toFixed(1).replace('.', ',')} МБ`
}

/** 2024-03-14 → 14.03.2024 */
export function formatDate(iso: string | null): string {
  if (!iso) return '—'
  const [y, m, d] = iso.slice(0, 10).split('-')
  return `${d}.${m}.${y}`
}

/** «7c1d2e…e5a4» — длинный guid источника коротко, как в макете. */
export function shortId(id: string | null): string {
  if (!id) return '—'
  return id.length > 12 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id
}

export const EVENT_LABEL: Record<string, string> = {
  discovered: 'Найден у источника',
  changed: 'Изменён в источнике',
  indexed: 'Обработан',
  error: 'Ошибка',
  deleted: 'Удалён',
  restored: 'Восстановлен в источнике',
  retry: 'Повтор по кнопке',
  reindex: 'Переиндексация',
  excluded: 'Исключён из поиска',
  included: 'Возвращён в поиск',
  uploaded: 'Загружен вручную',
}
