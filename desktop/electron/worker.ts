import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import type { Settings } from '../src/shared'

export interface WorkerOptions {
  root: string; resources: string; packaged: boolean; cwd: string
  pythonPath: string; onEvent(event: Record<string, unknown>): void
  onExit(): void
}

export function engineCommand(options: Pick<WorkerOptions, 'root' | 'resources' | 'packaged' | 'pythonPath'>) {
  const binary = join(options.resources, 'worker', 'gua-worker', process.platform === 'win32' ? 'gua-worker.exe' : 'gua-worker')
  if (options.packaged && existsSync(binary)) return { command: binary, args: [], bundled: true }
  if (options.packaged) throw new Error('安装包缺少执行引擎，请重新安装完整的 GUI Agent 安装包。')
  const venv = join(options.root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
  const command = options.pythonPath || process.env.GUI_AGENT_PYTHON || (existsSync(venv) ? venv : 'python')
  return { command, args: ['-u', '-m', 'gua.desktop'], bundled: false }
}

export class WorkerClient {
  private child?: ChildProcessWithoutNullStreams
  private readyPromise?: Promise<void>
  private alive = false
  constructor(private options: WorkerOptions) {}

  async boot() {
    if (this.readyPromise) return this.readyPromise
    this.readyPromise = new Promise<void>((resolve, reject) => {
      const engine = engineCommand(this.options)
      const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
      if (engine.bundled) env.PLAYWRIGHT_BROWSERS_PATH = join(this.options.resources, 'worker', 'browsers')
      const child = this.child = spawn(engine.command, engine.args, {
        cwd: this.options.cwd, env, shell: false, windowsHide: true, detached: process.platform !== 'win32',
        stdio: ['pipe', 'pipe', 'pipe'],
      })
      this.alive = true
      const timeout = setTimeout(() => {
        reject(new Error('执行引擎启动超时，请检查 Python 路径和依赖。'))
        void this.terminate()
      }, 20000)
      const lines = createInterface({ input: child.stdout })
      let ready = false
      lines.on('line', (line) => {
        if (line.length > 8_000_000) return
        try {
          const event = JSON.parse(line)
          if (typeof event !== 'object' || event === null || typeof event.type !== 'string') return
          if (event.type === 'ready' && !ready) {
            ready = true
            clearTimeout(timeout)
            resolve()
          }
          this.options.onEvent(event)
        } catch { /* stdout is private JSON-lines; discard non-protocol output */ }
      })
      // Drain, but never forward process diagnostics containing response bodies or secrets.
      child.stderr.on('data', () => {})
      child.stdin.on('error', () => {})
      child.on('error', () => {
        clearTimeout(timeout)
        reject(new Error('无法启动执行引擎，请在设置中指定已安装依赖的 Python 可执行文件。'))
      })
      child.once('close', () => {
        clearTimeout(timeout)
        this.alive = false
        if (!ready) reject(new Error('执行引擎未能启动，请检查 Python 路径和依赖。'))
        this.options.onExit()
      })
    })
    return this.readyPromise
  }

  send(message: Record<string, unknown>) {
    if (!this.child || !this.alive || !this.child.stdin.writable) throw new Error('执行引擎已断开，请重新运行任务。')
    this.child.stdin.write(JSON.stringify(message) + '\n')
  }

  async terminate() {
    const child = this.child
    if (!child || !this.alive) return
    const closed = new Promise<void>((resolve) => child.once('close', () => resolve()))
    try { this.send({ command: 'shutdown' }) } catch { /* already closing */ }
    await Promise.race([closed, new Promise<void>((resolve) => setTimeout(resolve, 600))])
    if (!this.alive || !child.pid) return
    if (process.platform === 'win32') {
      await new Promise<void>((resolve) => {
        const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { shell: false, windowsHide: true })
        killer.once('close', () => resolve())
        killer.once('error', () => { child.kill(); resolve() })
      })
    } else {
      try { process.kill(-child.pid, 'SIGKILL') } catch { child.kill('SIGKILL') }
    }
    await Promise.race([closed, new Promise<void>((resolve) => setTimeout(resolve, 1000))])
  }
}

export function requestSettings(settings: Settings, apiKey: string) {
  const { pythonPath: _path, ...safe } = settings
  return { ...safe, apiKey }
}
