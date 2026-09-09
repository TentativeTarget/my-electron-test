const { contextBridge } = require('electron')

contextBridge.exposeInMainWorld('collabNoteApi', {
  baseUrl: 'http://127.0.0.1:8765'
})