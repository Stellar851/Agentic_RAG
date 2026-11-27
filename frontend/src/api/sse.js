/**
 * 通用 POST SSE (Server-Sent Events) 流式消费器
 * 支持 AbortController 中断与多行 data: 帧拼接解析
 */
export async function streamSSEPost(url, payload, handlers = {}, signal = undefined) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
    signal
  })

  if (!response.ok) {
    const errText = await response.text()
    throw new Error(`HTTP ${response.status}: ${errText.slice(0, 200) || response.statusText}`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  const dispatchBlock = (block) => {
    if (!block || !block.trim()) return
    let eventName = 'message'
    const dataLines = []
    for (const rawLine of block.split(/\r?\n/)) {
      if (rawLine.startsWith('event:')) {
        eventName = rawLine.slice(6).trim()
      } else if (rawLine.startsWith('data:')) {
        dataLines.push(rawLine.slice(5).trimStart())
      }
    }
    if (!dataLines.length) return
    const rawData = dataLines.join('\n')
    try {
      const parsed = JSON.parse(rawData)
      if (handlers[eventName]) handlers[eventName](parsed)
      if (handlers.all) handlers.all(eventName, parsed)
    } catch (err) {
      console.warn('SSE JSON parse error:', err, rawData)
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      dispatchBlock(block)
    }
  }

  if (buffer.trim()) {
    dispatchBlock(buffer)
  }
}
