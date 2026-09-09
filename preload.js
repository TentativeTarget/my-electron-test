const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('collabNoteApi', {
  baseUrl: process.env.COLLABNOTE_API_URL || 'http://127.0.0.1:8765',
  openExternal: url => ipcRenderer.invoke('collabnote:open-external', url),
  credentials: {
    save: (username, password) => ipcRenderer.invoke('collabnote:credentials:save', username, password),
    load: () => ipcRenderer.invoke('collabnote:credentials:load'),
    remove: (username) => ipcRenderer.invoke('collabnote:credentials:remove', username)
  }
})
