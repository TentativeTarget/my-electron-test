const { spawn } = require('node:child_process')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')

const projectDir = __dirname
const pidFile = path.join(projectDir, '.collabnote-pids.json')
const APPS = ['backend', 'frontend']

// 每個前端實例在 PID 檔中以 `frontend:<設定檔>` 區分；
// 不帶設定檔的預設實例仍使用 `frontend`，可與具名實例同時運行。
function profileFromEnv() {
  return String(process.env.COLLABNOTE_PROFILE || '').trim()
}

function parseTarget(raw) {
  const value = String(raw || '').trim()
  if (!value) return null
  if (value === 'all') return 'all'
  const separator = value.indexOf(':')
  const app = separator === -1 ? value : value.slice(0, separator)
  const profile = separator === -1 ? '' : value.slice(separator + 1).trim()
  if (!APPS.includes(app)) return null
  if (profile && app !== 'frontend') return null
  if (app === 'backend') return 'backend'
  const name = profile || profileFromEnv()
  // frontend:default 代表沿用原本的預設 userData（不建立 profiles/<名稱>）
  if (name === 'default') return 'frontend:default'
  return name ? `frontend:${name}` : 'frontend'
}

// 每次 start frontend 都開一個新客戶端，自動分配沒被占用的設定檔編號，
// 讓多個視窗各自擁有獨立的登入狀態。
function nextClientKey(pids) {
  const used = new Set()
  for (const key of Object.keys(pids)) {
    const matched = /^frontend:client-(\d+)$/.exec(key)
    if (matched) used.add(Number(matched[1]))
  }
  let index = 1
  while (used.has(index)) index += 1
  return `frontend:client-${index}`
}

function readPids() {
  try {
    return JSON.parse(fs.readFileSync(pidFile, 'utf8'))
  } catch {
    return {}
  }
}

function writePids(pids) {
  fs.writeFileSync(pidFile, `${JSON.stringify(pids, null, 2)}\n`)
}

