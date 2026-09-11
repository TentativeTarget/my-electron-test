const { app, BrowserWindow, ipcMain, safeStorage } = require('electron/main')
const { shell } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

// 多用戶測試：以 --profile=<名稱> 或 COLLABNOTE_PROFILE=<名稱> 啟動時，
// 每個設定檔使用獨立的 userData（各自的登入狀態與已存密碼），
// 因此同一台電腦可以同時開多個視窗，以不同帳號連線到同一個後端。
function resolveProfileName() {
  const fromArg = process.argv.find(arg => arg.startsWith('--profile='))
  const raw = fromArg ? fromArg.slice('--profile='.length) : process.env.COLLABNOTE_PROFILE
  // 只保留安全字元（含中文），避免路徑跳脫或非法檔名
  return String(raw || '').trim().replace(/[^A-Za-z0-9_\-\u4e00-\u9fff]+/g, '').slice(0, 32)
}

const profileName = resolveProfileName()
if (profileName) {
  app.setPath('userData', path.join(app.getPath('userData'), 'profiles', profileName))
}

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
      preload: path.join(__dirname, 'preload.js'),
      // 讓 preload 取得設定檔名稱（即使只用 --profile= 參數啟動）
      additionalArguments: profileName ? [`--collabnote-profile=${profileName}`] : []
    }
  })

  if (profileName) {
    // 同時開多個前端時，用視窗標題分辨各自身分
    const title = `CollabNote — ${profileName}`
    win.setTitle(title)
    win.on('page-title-updated', event => {
      event.preventDefault()
      win.setTitle(title)
    })
  }

  win.once('ready-to-show', () => {
    win.show()
  })

  win.loadFile('index.html')
}

app.whenReady().then(() => {
  ipcMain.handle('collabnote:open-external', async (_event, url) => {
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return false
    try {
      await shell.openExternal(url)
      return true
    } catch {
      return false
    }
  })

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
