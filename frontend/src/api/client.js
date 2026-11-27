const getOverride = () => {
  if (typeof window !== 'undefined' && window.__RAG_ENDPOINTS__) {
    return window.__RAG_ENDPOINTS__
  }
  return {}
}

export function getServiceBases() {
  const override = getOverride()
  if (import.meta.env.DEV) {
    return {
      agent: override.agent || '',
      data: override.data || '',
      eval: override.eval || ''
    }
  }
  const host = typeof window !== 'undefined' ? window.location.hostname : '127.0.0.1'
  const protocol = typeof window !== 'undefined' ? window.location.protocol : 'http:'
  return {
    agent: override.agent || '',
    data: override.data || `${protocol}//${host}:8001`,
    eval: override.eval || `${protocol}//${host}:8002`
  }
}

export function agentUrl(path) {
  return `${getServiceBases().agent}${path}`
}

export function dataUrl(path) {
  return `${getServiceBases().data}${path}`
}

export function evalUrl(path) {
  return `${getServiceBases().eval}${path}`
}

export async function requestJson(url, options = {}) {
  const resp = await fetch(url, options)
  const text = await resp.text()
  let parsed = null
  try {
    parsed = text ? JSON.parse(text) : {}
  } catch {
    throw new Error(`HTTP ${resp.status}: ${text.slice(0, 160) || resp.statusText}`)
  }
  if (!resp.ok) {
    throw new Error(parsed?.detail || parsed?.message || `HTTP ${resp.status}`)
  }
  return parsed
}
