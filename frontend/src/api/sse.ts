/**
 * Разбор потока Server-Sent Events из fetch-ответа.
 *
 *   event: token
 *   data: {"text": "Для допуска"}
 *
 *   : ping            ← heartbeat сервера раз в 2 с, пропускаем
 */
export interface SseEvent {
  event: string
  data: unknown
}

export async function* readSse(res: Response): AsyncGenerator<SseEvent> {
  if (!res.body) return
  const reader = res.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buf = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let idx: number
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx)
        buf = buf.slice(idx + 2)
        const ev = parseBlock(block)
        if (ev) yield ev
      }
    }
  } finally {
    reader.releaseLock()
  }
}

function parseBlock(block: string): SseEvent | null {
  let event = 'message'
  const data: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
  }
  if (!data.length) return null
  try {
    return { event, data: JSON.parse(data.join('\n')) }
  } catch {
    return null
  }
}
