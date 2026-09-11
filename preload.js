const { contextBridge, ipcRenderer } = require('electron')

// 由環境變數指定的伺服器地址優先於使用者在介面上填寫的地址
const envBaseUrl = (process.env.COLLABNOTE_API_URL || '').trim()

contextBridge.exposeInMainWorld('collabNoteApi', {
  baseUrl: envBaseUrl || 'http://127.0.0.1:8765',
  hasEnvBaseUrl: Boolean(envBaseUrl),
  profileName: (process.env.COLLABNOTE_PROFILE || '').trim(),
  openExternal: url => ipcRenderer.invoke('collabnote:open-external', url),
  credentials: {
    save: (username, password) => ipcRenderer.invoke('collabnote:credentials:save', username, password),
    load: () => ipcRenderer.invoke('collabnote:credentials:load'),
    remove: (username) => ipcRenderer.invoke('collabnote:credentials:remove', username)
  }
})
