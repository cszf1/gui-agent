import { app, BrowserWindow, globalShortcut, ipcMain, safeStorage, shell } from 'electron'
import { dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { z } from 'zod'
import { Store } from './store'
import { WorkerClient, requestSettings } from './worker'
import { controlSchema, responseSchema, settingsSchema, startSchema } from './validation'
import { activeStatuses, applyEvent, type AgentEvent, type AppState, type DesktopEvent, type Run, type SettingsInput } from '../src/shared'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '../../..')
const STOP_SHORTCUT = 'CommandOrControl+Alt+Shift+Escape'
let window: BrowserWindow | null = null
let store: Store
let worker: WorkerClient | undefined
let workerSession = ''
let active: Run | undefined
let pending: AgentEvent | undefined
let quitting = false
let testing = false
let shortcutAvailable = false
let stopRequested = false
const reports = new Map<string, string>()

function publish(event: DesktopEvent) {
  if (window && !window.isDestroyed()) window.webContents.send('agent:event', event)
}

function state(): AppState {
  return { settings: store.publicSettings(), sessions: store.sessions,
    runtime: { platform: process.platform, shortcutAvailable, bundledWorker: app.isPackaged } }
}

function restoreWindow() {
  if (!window) return
  window.show()
  if (window.isMinimized()) window.restore()
  window.focus()
}

function hideForDesktop(run: Run) {
  if (!run.demo && store.settings.target === 'desktop') window?.minimize()
}

function acceptEvent(value: Record<string, unknown>) {
  if (!active || value.runId !== active.id) return
  if (!['state', 'log', 'preview', 'privacy', 'request', 'result', 'error'].includes(String(value.type))) return
  const event = { ...value } as unknown as AgentEvent
  // Reports may be opened only from the app-owned runs directory.
  if (event.type === 'result') {
    const candidate = typeof value.report === 'string' ? resolve(value.report) : ''
    const expected = join(app.getPath('userData'), 'runs', active.id, 'report.html')
    if (candidate === expected && existsSync(expected)) reports.set(active.id, expected)
    event.reportReady = reports.has(active.id)
    delete (event as unknown as Record<string, unknown>).report
  }
  const previousId = active.id
  const session = store.sessions.find((s) => s.id === active!.sessionId)
  active = applyEvent(active, event)
  if (session) session.runs = session.runs.map((r) => r.id === previousId ? active! : r)
  if (event.type === 'request') { pending = event; restoreWindow() }
  if (event.type === 'state' && event.state === 'paused') restoreWindow()
  if (event.type !== 'preview') store.persist()
  publish(event)
  if (event.type === 'result' || event.type === 'error') {
    active = undefined
    pending = undefined
    restoreWindow()
  }
}

function makeWorker(pythonPath: string, onEvent = acceptEvent, onExit = () => {
  worker = undefined
  if (active) acceptEvent({ type: 'result', runId: active.id,
    result: { status: 'user_abort', claimed_done: false, message: '执行进程已停止' } })
}) {
  return new WorkerClient({ root, resources: process.resourcesPath, packaged: app.isPackaged,
    cwd: app.isPackaged ? app.getPath('userData') : root, pythonPath, onEvent, onExit })
}

async function stop() {
  stopRequested = true
  const old = worker
  if (active && old) {
    try { old.send({ command: 'stop', runId: active.id }) } catch { /* process already exited */ }
  }
  await old?.terminate()
  if (worker === old) worker = undefined
  pending = undefined
  restoreWindow()
}

function handle(name: string, callback: (arg: unknown) => unknown) {
  ipcMain.handle(name, async (event, arg) => {
    if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) {
      throw new Error('无效的桌面调用')
    }
    try { return await callback(arg) } catch (error) {
      if (error instanceof z.ZodError) throw new Error(error.issues[0]?.message || '参数不正确')
      throw new Error(error instanceof Error ? error.message : '操作失败')
    }
  })
}

