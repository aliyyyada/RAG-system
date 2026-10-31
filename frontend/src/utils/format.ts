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
