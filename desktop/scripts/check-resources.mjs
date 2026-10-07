import { existsSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'

const target = process.argv[2] || process.platform
const binary = resolve('resources/gua-worker', target === 'win32' ? 'gua-worker.exe' : 'gua-worker')
const browsers = resolve('resources/browsers')
if (!existsSync(binary) || !existsSync(browsers) || !readdirSync(browsers).some((name) => name.startsWith('chromium-'))) {
  console.error('The bundled worker or Chromium is missing. Run npm run worker:build on the target OS first.')
  process.exit(1)
}
