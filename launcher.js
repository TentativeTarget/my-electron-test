const { spawn } = require('node:child_process')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')

const projectDir = __dirname
const pidFile = path.join(projectDir, '.collabnote-pids.json')
const targetNames = ['backend', 'frontend']

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

function removeStalePid(name, pids) {
  if (pids[name] && !isRunning(pids[name])) delete pids[name]
}

function commandFor(name) {
  if (name === 'backend') {
    return {
      command: process.platform === 'win32' ? 'python' : 'python3',
      args: [path.join(projectDir, 'server.py')],
      env: process.env
    }
  }

  return {
    command: require('electron'),
    args: [projectDir],
    env: process.env
  }
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
  removeStalePid(name, pids)
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
  for (const name of targetNames) {
    const pid = pids[name]
    console.log(`${name}: ${pid && isRunning(pid) ? `running (PID ${pid})` : 'stopped'}`)
  }
}

const action = process.argv[2] || 'start'
const target = process.argv[3] || 'all'
const names = target === 'all' ? targetNames : [target]

if (!names.every(name => targetNames.includes(name))) {
  console.error('target must be backend, frontend, or all')
  process.exit(1)
}

(async () => {
  if (action === 'start') {
    for (const name of names) await start(name)
  } else if (action === 'stop') {
    names.slice().reverse().forEach(stop)
  } else if (action === 'restart') {
    names.slice().reverse().forEach(stop)
    for (const name of names) await start(name)
  } else if (action === 'status') {
    status()
  } else {
    console.error('usage: node launcher.js [start|stop|restart|status] [backend|frontend|all]')
    process.exit(1)
  }
})()
