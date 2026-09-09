const { app, BrowserWindow, ipcMain, safeStorage } = require('electron/main')
const fs = require('node:fs')
const path = require('node:path')

function credentialsFile() {
  return path.join(app.getPath('userData'), 'collabnote-credentials.json')
}

function readCredentialStore() {
  try {
    return JSON.parse(fs.readFileSync(credentialsFile(), 'utf8'))
  } catch {
    return {}
  }
}

function writeCredentialStore(store) {
  fs.mkdirSync(path.dirname(credentialsFile()), { recursive: true })
  fs.writeFileSync(credentialsFile(), JSON.stringify(store, null, 2), { mode: 0o600 })
}

function decryptStoredPassword(encoded) {
  try {
    return safeStorage.decryptString(Buffer.from(encoded, 'base64'))
  } catch {
    return null
  }
}

const createWindow = () => {
  const win = new BrowserWindow({
    width: 1280,
    height: 720,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js')
    }
  })

  win.once('ready-to-show', () => {
    win.show()
  })

  win.loadFile('index.html')
}

app.whenReady().then(() => {
  ipcMain.handle('collabnote:credentials:load', () => {
    if (!safeStorage.isEncryptionAvailable()) return {}
    const store = readCredentialStore()
    const result = {}
    for (const [username, encoded] of Object.entries(store)) {
      const password = decryptStoredPassword(encoded)
      if (password !== null) result[username] = password
    }
    return result
  })

  ipcMain.handle('collabnote:credentials:save', (_event, username, password) => {
    if (!safeStorage.isEncryptionAvailable()) return false
    if (typeof username !== 'string' || typeof password !== 'string') return false
    const name = username.trim()
    if (!name || !password) return false
    const store = readCredentialStore()
    store[name] = safeStorage.encryptString(password).toString('base64')
    writeCredentialStore(store)
    return true
  })

  ipcMain.handle('collabnote:credentials:remove', (_event, username) => {
    const name = typeof username === 'string' ? username.trim() : ''
    const store = readCredentialStore()
    if (name && store[name]) {
      delete store[name]
      writeCredentialStore(store)
    }
    return true
  })

  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    }
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
