import { expect, it } from 'vitest'
import { controlSchema, responseSchema, settingsSchema } from '../../electron/validation'
import { defaults, applyEvent, type Run } from '../../src/shared'

it.each(['file:///tmp/credentials', 'https://key:secret@provider.test/v1', 'https://provider.test/v1?key=secret', 'https://provider.test/v1#token'])('rejects an unsafe API address: %s', (baseUrl) => {
  expect(settingsSchema.safeParse({ ...defaults, baseUrl }).success).toBe(false)
})
it('accepts a local OpenAI-compatible server without an API key', () => {
  expect(settingsSchema.safeParse({ ...defaults, baseUrl: 'http://127.0.0.1:11434/v1', model: 'vision-model' }).success).toBe(true)
})
it('rejects arbitrary IPC controls and malformed response IDs', () => {
  expect(controlSchema.safeParse({ runId: 'anything', action: 'shell' }).success).toBe(false)
  expect(responseSchema.safeParse({ runId: 'anything', requestId: '../../secrets', approved: true }).success).toBe(false)
})
it('preserves an uncertain result and excludes previews from persistent events', () => {
  const run: Run = { id: 'run', sessionId: 'chat', task: 'task', demo: false, createdAt: 1, status: 'running', events: [] }
  expect(applyEvent(run, { type: 'preview', runId: 'run', image: 'private' }).events).toEqual([])
  expect(applyEvent(run, { type: 'result', runId: 'run', result: { status: 'uncertain' } }).status).toBe('uncertain')
  expect(applyEvent(run, { type: 'result', runId: 'another-run', result: { status: 'done' } })).toBe(run)
})
