#!/usr/bin/env python3
"""
在 monitor 所在机器上完成 Google Health 授权（浏览器不必能访问 monitor）。

1. python google_auth.py start           打印授权地址，在任意浏览器打开并同意
2. 浏览器最终跳到打不开的 localhost 回调页，复制地址栏完整网址
3. python google_auth.py finish '<网址>'  把回调参数交给本机 monitor 换取 token
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.parse
import urllib.request


MONITOR = f"http://127.0.0.1:{os.getenv('FITBIT_MONITOR_PORT', '18765')}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _start() -> int:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(f"{MONITOR}/auth/start", timeout=8)
    except urllib.error.HTTPError as redirect:
        location = redirect.headers.get("location")
        if redirect.code in (302, 307) and location:
            print(location)
            return 0
        raise
    print("monitor 未返回授权跳转", file=sys.stderr)
    return 1


def _finish(callback_url: str) -> int:
    query = urllib.parse.urlsplit(callback_url).query
    if "code=" not in query:
        print("网址里没有 code 参数，请复制授权完成后地址栏的完整网址", file=sys.stderr)
        return 1
    try:
        with urllib.request.urlopen(f"{MONITOR}/oauth/callback?{query}", timeout=30) as r:
            print("Google Health 已连接" if r.status == 200 else f"授权失败: HTTP {r.status}")
            return 0 if r.status == 200 else 1
    except urllib.error.HTTPError as e:
        print(f"授权失败: HTTP {e.code}（start 之后 monitor 若重启过，需要重新 start）", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    if argv[1:2] == ["start"]:
        return _start()
    if argv[1:2] == ["finish"] and len(argv) == 3:
        return _finish(argv[2])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
