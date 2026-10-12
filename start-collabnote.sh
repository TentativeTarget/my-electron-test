#!/bin/bash
# CollabNote 一键启动：MySQL 检查 → Python 后端 → Electron 前端
#
# 用法：
#   ./start-collabnote.sh              使用环境变量里的数据库配置
#   COLLABNOTE_DB_PASSWORD=xxx ./start-collabnote.sh
#
# 需要先准备：
#   1. Node.js 与 npm
#   2. Python 3，并安装 requirements.txt
#   3. 一个可连接的 MySQL 8.x（见 README「数据库配置」）
set -e
cd "$(dirname "$0")"

echo "== 1/3 检查依赖 =="
command -v node >/dev/null || { echo "缺少 Node.js，请先安装"; exit 1; }
command -v npm >/dev/null || { echo "缺少 npm，请先安装"; exit 1; }
python3 -c "import pymysql, bleach, PIL" 2>/dev/null || {
  echo "缺少 Python 依赖，正在安装…"
  python3 -m pip install -r requirements.txt
}
[ -d node_modules ] || {
  echo "缺少 node_modules，正在安装…"
  npm install
}

echo "== 2/3 启动后端（MySQL 模式）=="
export COLLABNOTE_STORAGE="${COLLABNOTE_STORAGE:-mysql}"
node launcher.js start all

echo
echo "后端与前端已启动。查看状态：npm run launcher -- status all"
echo "停止：npm run launcher -- stop all"
