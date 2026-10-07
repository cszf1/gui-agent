import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { randomBytes } from 'node:crypto'

export default defineConfig(({ command }) => {
  const nonce = randomBytes(18).toString('base64')
  return {
  main: {
    plugins: [externalizeDepsPlugin()],
    build: { rollupOptions: { input: { index: resolve('electron/main.ts') } } },
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: { rollupOptions: {
      input: { index: resolve('electron/preload.ts') },
      output: { format: 'cjs', entryFileNames: '[name].cjs' },
    } },
  },
  renderer: {
    root: '.',
    html: command === 'serve' ? { cspNonce: nonce } : undefined,
    plugins: [react(), { name: 'desktop-development-csp', apply: 'serve',
      transformIndexHtml: { order: 'pre', handler: (html) => html.replace("script-src 'self'", `script-src 'self' 'nonce-${nonce}'`) },
    }],
    build: { rollupOptions: { input: resolve('index.html') } },
  },
  }
})
