# CollabNote

一个基于 Electron 与 Python 的多人共享笔记应用。Electron 前端与 Python 后端是两个独立进程，界面不直接读取数据文件，所有业务数据通过 HTTP API 传输。

## 功能特性

**账号与安全**

- 用户注册、登录、登出，使用 Bearer Token 会话认证。
- 密码以 PBKDF2-SHA256 加随机盐保存，不保存明文密码。
- 记住最近登录用户并自动填写；勾选后经系统密码库（macOS 钥匙串 / Windows DPAPI）加密保存密码。

**笔记编辑**

- 笔记读取、新建、编辑、自动保存、搜索与删除。
- 富文本工具栏：粗体 / 斜体 / 下划线 / 删除线、项目与编号列表、引用块、表格，格式即时生效。
- 字号可调整为「默认」或 12-40px，选中文字即时预览并随光标同步显示。
- 可插入超链接（自动补全 `https://`），点击链接会在系统默认浏览器中打开（仅限 http/https）。
- 「图片」采用本地文件上传，由后端存储到 `frontend/assets/images/`；点击图片可拖拽等比缩放，按 Delete 删除、按 Esc 取消。
- 每篇笔记在本次会话内保留最多 20 个版本快照，可随时复原（重启后清空）。
- 编辑器顶部实时显示同步状态：已同步 / 保存中… / 服务器断开时显示已断开。
- 正在查看同一篇笔记的好友（含自己）实时出现在标题旁头像堆叠与底部信息栏，区分「正在编辑 / 檢視中」，约每 4 秒心跳刷新。
- 删除笔记前显示确认弹窗。

**好友与社交**

- 好友申请、接受或拒绝，以及双向好友关系。
- 好友在线/离线状态，每 15 秒刷新一次。
- 单后端多前端：多位使用者以各自账号连接同一台服务器，笔记、好友与在线状态实时共享；登录页可填写服务器地址，支持 `--profile` 在同一台电脑上开多个实例。
- 好友栏停靠主窗口右侧，默认收起；圆形切换按钮默认贴在主框架右侧边框线上，展开后滑到好友栏左侧边线。点击时同一颗按钮在两个锚点间以非线性缓动平滑移动、图标同步旋转 180°，不会因按钮切换而闪烁。
- 在线好友头像区位于好友栏顶部：按在线优先展示，最多 5 个头像，在线计数不限且包含本人，右侧同时显示好友总数。

**界面与个性化**

- 深色/浅色主题切换，登录页与笔记工作区均可操作，偏好保存在本地。
- 昵称、自定义头衔与 JPG/PNG 头像设置，头像由后端裁剪压缩为 `256x256` JPEG。
- 后端不可用时显示连接错误窗口，并支持重新连接。

## 快速开始

需要 Node.js（含 npm）与 Python 3。安装依赖：

```bash
npm install
python3 -m pip install -r requirements.txt
```

### 一键启动

同时启动 Python 后端和 Electron 前端：

```bash
npm run launcher -- start all
```

查看运行状态、重启或关闭：

```bash
npm run launcher -- status all
npm run launcher -- restart all
npm run launcher -- stop all
```

启动器会记录前后端 PID，启动后端时会等待 API 端口就绪后再继续。后端默认监听所有网卡，同一局域网的其他电脑也能连接，详见「多用户连接同一个后端」。

### 独立启动

后端：

```bash
npm run backend
```

前端需要在另一个终端启动：

```bash
npm run frontend
```

也可以分别控制单个进程：

```bash
npm run launcher -- start backend
npm run launcher -- stop backend
npm run launcher -- start frontend
npm run launcher -- stop frontend
```

## 多用户连接同一个后端

一个后端进程可以同时服务多位使用者：所有账号、好友关系、笔记和在线状态都由 `server.py` 统一保存，前端只负责连接与显示。

### 1. 启动服务器

后端默认监听 `0.0.0.0`（所有网卡），启动后会打印可用地址：

```bash
npm run backend
```

```text
Notes API listening on http://0.0.0.0:8765
  區域網連線地址: http://192.168.1.100:8765
  多位使用者可將前端伺服器地址設為上述任一網址後登入不同帳號。
  本機連線地址: http://127.0.0.1:8765
```

- 只允许本机连接时使用 `COLLABNOTE_HOST=127.0.0.1 npm run backend`。
- 修改端口：`COLLABNOTE_PORT=8766 npm run backend`。
- 若系统防火墙询问是否允许 Python 接受传入连接，需要选择允许，否则其他电脑无法连入。

### 2. 让前端连接该服务器

每位使用者在自己电脑上打开登录页，在「服务器地址」填入服务器打印的局域网地址（例如 `http://192.168.1.100:8765`）后登录自己的账号。地址会记住，下次启动自动带入；留空则连接本机 `127.0.0.1:8765`。

也可以在启动时用环境变量固定地址（此时登录页的地址栏变为只读）：

```bash
COLLABNOTE_API_URL=http://192.168.1.100:8765 npm run frontend
```

### 3. 在同一台电脑上模拟多用户

用 `--profile` 或 `COLLABNOTE_PROFILE` 启动多个前端实例，每个实例拥有独立的登录状态与已保存密码：

```bash
npm run frontend -- --profile=alice
COLLABNOTE_PROFILE=bob npm run frontend
```

### 4. 连接失败时

界面会弹出「后端连接失败」对话框，并显示当前使用的服务器地址。可点击「更改服务器地址」回到登录页重新填写，或确认服务器已启动后点「重新连接」。

