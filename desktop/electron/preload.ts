import { contextBridge, ipcRenderer } from 'electron'
import type { DesktopAPI, DesktopEvent } from '../src/shared'

// Deliberately expose individual operations, never raw IPC or filesystem APIs.
const api: DesktopAPI = {
  load: () => ipcRenderer.invoke('agent:load'),
  newSession: () => ipcRenderer.invoke('agent:new-session'),
  saveSettings: (input) => ipcRenderer.invoke('agent:settings', input),
  testConnection: (input) => ipcRenderer.invoke('agent:test', input),
  start: (input) => ipcRenderer.invoke('agent:start', input),
  control: (input) => ipcRenderer.invoke('agent:control', input),
  respond: (input) => ipcRenderer.invoke('agent:respond', input),
  openReport: (id) => ipcRenderer.invoke('agent:report', id),
  subscribe: (callback) => {
    const listener = (_event: unknown, payload: DesktopEvent) => callback(payload)
    ipcRenderer.on('agent:event', listener)
    return () => ipcRenderer.removeListener('agent:event', listener)
  },
}
contextBridge.exposeInMainWorld('desktop', api)
