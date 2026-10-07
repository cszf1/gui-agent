import { z } from 'zod'
import { defaults } from '../src/shared'

export const settingsSchema = z.object({
  provider: z.enum(['openai', 'anthropic']),
  baseUrl: z.string().trim().max(2000).refine((value) => {
    try {
      const url = new URL(value)
      return ['http:', 'https:'].includes(url.protocol) && !!url.hostname &&
        !url.username && !url.password && !url.search && !url.hash
    } catch { return false }
  }, 'Base URL 应为完整的 HTTP(S) API 地址，不能包含凭据或查询参数'),
  model: z.string().trim().max(200),
  target: z.enum(['desktop', 'browser']),
  startUrl: z.string().trim().max(2000),
  taskWindow: z.string().trim().max(300),
  safetyMode: z.enum(['confirm', 'deny']),
  maxSteps: z.number().int().min(1).max(200),
  pythonPath: z.string().trim().max(2000).refine((v) => !/[\r\n\0]/.test(v)),
  apiKey: z.string().max(8192).optional(),
  clearApiKey: z.boolean().optional(),
}).strict()

export const startSchema = z.object({
  sessionId: z.string().uuid(), task: z.string().trim().min(1).max(20000), demo: z.boolean().optional(),
}).strict()
export const controlSchema = z.object({
  runId: z.string().uuid(), action: z.enum(['pause', 'resume', 'stop']),
}).strict()
export const responseSchema = z.object({
  runId: z.string().uuid(), requestId: z.string().regex(/^[a-f0-9]{32}$/),
  approved: z.boolean().optional(), answer: z.string().max(10000).optional(),
}).strict()

export function restoreSettings(value: unknown) {
  const parsed = settingsSchema.safeParse({ ...defaults, ...(typeof value === 'object' && value ? value : {}) })
  if (!parsed.success) return { ...defaults }
  const { apiKey: _key, clearApiKey: _clear, ...settings } = parsed.data
  return settings
}
