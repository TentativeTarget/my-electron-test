#!/usr/bin/env python3
"""打包免安裝的 CollabNote 後端：使用者不需安裝 Python，雙擊即可執行。

用法：
    python3 build-backend.py                  # 依目前平台打包到 dist/backend
    python3 build-backend.py --out <目錄>      # 指定輸出目錄
    python3 build-backend.py --platform win   # 在任何平台上組裝 Windows 版

macOS / Linux 用 PyInstaller 凍結成單一資料夾；Windows 用官方嵌入式 Python
加上 Pillow 的官方 wheel 組裝，不需要編譯環境。
"""

import argparse
import os
import pathlib
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

REPO = pathlib.Path(__file__).resolve().parent
DEFAULT_OUT = REPO / "dist" / "backend"
MYSQL_DIR_CANDIDATES = (
    REPO.parent / "Documents" / "OH-WorkSpace" / "邺珞-mysql",
    REPO.parent / "OH-WorkSpace" / "邺珞-mysql",
)
SERVER_NAME = "CollabNote-Server"
EMBED_VERSION = "3.12.10"
EMBED_URL = ("https://registry.npmmirror.com/-/binary/python/%s/python-%s-embed-amd64.zip"
             % (EMBED_VERSION, EMBED_VERSION))
PYTHON_PACKAGES = ("Pillow>=10.0", "PyMySQL>=1.1,<2", "bleach>=6.1,<7", "tinycss2>=1.2,<2", "webencodings>=0.5")

MAC_LAUNCHER = "啟動後端.command"
MAC_BUNDLE_LAUNCHER = "一鍵啟動.command"
MAC_BUNDLE_STOPPER = "停止後端.command"
WIN_LAUNCHER = "啟動後端.bat"
DOC_NAME = "使用說明.txt"

MAC_BUNDLE_LAUNCHER_BODY = r'''#!/bin/bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
MYSQL="$DIR/mysql/bin"
MYSQL_DATA="$DIR/mysql-data"
SOCK="$DIR/run/mysql.sock"
PORT="${COLLABNOTE_DB_PORT:-13307}"
mkdir -p "$DIR/logs" "$DIR/run" "$MYSQL_DATA"
if ! "$MYSQL/mysqladmin" --socket="$SOCK" -u root ping >/dev/null 2>&1; then
  nohup "$MYSQL/mysqld" --basedir="$DIR/mysql" --datadir="$MYSQL_DATA" --port="$PORT" --socket="$SOCK" --pid-file="$DIR/run/mysqld.pid" --log-error="$DIR/logs/mysql.err" --mysqlx=0 --bind-address=127.0.0.1 >/dev/null 2>&1 &
  for _ in $(seq 1 60); do "$MYSQL/mysqladmin" --socket="$SOCK" -u root ping >/dev/null 2>&1 && break; sleep 1; done
fi
if ! "$MYSQL/mysqladmin" --socket="$SOCK" -u root ping >/dev/null 2>&1; then echo "MySQL 启动失败，请查看 logs/mysql.err" >&2; exit 1; fi
"$MYSQL/mysql" --socket="$SOCK" -u root <<'SQL'
CREATE DATABASE IF NOT EXISTS collabnote CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'collabnote'@'127.0.0.1' IDENTIFIED BY 'collabnote-local';
ALTER USER 'collabnote'@'127.0.0.1' IDENTIFIED BY 'collabnote-local';
GRANT ALL PRIVILEGES ON collabnote.* TO 'collabnote'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
export COLLABNOTE_STORAGE=mysql COLLABNOTE_DB_HOST=127.0.0.1 COLLABNOTE_DB_PORT="$PORT" COLLABNOTE_DB_NAME=collabnote COLLABNOTE_DB_USER=collabnote COLLABNOTE_DB_PASSWORD=collabnote-local
export COLLABNOTE_PORT="${COLLABNOTE_PORT:-8765}"
export COLLABNOTE_HOST="${COLLABNOTE_HOST:-0.0.0.0}"
cd "$DIR"
exec "$DIR/CollabNote-Server" "$@"
'''
MAC_BUNDLE_STOPPER_BODY = r'''#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$DIR/mysql/bin/mysqladmin" ]; then "$DIR/mysql/bin/mysqladmin" --socket="$DIR/run/mysql.sock" -u root shutdown >/dev/null 2>&1 || true; fi
pkill -f "$DIR/CollabNote-Server" >/dev/null 2>&1 || true
'''

LAUNCHER_BODY = """#!/bin/bash
# CollabNote 後端：雙擊本檔即可啟動，會開啟終端機顯示即時日誌。
# 需要指定連接埠時可先開終端機執行：./{launcher} 9000
cd "$(dirname "$0")" || exit 1
chmod +x ./{server} 2>/dev/null
xattr -d com.apple.quarantine ./{server} 2>/dev/null
exec ./{server} "$@"
"""

