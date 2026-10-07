export type RunStatus = 'starting' | 'running' | 'pausing' | 'paused' | 'waiting' | 'done' | 'fail' | 'uncertain' | 'stopped' | 'error' | 'interrupted'

export interface Settings {
  provider: 'openai' | 'anthropic'
  baseUrl: string
  model: string
  target: 'desktop' | 'browser'
  startUrl: string
  taskWindow: string
  safetyMode: 'confirm' | 'deny'
  maxSteps: number
  pythonPath: string
}
export interface PublicSettings extends Settings { hasApiKey: boolean; keyPersisted: boolean }
export interface SettingsInput extends Settings { apiKey?: string; clearApiKey?: boolean }
export interface RunResult {
  status: string; claimed_done?: boolean; steps?: number; seconds?: number; message?: string; answer?: string
  budget?: { calls?: number; seconds?: number; prompt_tokens?: number; completion_tokens?: number }
  performance?: { phases?: Record<string, { seconds: number; count: number }>; execution_routes?: Record<string, number> }
}
export interface AgentEvent {
  type: 'state' | 'log' | 'preview' | 'privacy' | 'request' | 'result' | 'error'
  runId: string
  time?: number
  state?: RunStatus
  record?: Record<string, unknown>
  image?: string
  title?: string
  url?: string
  requestId?: string
  kind?: 'confirm' | 'ask'
  question?: string
  action?: Record<string, unknown>
  result?: RunResult
  message?: string
  reportReady?: boolean
}
export interface Run {
  id: string; sessionId: string; task: string; createdAt: number; demo: boolean
  status: RunStatus; events: AgentEvent[]; result?: RunResult; reportReady?: boolean
}
export interface Session { id: string; title: string; createdAt: number; runs: Run[] }
export interface Runtime { platform: string; shortcutAvailable: boolean; bundledWorker: boolean; engine?: string }
export interface AppState { settings: PublicSettings; sessions: Session[]; runtime: Runtime }
export type DesktopEvent = AgentEvent | { type: 'run-created'; run: Run }
export interface DesktopAPI {
  load(): Promise<AppState>
  newSession(): Promise<Session>
  saveSettings(input: SettingsInput): Promise<PublicSettings>
  testConnection(input: SettingsInput): Promise<{ ok: boolean; message: string }>
  start(input: { sessionId: string; task: string; demo?: boolean }): Promise<Run>
  control(input: { runId: string; action: 'pause' | 'resume' | 'stop' }): Promise<void>
  respond(input: { runId: string; requestId: string; approved?: boolean; answer?: string }): Promise<void>
  openReport(runId: string): Promise<void>
  subscribe(callback: (event: DesktopEvent) => void): () => void
}

export const defaults: Settings = {
  provider: 'openai', baseUrl: 'https://api.openai.com/v1', model: '',
  target: 'desktop', startUrl: 'https://example.com', taskWindow: '',
  safetyMode: 'confirm', maxSteps: 50, pythonPath: '',
}
export const activeStatuses = new Set<RunStatus>(['starting', 'running', 'pausing', 'paused', 'waiting'])

export function sameService(a: Pick<Settings, 'provider' | 'baseUrl'>, b: Pick<Settings, 'provider' | 'baseUrl'>) {
  try {
    return a.provider === b.provider && new URL(a.baseUrl.trim()).href.replace(/\/+$/, '') ===
      new URL(b.baseUrl.trim()).href.replace(/\/+$/, '')
  } catch { return false }
}

export function resultStatus(result?: RunResult): RunStatus {
  const value = result?.status
  if (value === 'done') return 'done'
  if (value === 'user_abort') return 'stopped'
  if (value === 'uncertain' || value === 'privacy_blocked') return 'uncertain'
  return 'fail'
}

export function applyEvent(run: Run, event: AgentEvent): Run {
  if (run.id !== event.runId) return run
  const next = { ...run }
  if (event.type === 'state' && event.state) next.status = event.state
  if (event.type === 'request') next.status = 'waiting'
  if (event.type === 'result') {
    next.result = event.result
    next.status = resultStatus(event.result)
    next.reportReady = event.reportReady
  }
  if (event.type === 'error') next.status = 'error'
  if (event.type !== 'preview') next.events = [...run.events, event].slice(-300)
  return next
}

export const statusText: Record<RunStatus, string> = {
  starting: '启动中', running: '执行中', pausing: '正在暂停', paused: '已暂停', waiting: '等待你确认',
  done: '已完成', fail: '未完成', uncertain: '需要检查', stopped: '已停止', error: '运行出错', interrupted: '运行已中断',
}
