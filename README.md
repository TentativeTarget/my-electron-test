# CollabNote

一个基于 Electron 和 Python 的多人共享笔记应用。Electron 前端与 Python 后端是两个独立进程，所有业务数据通过 HTTP API 传输。

## 功能

- 用户注册、登录、登出和 Bearer Token 会话认证。
- 笔记读取、新建、编辑、自动保存和删除。
- 删除笔记前显示确认弹窗。
- 好友申请、接受或拒绝申请，以及双向好友关系。
- 好友在线/离线状态，每 15 秒刷新一次。
- 用户昵称、个性化头衔和头像设置。
- JPG/PNG 头像由后端裁剪、压缩为 `256x256` JPEG。
- 后端不可用时显示连接错误窗口，并支持重新连接。

## 环境要求

- Node.js 和 npm
- Python 3
- Electron 依赖
- Pillow 图片处理库

安装依赖：

```bash
npm install
python3 -m pip install -r requirements.txt
```

## 启动方式

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

启动器会记录前后端 PID，并在启动后端时等待 API 端口就绪。

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

后端默认监听：

```text
http://127.0.0.1:8765
```

修改 Python 后端监听地址和端口：

```bash
COLLABNOTE_HOST=0.0.0.0 COLLABNOTE_PORT=8766 npm run backend
```

前端连接独立部署的 Python 服务：

```bash
COLLABNOTE_API_URL=http://127.0.0.1:8766 npm run frontend
```

## 数据文件

- `notes.json`：笔记数据。
- `users.json`：用户账号、密码哈希、昵称、头衔和好友关系。
- `user_avatars/`：后端处理后的头像文件，统一为 `256x256` JPEG。
- `.collabnote-pids.json`：启动器运行时 PID 文件，不应提交到版本库。

密码只保存为 PBKDF2-SHA256 哈希和随机盐，不保存明文密码。

## 主要 API

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

头像和服务状态：

```text
GET /api/users/:username/avatar
GET /api/health
```

除 `/api/health`、注册和登录接口外，业务接口需要携带：

```text
Authorization: Bearer <token>
```

## 开发备注

这是一个 UI 测试程序。

事已至此先吃饭吧，炒饭好吃。