function isRunning(pid) {
  if (!pid) return false
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

// 清除已結束的行程記錄，讓自動編號可以重複使用空出來的設定檔
function prunePids(pids) {
  let changed = false
  for (const [key, pid] of Object.entries(pids)) {
    if (!isRunning(pid)) {
      delete pids[key]
      changed = true
    }
  }
  return changed
}

function frontendKeys(pids) {
  const keys = Object.keys(pids).filter(name => name === 'frontend' || name.startsWith('frontend:'))
  return keys.sort((a, b) => a.localeCompare(b))
}

// 決定一個指令要作用在哪些實例上：
//   start  → 只啟動指定實例（frontend 代表預設實例）
//   stop   → frontend 代表「所有前端實例」，避免漏掉具名實例
function keysForAction(raw, action, pids) {
  const target = parseTarget(raw)
  if (!target) return null
  const stopping = action === 'stop' || action === 'restart'
  if (target === 'all') {
    const fronts = frontendKeys(pids)
    return stopping ? ['backend', ...fronts] : ['backend', nextClientKey(pids)]
  }
  if (target === 'frontend:default') return ['frontend']
  if (target === 'frontend') {
    // start：每次執行都開一個新的客戶端實例
    // stop ：關閉所有前端實例（含具名設定檔）
    if (!stopping) return [nextClientKey(pids)]
    const fronts = frontendKeys(pids)
    return fronts.length ? fronts : ['frontend']
  }
  return [target]
}

function commandFor(name) {
  if (name === 'backend') {
    return {
      command: process.platform === 'win32' ? 'python' : 'python3',
      args: [path.join(projectDir, 'server.py')],
      env: { ...process.env }
    }
  }

  let profile = name.startsWith('frontend:') ? name.slice('frontend:'.length) : ''
  if (profile === 'default') profile = ''
  const args = [projectDir]
  const env = { ...process.env }
  if (profile) {
    // 同時用參數與環境變數傳遞，讓 main.js 兩種啟動方式都能識別
    args.push(`--profile=${profile}`)
    env.COLLABNOTE_PROFILE = profile
  }
  return { command: require('electron'), args, env }
}

function waitForBackend(timeout = 5000) {
  // 後端預設綁定 0.0.0.0（所有網卡），但健康檢查要連 127.0.0.1 才保證可達
  const configuredHost = process.env.COLLABNOTE_HOST || '0.0.0.0'
  const host = configuredHost === '0.0.0.0' || configuredHost === '::' ? '127.0.0.1' : configuredHost
  const port = Number(process.env.COLLABNOTE_PORT || 8765)
  const startedAt = Date.now()

  return new Promise(resolve => {
    const check = () => {
      const request = http.get({ host, port, path: '/api/notes' }, response => {
        response.resume()
        resolve(true)
      })
      request.on('error', () => {
        if (Date.now() - startedAt >= timeout) {
          resolve(false)
        } else {
          setTimeout(check, 100)
        }
      })
    }
    check()
  })
}

async function start(name) {
  const pids = readPids()
  if (prunePids(pids)) writePids(pids)
  if (pids[name]) {
    console.log(`${name} already running (PID ${pids[name]})`)
    return
  }

  const target = commandFor(name)
  const child = spawn(target.command, target.args, {
    cwd: projectDir,
    detached: true,
    env: target.env,
    stdio: 'ignore'
  })
  child.unref()
  pids[name] = child.pid
  writePids(pids)
  if (name === 'backend') {
    const ready = await waitForBackend()
    if (!ready) {
      console.error('backend process started but API did not become ready')
      return
    }
  }
  console.log(`started ${name} (PID ${child.pid})`)
  if (name.startsWith('frontend:')) {
    const profile = name.slice('frontend:'.length)
    console.log(`  ↳ 設定檔 ${profile}：獨立登入狀態，可在這個視窗登入不同帳號`)
  }
}

function stop(name) {
  const pids = readPids()
  const pid = pids[name]
  if (!pid || !isRunning(pid)) {
    delete pids[name]
    writePids(pids)
    console.log(`${name} is not running`)
    return
  }

  try {
    if (process.platform === 'win32') {
      spawn('taskkill', ['/pid', String(pid), '/t', '/f'], { stdio: 'ignore' })
    } else {
      process.kill(-pid, 'SIGTERM')
    }
  } catch (error) {
    console.error(`failed to stop ${name}: ${error.message}`)
  }
  delete pids[name]
  writePids(pids)
  console.log(`stopped ${name} (PID ${pid})`)
}

function status() {
  const pids = readPids()
  const backend = pids.backend
  console.log(`backend: ${backend && isRunning(backend) ? `running (PID ${backend})` : 'stopped'}`)
  const fronts = frontendKeys(pids).filter(key => key !== 'frontend')
  const defaultPid = pids.frontend
  const rows = [
    ['frontend', defaultPid],
    ...fronts.map(key => [key, pids[key]])
  ]
  if (!rows.length) rows.push(['frontend', undefined])
  for (const [key, pid] of rows) {
    const label = key === 'frontend' ? 'frontend (預設)' : key
    console.log(`${label}: ${pid && isRunning(pid) ? `running (PID ${pid})` : 'stopped'}`)
  }
}

const USAGE = `usage:
  node launcher.js start  [backend|frontend|frontend:<設定檔>|all] ...
  node launcher.js stop   [backend|frontend|frontend:<設定檔>|all] ...
  node launcher.js restart [backend|frontend|frontend:<設定檔>|all] ...
  node launcher.js status

每次 start frontend 都會開啟一個新的客戶端（自動分配 client-1、client-2 …）：
  npm run launcher -- start frontend        # 第 1 個客戶端
  npm run launcher -- start frontend        # 第 2 個客戶端
  npm run launcher -- start all             # 後端 + 一個客戶端

指定設定檔名稱（同一個名稱會沿用先前的登入狀態）：
  npm run launcher -- start frontend:alice
使用預設設定檔（不安裝在 profiles 目錄下）：
  npm run launcher -- start frontend:default

停止：
  npm run launcher -- stop frontend         # 關閉所有前端實例
  npm run launcher -- stop frontend:client-2`

async function main(argv) {
  const action = argv[2] || 'start'
  const raws = argv.slice(3)
  if (action === 'status') {
    status()
    return
  }
  if (!['start', 'stop', 'restart'].includes(action)) {
    console.error(USAGE)
    process.exitCode = 1
    return
  }

  const requested = raws.length ? raws : ['all']
  const pids = readPids()
  if (prunePids(pids)) writePids(pids)
  const names = []
  for (const raw of requested) {
    const keys = keysForAction(raw, action, pids)
    if (!keys) {
      console.error(`target must be backend, frontend, frontend:<profile>, or all (got "${raw}")`)
      process.exitCode = 1
      return
    }
    for (const key of keys) if (!names.includes(key)) names.push(key)
  }

  if (action === 'start') {
    for (const name of names) await start(name)
  } else if (action === 'stop') {
    names.slice().reverse().forEach(stop)
  } else {
    names.slice().reverse().forEach(stop)
    for (const name of names) await start(name)
  }
}

if (require.main === module) {
  main(process.argv).catch(error => {
    console.error(error.message)
    process.exitCode = 1
  })
}

module.exports = { parseTarget, keysForAction, commandFor, frontendKeys, nextClientKey, prunePids, start, stop, status, main, pidFile }
