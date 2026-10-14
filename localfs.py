#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地文件服务

为什么需要它：
    WebView2 里用 file:// 加载音频/字幕并 fetch 会很不可靠（跨源限制、路径编码坑）。
    起一个只监听 127.0.0.1 的小服务，把「界面目录」和「库目录」以普通 HTTP 提供，
    音频播放和字幕读取就都变成标准 HTTP 请求，稳定且好调试。

安全约束：
    - 只绑定 127.0.0.1，不对外网开放
    - 只允许访问「显式允许的目录」内的文件（路径先 resolve 再校验前缀）
    - 只允许 GET/HEAD
"""
import json
import mimetypes
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

mimetypes.add_type('text/vtt', '.vtt')
mimetypes.add_type('audio/mpeg', '.mp3')
mimetypes.add_type('text/markdown; charset=utf-8', '.md')


class Handler(SimpleHTTPRequestHandler):
    allowed_roots = []
    quiet = True

    def log_message(self, fmt, *args):
        if not self.quiet:
            print('[http]', fmt % args)

    def _resolve(self):
        """把请求路径解析成真实文件；越界或不存在就返回 None。

        关键点：必须**确认命中一个真实文件**才能返回。
        之前写成「路径落在某个 root 内就 return」，结果请求库里的文件时，
        第一个 root（界面目录）下那个同名路径也算"合法"，
        于是直接返回了界面目录里并不存在的文件，永远 404。
        """
        from urllib.parse import unquote, urlparse
        rel = unquote(urlparse(self.path).path).lstrip('/')
        if not rel or rel.endswith('/'):
            rel = (rel + 'index.html').lstrip('/')
        for root in Handler.allowed_roots:
            root = Path(root)
            try:
                cand = (root / rel).resolve()
                if not cand.is_relative_to(root.resolve()):
                    continue                      # 越界，试下一个 root
                if cand.is_file():
                    return cand                   # 命中真实文件才返回
            except Exception:
                continue
        return None

    def do_GET(self):
        target = self._resolve()
        if target is None or not target.is_file():
            self.send_error(404, 'Not found')
            return
        try:
            data = target.read_bytes()
        except Exception as e:
            self.send_error(500, str(e))
            return
        ctype = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        rng = self.headers.get('Range')
        if rng and ctype.startswith('audio'):
            # 支持 Range，音频拖动进度条要靠它
            try:
                unit, _, spec = rng.partition('=')
                s, _, e = spec.partition('-')
                start = int(s) if s else 0
                end = int(e) if e else len(data) - 1
                end = min(end, len(data) - 1)
                chunk = data[start:end + 1]
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                self.send_header('Content-Length', str(len(chunk)))
                self.end_headers()
                self.wfile.write(chunk)
                return
            except Exception:
                pass
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()


class LocalServer:
    def __init__(self, roots, port=0):
        self.roots = [Path(r) for r in roots if r and Path(r).exists()]
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        H = Handler
        H.allowed_roots = list(self.roots)
        H.quiet = True
        self.httpd = ThreadingHTTPServer(('127.0.0.1', self.port), partial(H))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.port

    def set_roots(self, roots):
        Handler.allowed_roots = [Path(r) for r in roots if r and Path(r).exists()]

    def base(self):
        return f'http://127.0.0.1:{self.port}'

    def url_for(self, path):
        """把绝对路径换成 HTTP URL（取第一个能装下它的允许根）。

        注意：文件名里常有空格、中文、括号，**必须做百分号编码**，
        否则 urllib / 浏览器会直接报 InvalidURL 或 400。
        """
        from urllib.parse import quote
        p = Path(path).resolve()
        for root in Handler.allowed_roots:
            try:
                if p.is_relative_to(root.resolve()):
                    rel = p.relative_to(root.resolve()).as_posix()
                    return f'{self.base()}/{quote(rel)}'
            except Exception:
                continue
        return ''

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass
