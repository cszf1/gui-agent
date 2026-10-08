import { expect, test } from 'vitest'
import { applyEvent, type Run } from '../../src/shared'

test('late events from a different task never alter the current task', () => {
  const run: Run = { id: 'current', sessionId: 'session', task: 'task', demo: false, createdAt: Date.now(), status: 'running', events: [] }
  expect(applyEvent(run, { type: 'result', runId: 'old', result: { status: 'done' } })).toBe(run)
})
test('a privacy-blocked result remains uncertain and previews never enter history', () => {
  const run: Run = { id: 'current', sessionId: 'session', task: 'task', demo: false, createdAt: Date.now(), status: 'running', events: [] }
  expect(applyEvent(run, { type: 'preview', runId: 'current', image: 'frame' }).events).toHaveLength(0)
  expect(applyEvent(run, { type: 'result', runId: 'current', result: { status: 'privacy_blocked' } }).status).toBe('uncertain')
})
