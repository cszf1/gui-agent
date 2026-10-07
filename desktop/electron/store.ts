import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { randomUUID } from 'node:crypto'
import { activeStatuses, defaults, sameService, type PublicSettings, type Session, type Settings, type SettingsInput } from '../src/shared'
import { restoreSettings, settingsSchema } from './validation'

export interface Vault {
  available(): boolean
  encrypt(value: string): Buffer
  decrypt(value: Buffer): string
}

export class Store {
  settings: Settings = { ...defaults }
  sessions: Session[] = []
  private apiKey = ''
  private encryptedKey: string | undefined
  constructor(private file: string, private vault: Vault) {
    if (!existsSync(file)) return
    try {
      const value = JSON.parse(readFileSync(file, 'utf8'))
      this.settings = restoreSettings(value.settings)
      this.encryptedKey = typeof value.encryptedKey === 'string' ? value.encryptedKey : undefined
      if (this.encryptedKey && vault.available()) {
        try { this.apiKey = vault.decrypt(Buffer.from(this.encryptedKey, 'base64')) } catch { this.apiKey = '' }
      }
      // Stored history is local data, but tolerate incomplete writes or old schemas.
      if (Array.isArray(value.sessions)) {
        this.sessions = value.sessions.filter((s: Session) => typeof s.id === 'string' && Array.isArray(s.runs)).slice(-50)
        for (const session of this.sessions) {
          session.runs = session.runs.filter((r) => typeof r.id === 'string' && Array.isArray(r.events)).slice(-50)
          for (const run of session.runs) {
            if (activeStatuses.has(run.status)) run.status = 'interrupted'
            run.events = run.events.filter((e) => e.type !== 'preview').slice(-300)
          }
        }
      }
    } catch { /* Keep the original file intact; the UI can still configure a fresh app. */ }
  }

  publicSettings(): PublicSettings {
    return { ...this.settings, hasApiKey: !!this.apiKey, keyPersisted: !!this.apiKey && !!this.encryptedKey }
  }

  credentials(input?: SettingsInput): string {
    if (input?.clearApiKey) return ''
    if (input?.apiKey?.trim()) return input.apiKey.trim()
    return input && !sameService(input, this.settings) ? '' : this.apiKey
  }

  saveSettings(input: SettingsInput) {
    const { apiKey, clearApiKey, ...settings } = settingsSchema.parse(input)
    const nextKey = this.credentials({ ...settings, apiKey, clearApiKey })
    const encrypted = nextKey && this.vault.available() ? this.vault.encrypt(nextKey).toString('base64') : undefined
    const old = { settings: this.settings, apiKey: this.apiKey, encryptedKey: this.encryptedKey }
    this.settings = settings
    this.apiKey = nextKey
    this.encryptedKey = encrypted
    try { this.persist() } catch (error) {
      Object.assign(this, old)
      throw error
    }
    return this.publicSettings()
  }

  newSession(): Session {
    const session: Session = { id: randomUUID(), title: '新对话', createdAt: Date.now(), runs: [] }
    this.sessions = [...this.sessions, session].slice(-50)
    this.persist()
    return session
  }

  persist() {
    mkdirSync(dirname(this.file), { recursive: true })
    const tmp = this.file + '.tmp'
    const sessions = this.sessions.map((s) => ({ ...s, runs: s.runs.map((r) => ({ ...r,
      events: r.events.filter((e) => e.type !== 'preview'),
    })) }))
    writeFileSync(tmp, JSON.stringify({ version: 1, settings: this.settings, encryptedKey: this.encryptedKey,
      sessions }, null, 2), { mode: 0o600 })
    renameSync(tmp, this.file)
  }
}
