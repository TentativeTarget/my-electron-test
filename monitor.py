#!/usr/bin/env python3
"""CollabNote 後端終端監控器：左側列出連線使用者，右側顯示伺服器即時日誌。

用法：
    python3 monitor.py                       # 連到 http://127.0.0.1:8765
    python3 monitor.py --port 9000           # 指定連接埠
    python3 monitor.py --url http://192.168.1.50:8765
    python3 monitor.py --token <TOKEN>       # 非本機來源需對應 COLLABNOTE_MONITOR_TOKEN
    python3 monitor.py --once                # 只輸出一次畫面後結束（腳本／檢查用）

按鍵：
    q 離開    p 暫停／跟隨    c 清空日誌    a 顯示／隱藏靜態請求（預設隱藏）
    ↑↓ 捲動   PgUp／PgDn 翻頁   Home／End 跳到最舊／最新
"""

import argparse
import json
import os
import re
import select
import shutil
import signal
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import deque

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class Colors:
    """終端顏色；關閉時全部是空字串，版面計算不受影響。"""

    def __init__(self, enabled):
        codes = {
            "reset": "\x1b[0m",
            "bold": "\x1b[1m",
            "dim": "\x1b[2m",
            "red": "\x1b[31m",
            "green": "\x1b[32m",
            "yellow": "\x1b[33m",
            "blue": "\x1b[34m",
            "magenta": "\x1b[35m",
            "cyan": "\x1b[36m",
            "grey": "\x1b[90m",
        }
        for name, code in codes.items():
            setattr(self, name, code if enabled else "")


def char_width(char):
    """中日韓全形字算 2 欄，組合字元算 0 欄，其餘 1 欄。"""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    return sum(char_width(char) for char in text)


def plain(text):
    """去掉 ANSI 控制碼後的可見文字。"""
    return ANSI_PATTERN.sub("", text)


def truncate(text, width):
    if display_width(text) <= width:
        return text
    if width <= 0:
        return ""
    kept, used = "", 0
    for char in text:
        step = char_width(char)
        if used + step > width - 1:
            break
        kept += char
        used += step
    return kept + "…"


def fit(text, width):
    """把文字補到剛好 width 欄；放不下時截斷（截斷時會捨去顏色碼）。"""
    bare = plain(text)
    if display_width(bare) > width:
        return truncate(bare, width)
    return text + " " * max(0, width - display_width(bare))


