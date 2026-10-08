import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests/e2e', workers: 1, timeout: 120000,
  expect: { timeout: 30000 }, reporter: 'list',
  use: { channel: process.platform === 'win32' ? 'msedge' : undefined },
})
