const { contextBridge } = require('electron')

contextBridge.exposeInMainWorld('collabNoteApi', {
  baseUrl: process.env.COLLABNOTE_API_URL || 'http://127.0.0.1:8765'
})