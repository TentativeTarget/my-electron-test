const { app, BrowserWindow } = require('electron/main')
const { spawn } = require('node:child_process')
const path = require('node:path')

let pythonServer

const startPythonServer = () => {
  const pythonCommand = process.platform === 'win32' ? 'python' : 'python3'
  pythonServer = spawn(pythonCommand, [path.join(__dirname, 'server.py'), '8765'], {
    stdio: ['ignore', 'pipe', 'pipe']
  })

  pythonServer.stderr.on('data', data => {
    console.error(`[Python] ${data}`)
  })
}

const createWindow = () => {
  const win = new BrowserWindow({
    width: 800,
    height: 600,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js')
    }
  })

  win.loadFile('index.html')
}

app.whenReady().then(() => {
  startPythonServer()
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow()
    }
  })
})

app.on('window-all-closed', () => {
  pythonServer?.kill()
  if (process.platform !== 'darwin') {
    app.quit()
  }
})