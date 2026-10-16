# -*- coding: utf-8 -*-
"""从百田官方 CDN 抓取全部副本刷怪脚本 SWF（play/aiscript/fb<fbId>~1000.swf）。

背景
----
《龙斗士》客户端进副本时 `FbSourceLoader.getFBAIScript(fbId)` 会拉
`aiscript/fb<fbId>`，该 SWF 内嵌 3 个 XML（小怪属性 / 怪物技能帧 / 怪物 AI）。
本机原本只有进过的 fb10 / fb14 / fb16，其余副本靠这里补齐。

用法
----
    python server/fetch_aiscript_swf.py           # fb1~fb41
    python server/fetch_aiscript_swf.py 1 60      # 指定区间试探

输出：07-swf/downloaded_core/play/aiscript/fb<fbId>~1000.swf
CDN 走底层 socket 直连真实节点，绕过本地 hosts 劫持。
"""
from __future__ import annotations

import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CDN_IPS = ("183.215.57.92", "111.6.13.20", "183.204.229.21")
CDN_HOST = "lds.100bt.com"
SWF_MAGIC = (b"FWS", b"CWS", b"ZWS")

WS = Path(__file__).resolve().parent.parent
OUT = WS / "07-swf" / "downloaded_core" / "play" / "aiscript"
FB_MAX = 41          # FbConfig 的 fbId 上限；>41 官方无 aiscript

_i = 0
_lk = threading.Lock()


def next_ip() -> str:
    global _i
    with _lk:
        ip = CDN_IPS[_i % len(CDN_IPS)]
        _i += 1
        return ip


def fetch(path: str, timeout: int = 15, retries: int = 3):
    clean = path if path.startswith("/") else "/" + path
    for _ in range(retries):
        try:
            s = socket.create_connection((next_ip(), 80), timeout=timeout)
            s.sendall((f"GET {clean} HTTP/1.1\r\nHost: {CDN_HOST}\r\n"
                       "User-Agent: Mozilla/5.0 (Windows NT 6.1) FlashPlayer\r\n"
                       "Connection: close\r\n\r\n").encode())
            buf = bytearray()
            while True:
                d = s.recv(65536)
                if not d:
                    break
                buf.extend(d)
                if len(buf) > 8 * 1024 * 1024:
                    break
            s.close()
            head, _, body = bytes(buf).partition(b"\r\n\r\n")
            if not head.startswith(b"HTTP/1.1 200"):
                return None
            return body if body[:3] in SWF_MAGIC else None
        except Exception:
            continue
    return None


def try_one(fb_id: int):
    for cand in (f"play/aiscript/fb{fb_id}~1000.swf", f"play/aiscript/fb{fb_id}.swf"):
        data = fetch(cand)
        if data:
            (OUT / f"fb{fb_id}~1000.swf").write_bytes(data)
            return fb_id, cand, len(data)
    return fb_id, None, 0


def main(argv) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lo, hi = 1, FB_MAX
    if len(argv) > 2:
        lo, hi = int(argv[1]), int(argv[2])
    ok, miss = [], []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for fb_id, cand, size in ex.map(try_one, range(lo, hi + 1)):
            (ok if cand else miss).append(fb_id)
            print(f"  {'✅' if cand else '· '} fb{fb_id:<3} "
                  + (f"{size:>7} B  <- {cand}" if cand else "无"))
    print(f"\n命中 {len(ok)}/{hi - lo + 1}  缺失 {miss}")
    print(f"输出目录: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
