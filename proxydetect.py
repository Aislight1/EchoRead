# -*- coding: utf-8 -*-
"""本机代理环境探测（参考开源工具惯例：env → 系统代理 → 常见端口 → 连通验证）。

detect_proxy() 返回 {'proxy': 'http://127.0.0.1:7890' 或 '', 'via': 来源, 'verified': 是否实测可用}
- verified=False 的候选（端口开着但没验证通过）也会返回，调用方自行决定是否使用。
"""
import json
import os
import re
import socket
import subprocess
from pathlib import Path

# 常见本地 HTTP 代理端口（Clash/FlClash 7890 · Clash Verge Rev 7897 ·
# v2rayN 10809 · Privoxy 8118 · Netch 8889 · sing-box 惯例 2080）
COMMON_PORTS = (7890, 7897, 10809, 8118, 8889, 2080)

# 用来验证代理真的能出国际网的轻量目标（Connection 209 / 204 / 200 都算通）
VERIFY_URL = 'https://www.gstatic.com/generate_204'


def _port_open(host='127.0.0.1', port=0, timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _verify(proxy, timeout=5):
    """用 curl 走该代理访问一个 204 端点，通了才算数。"""
    try:
        r = subprocess.run(
            ['curl', '-x', proxy, '-s', '-o', 'NUL', '-w', '%{http_code}',
             '--max-time', str(timeout), VERIFY_URL],
            capture_output=True, timeout=timeout + 10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        code = (r.stdout.decode('ascii', 'replace') or '').strip()
        return code in ('200', '204')
    except Exception:
        return False


def _from_env():
    for k in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY'):
        v = (os.environ.get(k) or '').strip()
        if v.startswith('http://'):
            return v
    return ''


def _from_system_proxy():
    """读 Windows 系统代理（Clash/V2Ray 开「系统代理」就写在这里）。"""
    if os.name != 'nt':
        return ''
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        enabled, _ = winreg.QueryValueEx(k, 'ProxyEnable')
        server, _ = winreg.QueryValueEx(k, 'ProxyServer')
        winreg.CloseKey(k)
        if not enabled or not server:
            return ''
        server = server.strip()
        # ProxyServer 可能是 "127.0.0.1:7890" 或 "http=...;https=...;socks=..."
        if '=' in server:
            m = re.search(r'(?:^|;)(?:https?|http)=([^;]+)', server, re.I)
            server = m.group(1) if m else ''
        if not server:
            return ''
        if not server.startswith('http'):
            server = 'http://' + server
        return server
    except Exception:
        return ''


def _from_ports():
    for p in COMMON_PORTS:
        if _port_open(port=p):
            return f'http://127.0.0.1:{p}'
    return ''


def detect_proxy(log=None):
    """按 env → 系统代理 → 常见端口 找一个可用的国际网代理。

    找到候选后实测（generate_204）；实测不通也给出来但 verified=False。
    什么都没有返回 {'proxy': '', 'via': '', 'verified': False}。
    """
    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    cands = [(_from_env(), '环境变量'), (_from_system_proxy(), '系统代理')] + \
            [(f'http://127.0.0.1:{p}', f'端口探测:{p}') for p in COMMON_PORTS
             if _port_open(port=p)]
    seen = set()
    for proxy, via in cands:
        if not proxy or proxy in seen:
            continue
        seen.add(proxy)
        say(f'发现代理候选（{via}）：{proxy}，验证中…')
        if _verify(proxy):
            say(f'代理可用（{via}）：{proxy}')
            return {'proxy': proxy, 'via': via, 'verified': True}
        say(f'候选不通：{proxy}')
    for proxy, via in cands:
        if proxy:
            return {'proxy': proxy, 'via': via, 'verified': False}
    return {'proxy': '', 'via': '', 'verified': False}


if __name__ == '__main__':
    try:
        import sys
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    print(json.dumps(detect_proxy(), ensure_ascii=False, indent=1))