WIN_LAUNCHER_BODY = """@echo off
chcp 65001 >nul
title CollabNote 後端
cd /d "%~dp0"
rem 讓輸出與監控視窗正確顯示中文與框線
set PYTHONUTF8=1
rem 需要指定連接埠時：啟動後端.bat 9000
"%~dp0runtime\\python.exe" "%~dp0server.py" %*
if errorlevel 1 (
  echo.
  echo 後端已結束，按任意鍵關閉視窗...
  pause >nul
)
"""

SHARED_DOC = """資料存放位置
------------
本資料夾就是後端的資料目錄，執行後會產生：
  - users.json   帳號、密碼雜湊、好友關係
  - notes.json   筆記內文與讀寫權限
  - {data_dir}        頭像（avatars{sep}）與筆記圖片（images{sep}）
備份時直接把整個資料夾複製走即可；換電腦也只要複製這個資料夾。

變更連接埠
----------
{port_help}
"""

MAC_DOC = """CollabNote 後端（macOS x64 免安裝版）
=====================================

啟動
----
雙擊「啟動後端.command」即可。第一次雙擊若被 macOS 擋下，請在該檔上按右鍵 →「打開」。

啟動後終端機會顯示本機連線地址（http://127.0.0.1:8765）與區域網連線地址，
另外會自動開一個監控視窗（左側連線使用者、右側即時日誌）。

關閉
----
在後端視窗按 Control-C，或直接關閉該視窗，監控視窗會一起收掉。

環境需求
--------
不需要安裝 Python 或其他套件。本版本為 Intel（x86_64）執行檔，
Apple Silicon 機器會透過 Rosetta 2 執行。

""" + SHARED_DOC

WIN_DOC = """CollabNote 後端（Windows 10/11 x64 免安裝版）
=============================================

啟動
----
雙擊「啟動後端.bat」即可，會同時開出後端視窗與監控視窗。
若「啟動後端.bat」檔名在解壓縮後變成亂碼，改雙擊 start-backend.bat（內容相同）。

關閉
----
在後端視窗按 Ctrl+C，或直接關閉該視窗，監控視窗會一起收掉。

防火牆
------
第一次啟動時 Windows 可能詢問是否允許網路存取，選「允許」，
同一區域網內的其他電腦與前端才能連上。

環境需求
--------
不需要安裝 Python 或其他套件，已內含 Python 與 Pillow。
此版本為 64 位元，適用於 Windows 10/11 x64。

""" + SHARED_DOC


def download(url, dest):
    """下載檔案；python.org 版的 Python 常缺 CA bundle，憑證失敗時改用系統信任庫。"""
    try:
        _download(url, dest, None)
    except (ssl.SSLError, urllib.error.URLError):
        for candidate in ("/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt"):
            if pathlib.Path(candidate).is_file():
                _download(url, dest, ssl.create_default_context(cafile=candidate))
                return
        raise


def _download(url, dest, context):
    with urllib.request.urlopen(url, context=context) as response, open(dest, "wb") as target:
        shutil.copyfileobj(response, target)


def write_docs(target, platform_name, bundled=False):
    if bundled and platform_name != "win":
        launcher = target / MAC_BUNDLE_LAUNCHER
        launcher.write_text(MAC_BUNDLE_LAUNCHER_BODY, encoding="utf-8")
        launcher.chmod(0o755)
        stopper = target / MAC_BUNDLE_STOPPER
        stopper.write_text(MAC_BUNDLE_STOPPER_BODY, encoding="utf-8")
        stopper.chmod(0o755)
        doc = MAC_DOC.replace("啟動後端.command", "一鍵啟動.command")
    elif platform_name == "win":
        body = WIN_LAUNCHER_BODY.replace("\n", "\r\n")
        (target / WIN_LAUNCHER).write_text(body, encoding="utf-8")
        # 用解壓縮工具解到 Windows 時中文檔名偶爾會亂碼，另留一個 ASCII 入口
        (target / "start-backend.bat").write_text(body, encoding="utf-8")
        doc = WIN_DOC.format(data_dir="data\\", sep="\\",
                             port_help="在命令提示字元執行：啟動後端.bat 9000\n或先設定環境變數 COLLABNOTE_PORT。")
    else:
        launcher = target / MAC_LAUNCHER
        launcher.write_text(LAUNCHER_BODY.format(launcher=MAC_LAUNCHER, server=SERVER_NAME), encoding="utf-8")
        launcher.chmod(0o755)
        doc = MAC_DOC.format(data_dir="data/", sep="/",
                             port_help="./啟動後端.command 9000\n或設定環境變數：COLLABNOTE_PORT=9000 ./啟動後端.command")
    (target / DOC_NAME).write_text(doc, encoding="utf-8")


def copy_runtime_data(target, include_data=True):
    if not include_data:
        return
    source = REPO / "data"
    if source.is_dir():
        shutil.copytree(source, target / "data", symlinks=True)