## 项目结构

```text
my-electron-app/
├── index.html                 Electron 页面：界面、样式与前端逻辑
├── main.js                    Electron 主进程：窗口生命周期、凭据存取与 --profile 多实例
├── preload.js                 安全桥接，向页面暴露默认 API 地址与设定档名称
├── server.py                  Python HTTP 后端：API、认证、好友、笔记、头像
├── launcher.js                前后端进程启动器
├── package.json               npm 脚本与 Electron 依赖
├── requirements.txt           Python 依赖
├── notes.json                 笔记数据文件
├── users.json                 用户账号与好友关系数据文件
└── frontend/
    ├── assets/avatars/        头像资源目录（Git 可见）
    └── assets/images/         笔记图片资源目录（运行时创建）
```

Electron 与 Python 是两个独立进程，前端不直接读取数据文件，只通过 HTTP API 通信。业务请求流程如下：

1. 用户注册或登录，后端返回会话 Token。
2. 前端将 Token 保存在 `localStorage`。
3. 后续业务请求携带 `Authorization: Bearer <token>`。
4. 后端验证会话后读写 JSON 数据或头像文件。
5. 后端重启后内存会话清空，用户需要重新登录。

## 使用指南

### 注册与登录

1. 启动后端和前端，在登录页切换到注册模式。
2. 确认「服务器地址」指向你要使用的后端：留空为本机，多人协作时填服务器电脑的局域网地址。
3. 输入 3-32 位用户名和至少 6 位密码。
4. 注册成功后自动进入笔记工作区，已有账号直接登录。

### 管理笔记

1. 从左侧列表选择笔记，编辑右侧标题或正文，内容会自动保存。
2. 点击「新增笔记」创建笔记。
3. 点击笔记右侧删除按钮，确认后删除。
4. 使用搜索框按标题筛选。
5. 选中文字后可用工具栏设置粗体、斜体、下划线、删除线、列表、引用块或调整字号，也可插入超链接、表格或图片。
6. 点击笔记中的图片会出现蓝色边框与拖拽手柄，可等比缩放；Delete 删除，Esc 取消选中。
7. 点击工具栏「历史」可查看并复原当前笔记在本次会话内的版本快照。
8. 笔记中的链接可以直接点击，由系统默认浏览器打开（仅 http/https）。

### 添加好友

1. 点击主体与窗口边框之间的圆形箭头展开好友栏；在「好友状态」中输入对方登录名，点击添加按钮发送好友请求。
2. 点击「添加好友」旁的请求按钮展开请求区，可在「待处理请求」中接受或拒绝。
3. 接受后双方都会看到彼此的名称和在线状态。
4. 请求按钮上的红点提示待处理数量，「已发送请求」显示尚未处理的请求。

### 修改个人资料

1. 展开右侧好友栏，点击顶部用户入口，打开「个性化设置」。
2. 修改显示名称、自定义头衔或上传 JPG/PNG 头像。
3. 保存后后端会裁剪并压缩头像为 `256x256` JPEG。

## 数据文件

- `notes.json`：笔记数据。
- `users.json`：用户账号、密码哈希、昵称、头衔和好友关系。
- `frontend/assets/avatars/`：处理后的头像文件，统一为 `256x256` JPEG，并纳入 Git 副本。
- `frontend/assets/images/`：笔记中通过「图片」工具上传的图片文件，运行时创建。
- `.collabnote-pids.json`：启动器运行时 PID 文件，不应提交到版本库。

## API 参考

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
```

好友：

```text
GET  /api/friends
POST /api/friends
POST /api/friends/requests/:username/accept
POST /api/friends/requests/:username/reject
```

在线协作（实时查看/编辑状态）：

```text
POST /api/presence
```

请求体为 `{"noteId": <笔记 id>, "mode": "editing" | "viewing"}`，返回该笔记上在线的好友与自己的名单（含显示名、头像与 mode）。连续 12 秒无心跳会自动视为离线；登出或 `noteId: null` 时立即移除。

头像与服务状态：

```text
GET /api/users/:username/avatar
GET /api/health
```

笔记图片：

```text
POST /api/images/upload
GET  /api/images/:file
```

上传接口需要登录（携带 `Authorization: Bearer <token>`），仅接受 JPG/PNG，单张上限 10MB；图片保存到 `frontend/assets/images/` 后以 `/api/images/:file` 公开读取。

除 `/api/health`、注册和登录接口外，业务接口需要携带：

```text
Authorization: Bearer <token>
```

## 安全边界

- 密码只保存 PBKDF2-SHA256 哈希和随机盐。
- 当前会话存储在 Python 内存中，后端重启后失效。
- JSON 文件和头像目录应定期备份。
- 后端默认监听 `0.0.0.0`，同一局域网内的其他设备都能访问；仅在受信任网络中使用，公网部署前应改为 `COLLABNOTE_HOST=127.0.0.1` 并置于反向代理之后。
- 前端只有在登录页或 `COLLABNOTE_API_URL` 指定地址时才会连接外部服务器，不会主动扫描网络。
- 当前实现适合本地或受信任局域网；生产部署还应增加 HTTPS、持久化会话、访问控制和更严格的输入限制。

## 开发备注

这是一个 UI 测试程序。

事已至此先吃饭吧，炒饭好吃。

测试提交owo
进行了部分修改

修复好友头像小bug
增添界面色调预设