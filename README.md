# CollabNote

Electron 前端與 Python 後端是兩個獨立程序，筆記資料只透過 HTTP API 傳輸。

## 啟動後端

```bash
npm run backend
```

預設監聽 `http://127.0.0.1:8765`，資料保存在 `notes.json`。

## 啟動前端

在另一個終端執行：

```bash
npm run frontend
```

前端預設連線到 `http://127.0.0.1:8765`。如需連接獨立部署的 Python 服務，可設定 API 位址：

```bash
COLLABNOTE_API_URL=http://127.0.0.1:8765 npm run frontend
```

Python 後端也可透過 `COLLABNOTE_HOST` 和 `COLLABNOTE_PORT` 設定監聽位址與端口。

# 這是一個UI測試的程序
雖然不知道為啥寫出來那麼💩
## 事已至此先吃飯吧
炒飯好吃
