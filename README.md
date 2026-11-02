# CollabNote

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="assets/banner-light.png">
    <img src="assets/banner-light.png" alt="CollabNote — 字符雨横幅" width="100%">
  </picture>
</p>

基于 Electron + Python 的多人共享笔记，带好友协作和记账本。

前端只负责显示，所有数据通过 HTTP API 存在后端：默认 MySQL，也可回落到 JSON 文件。

**最短启动**（先配好数据库，见[数据库](#数据库mysql)）

```bash
npm install
python3 -m pip install -r requirements.txt
./start-collabnote.sh
```

<sub>[功能](#功能) · [快速开始](#快速开始) · [数据库](#数据库mysql) · [多人使用](#多人使用) · [监控器](#后端监控器) · [打包](#打包) · [项目结构](#项目结构) · [API](#api) · [安全边界](#安全边界)</sub>

---

## 功能

### 账号

- 注册、登录、登出，Bearer Token 会话；用户名 3–32 位，密码至少 6 位。
- 密码以 PBKDF2-SHA256 加随机盐保存。
- 记住最近登录的用户；勾选后密码经系统密码库（macOS 钥匙串 / Windows DPAPI）加密保存。

### 笔记

- 新建、编辑、自动保存、搜索、删除（删除前确认）。
- 富文本：粗体、斜体、下划线、删除线、项目/编号列表、引用块、表格。
- 字号 12–40px，选中即预览，随光标回显。
- 超链接自动补 `https://`，点击用系统浏览器打开（仅 http/https）。
- 图片本地插入，后端存 `data/images/`；点选后可等比缩放，Delete 删除、Esc 取消。
- 笔记列表右键菜单可删除自己建立的笔记（没有权限的笔记不弹菜单）。
- 每篇笔记本会话内保留 20 个版本快照，可复原（重启清空）。
- 顶部实时显示同步状态：已同步 / 保存中… / 已断开。

### 协作

- 每篇笔记有建立者与协作者，非协作者看不到这篇笔记的任何痕迹。
- 权限分「可编辑」「只读」，只读者正文与工具栏都锁住；删除只有建立者能做。
- 右上角「共用」可按登录名或好友快选邀请、切换权限、移除。
- 同一篇笔记的在线者显示在标题旁头像堆叠和底部信息栏，区分编辑中/檢視中。
- SSE 实时推送：别人增删改会立刻出现；你正在编辑时不会被覆盖，只提示「其他人已更新」。

### 好友

- 申请、接受、拒绝，双向好友关系。
- 在线状态由前端每 15 秒拉取一次。
- 好友栏停靠右侧、默认收起；收起时只留一个贴边的圆形按钮，点开滑出。

### 金融账本

- 「笔记 / 金融」双工作区切换。
- 账本可新建、改名、设预算；删除为软删除。
- 成员按 `owner` / `admin` / `editor` / `viewer` 四档管理，拥有者与管理员可改角色或移除。
- 记账：左侧填内容、右侧填金额，点「加钱」记收入、「扣钱」记支出，可带类别。
- 报表：KPI、收支趋势、分类占比、最近交易、AA 结算、本月预算；周期可切今天/本月/今年。
- 删除交易会留快照，可从历史里还原。
- 账本变更通过 `ledger-changed` 事件推给同一账本的在线成员。
- 存储：MySQL 用 `ledgers` / `ledger_members` / `transactions` / `ledger_history` 四张表；JSON 模式用 `ledgers.json`。

### 界面

- 深色/浅色主题，登录页与工作区都能切，偏好存本地。
- 昵称、头衔、JPG/PNG 头像（后端裁成 256×256 JPEG）。
- 无系统标题栏（macOS 保留红绿灯），窗口可拖动，最小 1024×660。
- 左侧栏宽度可拖 220px 至窗口 60%，松手记住，方向键可微调。
- 后端连不上时弹「连接失败」，可改服务器地址或重连。

[↑ 回到顶部](#collabnote)

---

## 快速开始

需要 Node.js（含 npm）、Python 3，以及一个能连上的 MySQL 8.x。

```bash
npm install
python3 -m pip install -r requirements.txt
```

配置好数据库（见下一节）后：

```bash
./start-collabnote.sh              # 检查依赖 → 后端 → 前端
```

也可以分开：

```bash
npm run backend                    # 只起 Python 后端
npm run frontend                   # 只起 Electron 客户端
npm run launcher -- start all      # 后端 + 一个客户端
npm run launcher -- status all     # 看状态
npm run launcher -- stop all       # 全停
```

后端启动时会自动开一个终端窗口跑监控器，不想开就加 `COLLABNOTE_MONITOR_WINDOW=0`。

### 前端起不来时

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `Cannot find module 'electron/main'` | 终端带着 `ELECTRON_RUN_AS_NODE=1`（常见于 VS Code 终端） | `npm run frontend` 会自动移除；手动跑用 `env -u ELECTRON_RUN_AS_NODE npm run frontend` |
| `spawn ... Electron ENOENT` | `node_modules/electron/dist` 不完整 | 脚本会自动重装；也可手动 `node node_modules/electron/install.js` |

[↑ 回到顶部](#collabnote)

---

## 数据库（MySQL）

默认存储是 MySQL，密码只从环境变量读，别写进仓库。项目**不会**自动读 `.env`，要自己 `export`。

### 建库建号

```sql
CREATE DATABASE collabnote CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'collabnote'@'localhost' IDENTIFIED BY '请换成强密码';
GRANT ALL PRIVILEGES ON collabnote.* TO 'collabnote'@'localhost';
FLUSH PRIVILEGES;
```

### 环境变量

```bash
export COLLABNOTE_STORAGE=mysql
export COLLABNOTE_DB_HOST=127.0.0.1
export COLLABNOTE_DB_PORT=3306
export COLLABNOTE_DB_NAME=collabnote
export COLLABNOTE_DB_USER=collabnote
export COLLABNOTE_DB_PASSWORD='你的数据库密码'
```

首次启动会自动执行 `schema.sql` 建表。

### 不用 MySQL（JSON 模式）

```bash
COLLABNOTE_STORAGE=json npm run backend
```

数据落在项目目录的 `users.json`、`notes.json`、`ledgers.json` 和 `data/`，已被 `.gitignore` 挡住。换机器时整个文件夹拷走即可。

### 从 JSON 迁到 MySQL

先备份，再预览，最后正式导：

```bash
python3 migrate_json_to_mysql.py --dry-run
python3 migrate_json_to_mysql.py
```

会一并搬账号、笔记、成员、交易与历史，原 JSON 文件不动。迁移不支持反向（MySQL → JSON）。

[↑ 回到顶部](#collabnote)

---

## 多人使用

### 局域网共用一台后端

```bash
npm run backend      # 默认监听 0.0.0.0，会打印局域网地址
```

其他人登录时在「服务器地址」填 `http://192.168.1.100:8765`。留空则连本机。也可以用 `COLLABNOTE_API_URL` 固定地址，此时登录页地址栏只读。

- 只想本机用：`COLLABNOTE_HOST=127.0.0.1 npm run backend`
- 换端口：`COLLABNOTE_PORT=8766 npm run backend`
- 系统防火墙问起要选「允许」，不然外部连不进来

### 同一台电脑开多个客户端

```bash
npm run launcher -- start frontend               # client-1
npm run launcher -- start frontend               # client-2
npm run launcher -- start frontend:alice         # 指定名字，沿用该名字的登录状态
npm run launcher -- start frontend:default       # 原本的默认配置
```

每个名字对应独立的 `userData/profiles/<名字>`，登录状态、记住的密码、主题互不干扰，但都连同一个后端。设定档名只保留中英文、数字、`-`、`_`。

```bash
npm run launcher -- status
npm run launcher -- stop frontend                # 关掉所有前端
npm run launcher -- stop frontend:client-2       # 只关一个
```

[↑ 回到顶部](#collabnote)

---

## 后端监控器

`monitor.py` 只用标准库，左边看谁在线、在看哪篇笔记，右边滚服务器日志。

```bash
npm run monitor
python3 monitor.py --port 9000
python3 monitor.py --url http://192.168.1.50:8765
python3 monitor.py --once                        # 输出一帧就退出
python3 monitor.py --exit-when-offline 4         # 失联 4 秒自退
```

按键：`q` 退出、`p` 暂停跟随、`c` 清屏、`a` 显示/隐藏静态请求、方向键与 PgUp/PgDn 翻历史。

日志等级 `INFO` / `HTTP` / `EVENT` / `LIVE` / `WARN` / `STATIC`，缓冲最近 500 行，后端重启清空。两个监控接口默认只接受本机回环；远端查看要先设 `COLLABNOTE_MONITOR_TOKEN`，监控器带上 `--token`。

日志默认同时打印到终端，`COLLABNOTE_LOG_STDOUT=0` 可关。

[↑ 回到顶部](#collabnote)

---

## 打包

只打包前端；后端要另外部署，客户端通过登录页或 `COLLABNOTE_API_URL` 连过去。

```bash
npm run dist:mac     # dist/CollabNote-0.2.0_Beta-mac-x64.dmg
npm run dist:win     # dist/CollabNote-0.2.0_Beta-win-x64.exe（NSIS）
npm run pack         # 只出未打包目录，方便试跑
```

打包内容由 `package.json` 的 `build.files` 决定，只含 `index.html`、`main.js`、`preload.js`、`frontend/vendor/**`，本机数据一律不进包。

### 镜像别删

`package.json` 的 `config` 里写死了两个镜像，国内网络没它会卡在 GitHub：

```json
"config": {
  "electron_mirror": "https://registry.npmmirror.com/-/binary/electron/",
  "electron_builder_binaries_mirror": "https://registry.npmmirror.com/-/binary/electron-builder-binaries/"
}
```

必须走 `npm run dist:win` 这类 npm 脚本，直接 `npx electron-builder` 拿不到这两个变量。首次打包会下约 100 MB 工具链，缓存在 `~/Library/Caches/`。

### 免安装后端

对方不想装 Python 时用这个：

```bash
npm run dist:backend                          # 按当前系统打包
python3 build-backend.py --platform win       # 任何系统都能组 Windows 版
python3 build-backend.py --out /tmp/backend   # 指定输出目录
```

- macOS：`dist/backend/CollabNote-Backend-macOS-x86_64/`，双击 `启动后端.command`（Intel；Apple Silicon 走 Rosetta 2）。
- Windows：`dist/backend/CollabNote-Backend-Windows-x64/`，双击 `启动后端.bat`。
- 会同时打开后端和监控两个窗口，关后端时监控一起收。
- 整个文件夹就是数据目录，拷走就能带走数据。

### 已知限制

- 两个平台都没做代码签名：macOS 要右键「打开」，Windows 可能弹 SmartScreen 选「仍要运行」。
- macOS 版只能在 macOS 上构建，架构跟随构建机的 Python。
- 客户端单独发给别人只会得到登录界面，还得有后端。

[↑ 回到顶部](#collabnote)

---

## 项目结构

```text
my-electron-app/
├── index.html                 Electron 页面：界面、样式、前端逻辑
├── main.js                    主进程：窗口、凭据、--profile 多实例
├── preload.js                 安全桥接
├── server.py                  后端：认证、好友、笔记、账本、上传
├── database.py                存储层：MySQL 与 JSON 两套实现
├── schema.sql                 建表脚本（首次启动自动执行）
├── migrate_json_to_mysql.py   JSON → MySQL 迁移（支持 --dry-run）
├── monitor.py                 终端监控器
├── launcher.js                进程启动器（多客户端）
├── build-backend.py           免安装后端打包
├── start-frontend.js          启动前端：补齐 Electron、清环境变量
├── start-collabnote.sh        一键启动
├── .env.example               环境变量模板（密码不写入仓库）
├── package.json               npm 脚本与依赖
├── build/                     应用图标（含 icon-source.png 源文件）
├── data/                      头像与笔记图片（运行时生成）
├── frontend/vendor/           Font Awesome 等静态依赖
├── notes.json / users.json / ledgers.json    JSON 模式数据（不提交）
└── .local/                    本机运行环境（MySQL、打包产物，不提交）
```

Electron 和 Python 是两个进程，前端不读数据文件。流程：登录拿 Token → 存 `localStorage` → 请求带 `Authorization: Bearer` → 后端查存储 → 写回。后端重启后内存会话清空，要重新登录。

[↑ 回到顶部](#collabnote)

---

## API

认证：

```text
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/me
POST /api/auth/profile
```

笔记：

```text
GET    /api/notes
POST   /api/notes
PUT    /api/notes/:id
DELETE /api/notes/:id
GET    /api/notes/:id/members
POST   /api/notes/:id/members
DELETE /api/notes/:id/members/:username
```

账本：

```text
GET    /api/ledgers
POST   /api/ledgers
PUT    /api/ledgers/:id
DELETE /api/ledgers/:id
GET    /api/ledgers/:id/transactions
POST   /api/ledgers/:id/transactions
PUT    /api/ledgers/:id/transactions/:txn
DELETE /api/ledgers/:id/transactions/:txn
GET    /api/ledgers/:id/members
POST   /api/ledgers/:id/members
DELETE /api/ledgers/:id/members/:username
GET    /api/ledgers/:id/history
POST   /api/ledgers/:id/restore
GET    /api/ledgers/:id/settlement
```

好友：

```text
GET  /api/friends
POST /api/friends
POST /api/friends/requests/:username/accept
POST /api/friends/requests/:username/reject
```

在线协作：

```text
POST /api/presence
GET  /api/events?token=<token>
```

`presence` 请求体 `{"noteId": <id>, "mode": "editing" | "viewing"}`，12 秒无心跳算离线。

`events` 是 SSE，推送这些事件（除 `presence` 外都带 `sid`，前端用它忽略自己造成的变更）：

```text
note-created    { note, sid }
note-updated    { note, sid }
note-deleted    { noteId, sid }
note-members    { note, sid }
note-unshared   { noteId, sid }
presence        { noteId, people }
ledger-changed  { ledgerId, sid }
```

连上先收 `: connected`，之后每 15 秒一个 `: ping`。

监控与公开资源：

```text
GET /api/monitor/status     在线用户、运行时间、计数（JSON）
GET /api/monitor/logs       日志 SSE，先补发最近 500 行
GET /api/users/:username/avatar
GET /api/health
POST /api/images/upload     需登录，JPG/PNG，单张 ≤ 10MB
GET  /api/images/:file
```

除 health、注册、登录、登出、头像和图片读取外，其余都要带 `Authorization: Bearer <token>`。

[↑ 回到顶部](#collabnote)

---

## 安全边界

- 密码只存 PBKDF2-SHA256 + 随机盐，不存明文。
- 会话在内存里，后端重启即失效；30 天无活动也失效。
- 同一账号 5 分钟内登录失败 10 次会被暂时限流。
- 单次请求体上限 8MB。
- 账号、笔记、权限、账本都只在后端：MySQL 模式在 `users` / `notes` / `note_members` / `ledgers` 等表，JSON 模式在三个 `.json` 文件。前端不留副本，`localStorage` 只存主题、服务器地址、最近账号和 token。
- `.gitignore` 挡住 `/users.json`、`/notes.json`、`/ledgers.json`、`/data/`、`/frontend/assets/`、`/.collabnote-pids.json` 和 `/tmp*`，`git add -A` 也推不上去。
- 监控接口只收 `127.0.0.1` / `::1`，远端要 token。
- 后端默认监听 `0.0.0.0`，只在可信局域网用；公网部署请改 `COLLABNOTE_HOST=127.0.0.1` 并套反向代理。
- 生产环境还缺 HTTPS、持久化会话、更严的输入校验。

[↑ 回到顶部](#collabnote)

---

## 备注

这是一个 UI 测试程序。

事已至此先吃饭吧，炒饭好吃。

测试提交 owo
进行了部分修改

修复好友头像小 bug
增添界面色调预设
