import { afterEach, describe, expect, it } from 'vitest'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { Store, type Vault } from '../../electron/store'
import { defaults, type Run } from '../../src/shared'

const directories: string[] = []
function path() {
  const root = mkdtempSync(join(tmpdir(), 'gua-store-')); directories.push(root)
  return join(root, 'state.json')
}
const key = randomBytes(32)
const vault: Vault = {
  available: () => true,
  encrypt: (value) => {
    const iv = randomBytes(16); const cipher = createCipheriv('aes-256-cbc', key, iv)
    return Buffer.concat([iv, cipher.update(value, 'utf8'), cipher.final()])
  },
  decrypt: (value) => {
    const decipher = createDecipheriv('aes-256-cbc', key, value.subarray(0, 16))
    return Buffer.concat([decipher.update(value.subarray(16)), decipher.final()]).toString('utf8')
  },
}
afterEach(() => directories.splice(0).forEach((root) => rmSync(root, { recursive: true, force: true })))

describe('credential storage', () => {
  it('persists only encrypted credentials and never returns the key to the UI', () => {
    const file = path(); const store = new Store(file, vault)
    const publicValue = store.saveSettings({ ...defaults, apiKey: 'a-private-key-value', model: 'vision' })
    expect(publicValue.hasApiKey).toBe(true)
    expect(publicValue.keyPersisted).toBe(true)
    expect(JSON.stringify(publicValue)).not.toContain('a-private-key-value')
    expect(readFileSync(file, 'utf8')).not.toContain('a-private-key-value')
    expect(new Store(file, vault).credentials()).toBe('a-private-key-value')
  })
  it('keeps credentials in memory when a secure OS vault is unavailable', () => {
    const file = path(); const store = new Store(file, { ...vault, available: () => false })
    expect(store.saveSettings({ ...defaults, apiKey: 'a-private-key-value' }).keyPersisted).toBe(false)
    expect(store.credentials()).toBe('a-private-key-value')
    expect(readFileSync(file, 'utf8')).not.toContain('a-private-key-value')
    expect(new Store(file, vault).credentials()).toBe('')
  })
  it('retains the existing key on an empty edit and clears it only explicitly', () => {
    const store = new Store(path(), vault)
    store.saveSettings({ ...defaults, apiKey: 'original-key' })
    store.saveSettings({ ...defaults, apiKey: '' })
    expect(store.credentials()).toBe('original-key')
    store.saveSettings({ ...defaults, clearApiKey: true })
    expect(store.credentials()).toBe('')
  })
  it('does not forward a retained credential to a changed service address', () => {
    const store = new Store(path(), vault)
    store.saveSettings({ ...defaults, apiKey: 'original-key' })
    const changed = { ...defaults, baseUrl: 'https://another-provider.example/v1', apiKey: '' }
    expect(store.credentials(changed)).toBe('')
    expect(store.credentials({ ...changed, apiKey: 'new-service-key' })).toBe('new-service-key')
    store.saveSettings(changed)
    expect(store.credentials()).toBe('')
  })
  it('marks interrupted executions after restart without preserving image payloads', () => {
    const file = path(); const store = new Store(file, vault); const session = store.newSession()
    const run: Run = { id: 'r', sessionId: session.id, task: 'task', demo: false, createdAt: 1, status: 'running',
      events: [{ type: 'preview', runId: 'r', image: 'secret-frame' }, { type: 'log', runId: 'r', record: { kind: 'step' } }] }
    session.runs.push(run); store.persist()
    expect(readFileSync(file, 'utf8')).not.toContain('secret-frame')
    const restored = new Store(file, vault).sessions[0].runs[0]
    expect(restored.status).toBe('interrupted')
    expect(restored.events).toHaveLength(1)
  })
})
