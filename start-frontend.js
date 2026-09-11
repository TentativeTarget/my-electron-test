#!/usr/bin/env node
// 啟動 Electron 前端（npm run frontend）。
//
// 這裡自己 spawn Electron，而不是直接跑 `electron .`，原因有兩個：
//
// 1. 有些環境（例如從 VS Code 的終端機啟動）帶著 ELECTRON_RUN_AS_NODE=1，
//    那會讓 Electron 退化成純 Node 模式，載入 app 時直接以
//    "Cannot find module 'electron/main'" 失敗。
// 2. npm 安裝若中斷，node_modules/electron/dist 會缺 Electron.app，
//    啟動時只會得到 ENOENT；這裡偵測到就自動補回來。
const { spawn, spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const projectDir = __dirname

function electronDir() {
  return path.join(projectDir, 'node_modules', 'electron')
}

function electronBinary() {
  let relative
  try {
    relative = fs.readFileSync(path.join(electronDir(), 'path.txt'), 'utf8').trim()
  } catch {
    return null
  }
  return relative ? path.join(electronDir(), 'dist', relative) : null
}

// 回傳可用的 Electron 執行檔；安裝不完整時先跑一次 electron 的安裝腳本補回來
function ensureElectron() {
  const binary = electronBinary()
  if (binary && fs.existsSync(binary)) return binary
  const installer = path.join(electronDir(), 'install.js')
  if (!fs.existsSync(installer)) {
    console.error('找不到 node_modules/electron，請先執行：npm install')
    process.exit(1)
  }
  console.log('· Electron 執行檔不完整，正在還原…')
  spawnSync(process.execPath, [installer], { cwd: projectDir, stdio: 'inherit' })
  const restored = electronBinary()
  if (!restored || !fs.existsSync(restored)) {
    console.error('Electron 還原失敗，請執行 npm install 後再試。')
    process.exit(1)
  }
  return restored
}

// 啟動 GUI 前必須移除 ELECTRON_RUN_AS_NODE，否則 Electron 會以 Node 模式執行
function guiEnv(overrides) {
  const env = { ...process.env, ...overrides }
  delete env.ELECTRON_RUN_AS_NODE
  return env
}

function main() {
  const binary = ensureElectron()
  const child = spawn(binary, [projectDir, ...process.argv.slice(2)], {
    cwd: projectDir,
    env: guiEnv(),
    stdio: 'inherit'
  })
  child.on('exit', code => process.exit(code ?? 0))
  child.on('error', error => {
    console.error('無法啟動 Electron：' + error.message)
    process.exit(1)
  })
}

if (require.main === module) main()

module.exports = { ensureElectron, guiEnv, electronBinary, projectDir }