class Monitor:
    """背景執行兩條連線：輪詢使用者狀態 + 訂閱日誌 SSE。"""

    LEVEL_STYLE = {
        "INFO": ("cyan", "INFO"),
        "HTTP": ("blue", "HTTP"),
        "STATIC": ("grey", "STATIC"),
        "EVENT": ("magenta", "EVENT"),
        "PRESENCE": ("green", "LIVE"),
        "WARN": ("yellow", "WARN"),
        "ERROR": ("red", "ERROR"),
    }

    def __init__(self, base_url, token="", interval=1.0, title="", exit_when_offline=0.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.interval = max(0.2, interval)
        self.title = title
        self.exit_when_offline = max(0.0, exit_when_offline)
        self.offline_since = None
        self.pid_file = ""
        self.lock = threading.Lock()
        self.logs = deque(maxlen=2000)
        self.status = None
        self.state = "connecting"
        self.error = ""
        self.paused = False
        self.show_static = False
        self.offset = 0
        self.stopping = False
        self.stdin_fd = None

    # ─── 資料來源 ───
    def _open(self, path, timeout):
        request = urllib.request.Request(self.base_url + path)
        if self.token:
            request.add_header("X-Monitor-Token", self.token)
        return urllib.request.urlopen(request, timeout=timeout)

    def start(self):
        for target in (self.poll_status, self.stream_logs):
            threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self.stopping = True

    def _sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def poll_status(self):
        while not self.stopping:
            try:
                with self._open("/api/monitor/status", 3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                with self.lock:
                    self.status = payload
                    self.state = "online"
                    self.error = ""
                    self.offline_since = None
            except urllib.error.HTTPError as exc:
                with self.lock:
                    self.state = "offline"
                    self.error = "HTTP %s（監控端點只開放本機，或需要 --token）" % exc.code
                    self.note_offline()
            except Exception as exc:
                with self.lock:
                    self.state = "offline"
                    self.error = str(getattr(exc, "reason", "") or exc) or exc.__class__.__name__
                    self.note_offline()
            self._sleep(self.interval)

    def note_offline(self):
        """後端失聯就開始計時，超過門檻讓監控器自己結束（由呼叫端持鎖）。"""
        now = time.monotonic()
        if self.offline_since is None:
            self.offline_since = now
        elif self.exit_when_offline and now - self.offline_since >= self.exit_when_offline:
            self.stopping = True

    def stream_logs(self):
        backoff = 1.0
        while not self.stopping:
            try:
                with self._open("/api/monitor/logs", None) as response:
                    with self.lock:
                        self.state = "online"
                    backoff = 1.0
                    pending = ""
                    for raw in response:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            pending = line[5:].strip()
                        elif not line and pending:
                            self.handle_event(pending)
                            pending = ""
            except Exception:
                pass
            if self.stopping:
                break
            self._sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def handle_event(self, raw):
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        with self.lock:
            if event.get("type") == "backlog":
                self.logs.clear()
                self.logs.extend(event.get("lines") or [])
            elif event.get("type") == "line":
                self.logs.append({
                    "time": event.get("time", ""),
                    "level": event.get("level", "INFO"),
                    "text": event.get("text", ""),
                })
            elif event.get("type") == "shutdown" and self.exit_when_offline:
                # 後端關閉通知：由後端開啟的監控器跟著結束，視窗才好收掉
                self.stopping = True

    # ─── 畫面 ───
    def visible_logs(self):
        if self.show_static:
            return list(self.logs)
        return [entry for entry in self.logs if entry.get("level") != "STATIC"]

    def render(self, colors, columns=None, rows=None):
        if columns is None or rows is None:
            size = shutil.get_terminal_size((110, 32))
            columns, rows = (columns or size.columns), (rows or size.lines)
        columns = max(40, columns - 1)
        rows = max(14, rows)
        inner = columns - 2
        left_width = max(18, min(38, inner // 3))
        right_width = max(16, inner - left_width - 1)
        body_rows = rows - 3

        with self.lock:
            status = self.status or {}
            info = status.get("server") or {}
            users = [user for user in (status.get("users") or []) if user.get("online") or user.get("streams")]
            logs = self.visible_logs()
            offset = self.offset if self.paused else 0
            state = self.state
            error = self.error
            paused = self.paused

        # 左側：連線使用者
        left = [colors.bold + "連線使用者 %d" % len(users) + colors.reset]
        if not users:
            left.append(colors.grey + "（目前沒有使用者）" + colors.reset)
        for user in users:
            live = bool(user.get("online"))
            marker = (colors.green + "●" + colors.reset) if live else (colors.grey + "○" + colors.reset)
            mode = user.get("mode")
            if mode == "editing":
                tag, tag_color = "編輯", colors.yellow
            elif mode == "viewing":
                tag, tag_color = "檢視", colors.cyan
            else:
                tag, tag_color = "在線", colors.grey
            name = user.get("displayName") or user.get("username")
            if name != user.get("username"):
                name = "%s(%s)" % (name, user.get("username"))
            prefix = "%s %s" % (marker, name)
            gap = max(1, left_width - 2 - display_width(plain(prefix)) - display_width(tag))
            left.append(prefix + " " * gap + tag_color + tag + colors.reset)
            if user.get("noteTitle") is not None:
                detail = "#%s %s" % (user.get("noteId"), user.get("noteTitle") or "")
            elif user.get("streams"):
                detail = "已連線，未開啟筆記"
            else:
                detail = "等待中"
            left.append(colors.grey + "  " + detail + colors.reset)

        # 右側：伺服器日誌
        header = colors.bold + "伺服器即時日誌" + colors.reset
        tail_note = (colors.yellow + "已暫停" if paused else colors.green + "跟隨中") + colors.reset
        if offset:
            tail_note += colors.grey + " (-%d)" % offset + colors.reset
        gap = max(1, right_width - 2 - display_width(plain(header)) - display_width(plain(tail_note)))
        right = [header + " " * gap + tail_note]
        end = max(0, len(logs) - offset)
        start = max(0, end - (body_rows - 1))
        for entry in logs[start:end]:
            level = str(entry.get("level", "INFO")).upper()
            color_name, label = self.LEVEL_STYLE.get(level, ("reset", level))
            color = getattr(colors, color_name, "")
            stamp = colors.grey + fit(entry.get("time", ""), 8) + colors.reset
            badge = color + fit(label, 6) + colors.reset
            message_width = max(12, right_width - 19)
            right.append("%s %s %s" % (stamp, badge, fit(entry.get("text", ""), message_width)))

        # 組合外框
        title = " CollabNote 伺服器監控 "
        if state == "online":
            state_text, state_color = "已連線", colors.green
        elif state == "offline":
            state_text, state_color = "無法連線", colors.red
        else:
            state_text, state_color = "連線中…", colors.yellow
        right_corner = state_color + "● " + state_text + colors.reset + " "
        filler = inner - display_width(title) - display_width(plain(right_corner))
        lines = ["┌" + title + "─" * max(0, filler) + right_corner + "┐"]
        for index in range(body_rows):
            left_cell = " " + fit(left[index], left_width - 2) + " " if index < len(left) else " " * left_width
            right_cell = " " + fit(right[index], right_width - 2) + " " if index < len(right) else " " * right_width
            lines.append("│" + left_cell + "│" + right_cell + "│")
        lines.append("└" + "─" * inner + "┘")

        uptime = info.get("uptimeSeconds")
        if uptime is None:
            uptime_text = "--:--:--"
        else:
            total = int(uptime)
            uptime_text = "%02d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)
        if error:
            hint = "連線失敗：%s（自動重試中）" % error
        else:
            hint = "q 離開  p 暫停  c 清空  a 靜態請求(%s)  ↑↓ 捲動" % ("顯示" if self.show_static else "隱藏")
        footer = "%s  │  %s  運行 %s  在線 %s/%s  日誌 %d" % (
            hint,
            self.base_url,
            uptime_text,
            info.get("onlineCount", 0),
            info.get("userCount", 0),
            len(logs),
        )
        lines.append(fit(footer, columns))
        return lines

    def draw(self, lines):
        parts = ["\x1b[H"]
        for index, line in enumerate(lines, start=1):
            parts.append("\x1b[%d;1H" % index)
            parts.append(line)
            parts.append("\x1b[K")
        parts.append("\x1b[J")
        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    # ─── 按鍵 ───
    KEYS = {
        "\x1b[A": "up", "\x1b[B": "down", "\x1b[5~": "pageup", "\x1b[6~": "pagedown",
        "\x1b[H": "home", "\x1b[F": "end", "\x1b[1~": "home", "\x1b[4~": "end",
    }

    @classmethod
    def parse_keys(cls, data):
        keys, index = [], 0
        while index < len(data):
            matched = False
            for sequence, name in cls.KEYS.items():
                if data.startswith(sequence, index):
                    keys.append(name)
                    index += len(sequence)
                    matched = True
                    break
            if not matched:
                keys.append(data[index])
                index += 1
        return keys

    def handle_key(self, key):
        with self.lock:
            total = len(self.logs)
            if key in ("q", "Q", "\x03"):
                self.stopping = True
            elif key in ("p", "P", " "):
                self.paused = not self.paused
                if not self.paused:
                    self.offset = 0
            elif key in ("c", "C"):
                self.logs.clear()
                self.offset = 0
            elif key in ("a", "A"):
                self.show_static = not self.show_static
                self.offset = 0
            elif key == "up":
                self.paused = True
                self.offset = min(total, self.offset + 1)
            elif key == "down":
                self.offset = max(0, self.offset - 1)
                self.paused = self.offset > 0
            elif key == "pageup":
                self.paused = True
                self.offset = min(total, self.offset + 10)
            elif key == "pagedown":
                self.offset = max(0, self.offset - 10)
                self.paused = self.offset > 0
            elif key == "home":
                self.paused = True
                self.offset = total
            elif key == "end":
                self.paused = False
                self.offset = 0

    def pump_input(self, timeout):
        if self.stdin_fd is None:
            time.sleep(timeout)
            return
        try:
            ready, _, _ = select.select([self.stdin_fd], [], [], timeout)
        except (OSError, ValueError):
            time.sleep(timeout)
            return
        if not ready:
            return
        try:
            data = os.read(self.stdin_fd, 32)
        except OSError:
            return
        for key in self.parse_keys(data.decode("utf-8", "replace")):
            self.handle_key(key)

    def run(self, colors):
        if self.pid_file:
            try:
                with open(self.pid_file, "w", encoding="utf-8") as handle:
                    handle.write(str(os.getpid()))
            except OSError:
                self.pid_file = ""
        interactive = sys.stdin.isatty()
        if interactive:
            self.stdin_fd = sys.stdin.fileno()
        terminal_state = None
        if interactive:
            try:
                import termios
                import tty
                terminal_state = termios.tcgetattr(self.stdin_fd)
                tty.setcbreak(self.stdin_fd)
            except Exception:
                terminal_state = None
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        if self.title:
            # 設定終端機視窗標題，後端關閉後才能認出這個視窗並收掉
            sys.stdout.write("\x1b]0;%s\x07" % self.title)
        sys.stdout.flush()
        try:
            while not self.stopping:
                self.draw(self.render(colors))
                self.pump_input(0.2)
        finally:
            if terminal_state is not None:
                import termios
                termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, terminal_state)
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            if self.pid_file:
                try:
                    os.remove(self.pid_file)
                except OSError:
                    pass


def build_parser():
    parser = argparse.ArgumentParser(description="CollabNote 後端終端監控器")
    parser.add_argument("--url", default=os.environ.get("COLLABNOTE_MONITOR_URL", ""),
                        help="伺服器位址，例如 http://192.168.1.50:8765")
    parser.add_argument("--host", default=os.environ.get("COLLABNOTE_MONITOR_HOST", "127.0.0.1"),
                        help="伺服器主機（未指定 --url 時使用）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("COLLABNOTE_PORT", 8765)),
                        help="伺服器連接埠（預設 8765）")
    parser.add_argument("--token", default=os.environ.get("COLLABNOTE_MONITOR_TOKEN", ""),
                        help="非本機來源需要的監控 token")
    parser.add_argument("--interval", type=float, default=1.0, help="使用者名單更新間隔（秒）")
    parser.add_argument("--title", default="", help="設定終端機視窗標題")
    parser.add_argument("--pid-file", default="", help="啟動時寫入自己的 PID，結束時刪除")
    parser.add_argument("--exit-when-offline", type=float, default=0.0,
                        help="後端失聯幾秒後自動結束（0 表示持續重試）")
    parser.add_argument("--once", action="store_true", help="只輸出一次畫面後結束")
    parser.add_argument("--no-color", action="store_true", help="關閉顏色")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    base_url = args.url.strip() or "http://%s:%d" % (args.host, args.port)
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url
    colors = Colors(not args.no_color and sys.stdout.isatty())
    monitor = Monitor(base_url, args.token, args.interval,
                      title=args.title, exit_when_offline=args.exit_when_offline)
    monitor.pid_file = args.pid_file

    def handle_signal(signum, frame):
        monitor.stop()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handle_signal)
            except (ValueError, OSError):
                pass

    monitor.start()
    if args.once:
        time.sleep(1.0)
        monitor.stop()
        columns, rows = shutil.get_terminal_size((110, 32))
        for line in monitor.render(colors, columns, rows):
            print(line)
        return 0
    monitor.run(colors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
