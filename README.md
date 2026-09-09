# CollabNote

一个基于 Electron 与 Python 的多人共享笔记应用。Electron 前端与 Python 后端是两个独立进程，界面不直接读取数据文件，所有业务数据通过 HTTP API 传输。

## 功能特性

**账号与安全**

- 用户注册、登录、登出，使用 Bearer Token 会话认证。
- 密码以 PBKDF2-SHA256 加随机盐保存，不保存明文密码。
- 记住最近登录用户并自动填写；勾选后经系统密码库（macOS 钥匙串 / Windows DPAPI）加密保存密码。

**笔记编辑**

- 笔记读取、新建、编辑、自动保存、搜索与删除。
- 编辑器顶部实时显示同步状态：已同步 / 保存中… / 服务器断开时显示已断开。
- 删除笔记前显示确认弹窗。

**好友与社交**

- 好友申请、接受或拒绝，以及双向好友关系。
- 好友在线/离线状态，每 15 秒刷新一次。
- 好友栏停靠主窗口右侧，可一键收起或重新展开。

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

启动器会记录前后端 PID，启动后端时会等待 API 端口就绪后再继续。

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

## 网络配置

后端默认监听 `http://127.0.0.1:8765`。可通过环境变量修改后端监听地址和端口：

```bash
COLLABNOTE_HOST=0.0.0.0 COLLABNOTE_PORT=8766 npm run backend
```

前端连接独立部署的 Python 服务：

```bash
COLLABNOTE_API_URL=http://127.0.0.1:8766 npm run frontend
```

## 项目结构

```text
my-electron-app/
├── index.html                 Electron 页面：界面、样式与前端逻辑
├── main.js                    Electron 主进程：窗口生命周期与凭据存取
├── preload.js                 安全桥接，向页面暴露 API 地址
├── server.py                  Python HTTP 后端：API、认证、好友、笔记、头像
├── launcher.js                前后端进程启动器
├── package.json               npm 脚本与 Electron 依赖
├── requirements.txt           Python 依赖
├── notes.json                 笔记数据文件
├── users.json                 用户账号与好友关系数据文件
└── frontend/
    └── assets/avatars/        头像资源目录（Git 可见）
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
2. 输入 3-32 位用户名和至少 6 位密码。
3. 注册成功后自动进入笔记工作区，已有账号直接登录。

### 管理笔记

1. 从左侧列表选择笔记，编辑右侧标题或正文，内容会自动保存。
2. 点击「新增笔记」创建笔记。
3. 点击笔记右侧删除按钮，确认后删除。
4. 使用搜索框按标题筛选。

### 添加好友

1. 在主界面右侧的好友栏「好友状态」中输入对方登录名，点击添加按钮发送好友请求。
2. 点击「添加好友」旁的请求按钮展开请求区，可在「待处理请求」中接受或拒绝。
3. 接受后双方都会看到彼此的名称和在线状态。
4. 请求按钮上的红点提示待处理数量，「已发送请求」显示尚未处理的请求。

### 修改个人资料

1. 点击右侧用户入口，打开「个性化设置」。
2. 修改显示名称、自定义头衔或上传 JPG/PNG 头像。
3. 保存后后端会裁剪并压缩头像为 `256x256` JPEG。

## 数据文件

- `notes.json`：笔记数据。
- `users.json`：用户账号、密码哈希、昵称、头衔和好友关系。
- `frontend/assets/avatars/`：处理后的头像文件，统一为 `256x256` JPEG，并纳入 Git 副本。
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

头像与服务状态：

```text
GET /api/users/:username/avatar
GET /api/health
```

除 `/api/health`、注册和登录接口外，业务接口需要携带：

```text
Authorization: Bearer <token>
```

## 安全边界

- 密码只保存 PBKDF2-SHA256 哈希和随机盐。
- 当前会话存储在 Python 内存中，后端重启后失效。
- JSON 文件和头像目录应定期备份。
- 当前实现适合本地或受信任局域网；生产部署还应增加 HTTPS、持久化会话、访问控制和更严格的输入限制。

## 开发备注

这是一个 UI 测试程序。

事已至此先吃饭吧，炒饭好吃。
