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
SERVER_NAME = "CollabNote-Server"
EMBED_VERSION = "3.12.10"
EMBED_URL = ("https://registry.npmmirror.com/-/binary/python/%s/python-%s-embed-amd64.zip"
             % (EMBED_VERSION, EMBED_VERSION))
PILLOW_SPEC = "Pillow>=10.0"

MAC_LAUNCHER = "啟動後端.command"
WIN_LAUNCHER = "啟動後端.bat"
DOC_NAME = "使用說明.txt"

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


def write_docs(target, platform_name):
    if platform_name == "win":
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


def build_frozen(out_dir):
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
        subprocess.run([str(python), "-m", "pip", "install", "--quiet", "pillow", "pyinstaller"], check=True)

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
                        "--implementation", "cp", "--no-deps", PILLOW_SPEC, "-d", str(wheels)], check=True)
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

    for name in ("server.py", "monitor.py"):
        shutil.copy2(REPO / name, target / name)
    write_docs(target, "win")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="打包免安裝的 CollabNote 後端")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="輸出目錄（預設 dist/backend）")
    parser.add_argument("--platform", default="auto", choices=["auto", "mac", "win"],
                        help="目標平台；auto 依目前系統決定")
    args = parser.parse_args(argv)

    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.platform == "win":
        targets = [build_embed(out_dir)]
    elif args.platform == "mac":
        targets = [build_frozen(out_dir)]
    elif sys.platform == "win32":
        targets = [build_embed(out_dir)]
    else:
        targets = [build_frozen(out_dir)]

    for target in targets:
        print("✓ 完成：%s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