function installIPC() {
  handle('agent:load', () => {
    if (!store.sessions.length) store.newSession()
    return state()
  })
  handle('agent:new-session', () => {
    if (active || testing) throw new Error('请先结束当前任务。')
    return store.newSession()
  })
  handle('agent:settings', async (arg) => {
    if (active || testing) throw new Error('请先结束当前任务，再修改设置。')
    const input = settingsSchema.parse(arg)
    const old = worker
    worker = undefined
    await old?.terminate()
    return store.saveSettings(input)
  })
  handle('agent:test', async (arg) => {
    if (active || testing) throw new Error('已有任务正在运行。')
    const input: SettingsInput = settingsSchema.parse(arg)
    if (!input.model) throw new Error('请填写模型名称。')
    testing = true
    const runId = randomUUID()
    let timer: ReturnType<typeof setTimeout> | undefined
    let testWorker: WorkerClient | undefined
    try {
      return await new Promise<{ ok: boolean; message: string }>((resolveResult) => {
        let finished = false
        const finish = (ok: boolean, message: string) => {
          if (finished) return
          finished = true
          clearTimeout(timer)
          resolveResult({ ok, message })
        }
        testWorker = makeWorker(input.pythonPath, (event) => {
          if (event.runId !== runId) return
          if (event.type === 'tested') finish(true, String(event.message))
          if (event.type === 'error') finish(false, String(event.message))
        }, () => finish(false, '连接测试进程已退出。'))
        timer = setTimeout(() => finish(false, '连接测试超时，请检查服务商地址和网络。'), 45000)
        void testWorker.boot().then(() => testWorker!.send({ command: 'test', runId,
          settings: requestSettings(input, store.credentials(input)) })).catch((error) => finish(false, error.message))
      })
    } finally {
      clearTimeout(timer)
      await testWorker?.terminate()
      testing = false
    }
  })
  handle('agent:start', async (arg) => {
    if (active || testing) throw new Error('请先结束当前任务。')
    const input = startSchema.parse(arg)
    const session = store.sessions.find((s) => s.id === input.sessionId)
    if (!session) throw new Error('对话不存在。')
    if (!input.demo && !store.settings.model) throw new Error('请先在设置中填写模型名称。')
    const run: Run = { id: randomUUID(), sessionId: session.id, task: input.task, createdAt: Date.now(),
      demo: !!input.demo, status: 'starting', events: [] }
    active = run
    stopRequested = false
    if (!session.runs.length) session.title = run.task.slice(0, 24)
    const context = session.runs.slice(-6).map((r) => `${r.task}\n结果: ${r.result?.status || r.status}`).join('\n\n')
    session.runs = [...session.runs, run].slice(-50)
    store.persist()
    publish({ type: 'run-created', run })
    hideForDesktop(run)
    try {
      if (worker && workerSession !== session.id) {
        const old = worker
        // Clearing the pointer prevents closing an idle old worker from ending the new run.
        worker = undefined
        await old.terminate()
      }
      if (!worker) {
        let owner: WorkerClient
        owner = makeWorker(store.settings.pythonPath, acceptEvent, () => {
          if (worker !== owner) return
          worker = undefined
          if (active) {
            if (stopRequested || quitting) acceptEvent({ type: 'result', runId: active.id,
              result: { status: 'user_abort', claimed_done: false, message: '执行进程已停止' } })
            else acceptEvent({ type: 'error', runId: active.id, message: '执行进程意外退出，请检查运行环境后重试。' })
          }
        })
        worker = owner
      }
      workerSession = session.id
      await worker.boot()
      if (active?.id !== run.id) return run
      worker.send({ command: 'run', runId: run.id, task: run.task, context, demo: run.demo,
        headless: process.env.GUI_AGENT_TEST_HEADLESS === '1', runsRoot: join(app.getPath('userData'), 'runs'),
        settings: requestSettings(store.settings, store.credentials()) })
    } catch (error) {
      acceptEvent({ type: 'error', runId: run.id, message: error instanceof Error ? error.message : '启动失败' })
      const old = worker
      worker = undefined
      await old?.terminate()
    }
    return session.runs.find((r) => r.id === run.id) || run
  })
  handle('agent:control', async (arg) => {
    const input = controlSchema.parse(arg)
    if (!active || active.id !== input.runId || !worker) throw new Error('这个任务已经结束。')
    if (input.action === 'stop') { await stop(); return }
    if (pending) throw new Error('请先处理当前确认请求，或者停止任务。')
    if (input.action === 'resume') hideForDesktop(active)
    worker.send({ command: input.action, runId: input.runId })
  })
  handle('agent:respond', (arg) => {
    const input = responseSchema.parse(arg)
    if (!active || active.id !== input.runId || !worker || pending?.requestId !== input.requestId) {
      throw new Error('这个确认请求已经失效。')
    }
    if (pending.kind === 'confirm' && typeof input.approved !== 'boolean') throw new Error('请选择允许或拒绝。')
    if (pending.kind === 'ask' && typeof input.answer !== 'string') throw new Error('请输入回答。')
    hideForDesktop(active)
    worker.send({ command: 'respond', ...input })
    pending = undefined
  })
  handle('agent:report', async (arg) => {
    const id = z.string().uuid().parse(arg)
    const path = join(app.getPath('userData'), 'runs', id, 'report.html')
    if (!path.startsWith(join(app.getPath('userData'), 'runs') + sep) || !existsSync(path)) throw new Error('报告尚未生成。')
    const error = await shell.openPath(path)
    if (error) throw new Error('无法打开报告，请检查默认浏览器。')
  })
}

function createWindow() {
  window = new BrowserWindow({ width: 1380, height: 900, minWidth: 1000, minHeight: 650,
    title: 'GUI Agent', backgroundColor: '#111315', autoHideMenuBar: true,
    icon: join(here, '../../assets/icon.png'),
    webPreferences: { preload: join(here, '../preload/index.cjs'), nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event) => event.preventDefault())
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  const url = process.env.ELECTRON_RENDERER_URL
  if (!app.isPackaged && url) void window.loadURL(url)
  else void window.loadFile(join(here, '../renderer/index.html'))
  window.on('closed', () => { window = null })
}

app.setName('GUI Agent')
if (process.env.GUI_AGENT_USER_DATA) app.setPath('userData', resolve(process.env.GUI_AGENT_USER_DATA))
if (!app.requestSingleInstanceLock()) app.quit()
else {
  app.on('second-instance', restoreWindow)
  void app.whenReady().then(() => {
    store = new Store(join(app.getPath('userData'), 'state.json'), {
      available: () => safeStorage.isEncryptionAvailable() &&
        (process.platform !== 'linux' || safeStorage.getSelectedStorageBackend() !== 'basic_text'),
      encrypt: (value) => safeStorage.encryptString(value), decrypt: (value) => safeStorage.decryptString(value),
    })
    shortcutAvailable = globalShortcut.register(STOP_SHORTCUT, () => { void stop() })
    installIPC()
    createWindow()
  })
  app.on('activate', () => { if (!window) createWindow() })
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
  app.on('before-quit', (event) => {
    globalShortcut.unregisterAll()
    if (quitting || !worker) return
    event.preventDefault()
    quitting = true
    void worker.terminate().finally(() => app.quit())
  })
}
