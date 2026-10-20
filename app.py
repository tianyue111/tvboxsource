#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TVBox 自建源 —— Unraid / Docker 一体化服务

功能:
  * 启动即生成一次源, 之后每隔 UPDATE_INTERVAL_HOURS 小时自动重新生成
  * 通过 HTTP 提供 tvbox.json / tvbox_full.json / tvbox_multi.json
  * TVBox 客户端配置地址: http://<Unraid-IP>:8787/tvbox.json

环境变量:
  DATA_DIR                输出目录(GitHub JSON 落地处), 默认 /data
  PORT                    HTTP 端口, 默认 8787
  UPDATE_INTERVAL_HOURS   更新间隔(小时), 默认 24
  RUN_ON_START            启动时是否立即更新, 默认 1
  CF_PROXY                可选 Cloudflare Worker 代理
"""
from __future__ import annotations

import http.server
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8787"))
INTERVAL_H = float(os.environ.get("UPDATE_INTERVAL_HOURS", "24"))
RUN_ON_START = os.environ.get("RUN_ON_START", "1") == "1"

_update_lock = threading.Lock()
_last = {"started": None, "finished": None, "ok": None}


def run_update() -> None:
    with _update_lock:
        _last["started"] = datetime.now().isoformat(timespec="seconds")
        print(f"[scheduler] 开始更新 @ {_last['started']}", flush=True)
        env = dict(os.environ)
        env["OUT_DIR"] = DATA_DIR
        try:
            subprocess.run([sys.executable, os.path.join(BASE, "update.py")],
                           env=env, check=True)
            _last["ok"] = True
        except Exception as e:  # noqa: BLE001
            print(f"[scheduler] 更新失败: {e}", flush=True)
            _last["ok"] = False
        finally:
            _last["finished"] = datetime.now().isoformat(timespec="seconds")


def scheduler() -> None:
    if RUN_ON_START:
        run_update()
    interval = max(300.0, INTERVAL_H * 3600)
    while True:
        time.sleep(interval)
        run_update()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DATA_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.rstrip("/") == "/update":
            threading.Thread(target=run_update, daemon=True).start()
            self.send_response(202)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"update started\n")
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, fmt, *args):  # 精简日志
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=scheduler, daemon=True).start()
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"[server] 监听 0.0.0.0:{PORT}, 数据目录 {DATA_DIR}", flush=True)
        print(f"[server] TVBox 配置: http://<本机IP>:{PORT}/tvbox.json", flush=True)
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
