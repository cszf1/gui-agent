import type { DesktopAPI, DesktopEvent } from './shared'

const fragment = new URLSearchParams(window.location.hash.slice(1))
const token = fragment.get('token') || sessionStorage.getItem('gua-control-token') || ''
if (fragment.has('token')) {
  sessionStorage.setItem('gua-control-token', token)
  history.replaceState(null, '', location.pathname)
}

async function call<T>(operation: string, body: unknown = {}, signal?: AbortSignal): Promise<T> {
  if (!token) throw new Error('请从 GUI Agent 快捷方式打开软件。')
  const response = await fetch(`/api/${operation}`, { method: 'POST', signal,
    headers: { 'Content-Type': 'application/json', 'X-Gua-Control': token }, body: JSON.stringify(body) })
  const result = await response.json()
  if (!response.ok) throw new Error(result.error || '本机操作失败')
  return result.value as T
}

const api: DesktopAPI = {
  load: () => call('load'), newSession: () => call('new-session'),
  saveSettings: (input) => call('settings', input), testConnection: (input) => call('test', input),
  start: (input) => call('start', input), control: (input) => call('control', input),
  respond: (input) => call('respond', input), openReport: (runId) => call('report', { runId }),
  storageInfo: () => call('storage'), clearLocalHistory: () => call('clear-history'),
  openDataFolder: () => call('open-data'),
  subscribe: (callback) => {
    let live = true, cursor = 0
    const abort = new AbortController()
    void (async () => {
      while (live) {
        try {
          const result = await call<{ cursor: number; events: DesktopEvent[] }>('poll', { cursor }, abort.signal)
          cursor = result.cursor
          for (const event of result.events) if (live) callback(event)
        } catch {
          if (live) await new Promise((done) => setTimeout(done, 1000))
        }
      }
    })()
    return () => { live = false; abort.abort() }
  },
}
window.desktop = api