def copy_mysql_bundle(target, mysql_dir, empty_data=False):
    mysql_dir = pathlib.Path(mysql_dir).expanduser().resolve()
    binary_source = mysql_dir / "mysql-8.4.11-macos15-x86_64"
    data_source = mysql_dir / "data"
    if not binary_source.is_dir():
        raise RuntimeError(f"找不到 MySQL runtime：{binary_source}")
    if not data_source.is_dir() and not empty_data:
        raise RuntimeError(f"找不到 MySQL 数据目录：{data_source}")
    shutil.copytree(binary_source, target / "mysql", symlinks=True)
    if empty_data:
        (target / "mysql-data").mkdir()
        subprocess.run([str(target / "mysql" / "bin" / "mysqld"), "--initialize-insecure", f"--basedir={target / 'mysql'}", f"--datadir={target / 'mysql-data'}", f"--log-error={target / 'mysql-data' / 'init.err'}"], check=True)
    else:
        shutil.copytree(data_source, target / "mysql-data", symlinks=True)
    (target / "run").mkdir()
    (target / "logs").mkdir()


def build_frozen(out_dir, mysql_dir=None, empty_data=False):
    """用 PyInstaller 把 server.py 凍結成可攜資料夾（macOS / Linux）。"""
    arch = platform.machine()
    system = "macOS" if sys.platform == "darwin" else platform.system()
    target = out_dir / ("CollabNote-Backend-%s-%s" % (system, arch))
    if target.exists():
        shutil.rmtree(target)

    work = pathlib.Path(tempfile.mkdtemp(prefix="collabnote-build-"))
    try:
        venv = work / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        print("· 安裝打包工具（PyInstaller、Pillow）…")
        subprocess.run([str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--quiet", "pyinstaller", *PYTHON_PACKAGES], check=True)

        print("· 凍結後端執行檔…")
        subprocess.run([
            str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
            "--name", SERVER_NAME, "--hidden-import", "monitor",
            "--distpath", str(work / "dist"), "--workpath", str(work / "work"),
            "--specpath", str(work), str(REPO / "server.py"),
        ], check=True)

        built = work / "dist" / SERVER_NAME
        target.mkdir(parents=True)
        for item in built.iterdir():
            shutil.move(str(item), str(target / item.name))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # PyInstaller 不會自動帶入外部 schema；MySQL 模式首次啟動需要它。
    for name in ("database.py", "schema.sql"):
        shutil.copy2(REPO / name, target / name)
    if mysql_dir:
        copy_runtime_data(target)
        copy_mysql_bundle(target, mysql_dir, empty_data=empty_data)
        write_docs(target, "mac" if sys.platform == "darwin" else "posix", bundled=True)
    else:
        write_docs(target, "mac" if sys.platform == "darwin" else "posix")
    return target


def build_embed(out_dir):
    """用官方嵌入式 Python 組裝 Windows 可攜版（任何平台都能執行這個步驟）。"""
    target = out_dir / "CollabNote-Backend-Windows-x64"
    if target.exists():
        shutil.rmtree(target)
    runtime = target / "runtime"
    runtime.mkdir(parents=True)

    work = pathlib.Path(tempfile.mkdtemp(prefix="collabnote-embed-"))
    try:
        archive = work / "python-embed.zip"
        print("· 下載嵌入式 Python %s…" % EMBED_VERSION)
        download(EMBED_URL, archive)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(runtime)

        print("· 下載 Pillow（win_amd64）…")
        wheels = work / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--only-binary=:all:",
                        "--platform", "win_amd64", "--python-version", EMBED_VERSION.rsplit(".", 1)[0],
                        "--implementation", "cp", "--no-deps", *PYTHON_PACKAGES, "-d", str(wheels)], check=True)
        site_packages = runtime / "Lib" / "site-packages"
        site_packages.mkdir(parents=True)
        for wheel in wheels.glob("*.whl"):
            with zipfile.ZipFile(wheel) as bundle:
                bundle.extractall(site_packages)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # 嵌入式 Python 預設看不到 site-packages，靠 ._pth 加入
    (runtime / "python312._pth").write_text(
        "python312.zip\r\n.\r\nLib\\site-packages\r\n", encoding="ascii")

    for name in ("server.py", "monitor.py", "database.py", "schema.sql"):
        shutil.copy2(REPO / name, target / name)
    write_docs(target, "win")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="打包免安裝的 CollabNote 後端")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="輸出目錄（預設 dist/backend）")
    parser.add_argument("--platform", default="auto", choices=["auto", "mac", "win"],
                        help="目標平台；auto 依目前系統決定")
    parser.add_argument("--mysql-dir", default=None, help="MySQL runtime 所在目录；macOS 默认使用工作区内的版本")
    parser.add_argument("--empty-data", action="store_true", help="生成空白 MySQL 数据目录")
    args = parser.parse_args(argv)

    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    default_mysql = next((path for path in MYSQL_DIR_CANDIDATES if path.is_dir()), None)
    mysql_dir = args.mysql_dir or (str(default_mysql) if default_mysql and args.platform in ("auto", "mac") else None)
    if args.platform == "win":
        targets = [build_embed(out_dir)]
    elif args.platform == "mac":
        targets = [build_frozen(out_dir, mysql_dir=mysql_dir, empty_data=args.empty_data)]
    elif sys.platform == "win32":
        targets = [build_embed(out_dir)]
    else:
        targets = [build_frozen(out_dir, mysql_dir=mysql_dir, empty_data=args.empty_data)]

    for target in targets:
        print("✓ 完成：%s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
