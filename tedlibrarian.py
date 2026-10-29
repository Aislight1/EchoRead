#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
听与读 · 核心逻辑

职责：
  - 下载 TED 演讲（音频 MP3 + 英中字幕 + 中英对照 + 纯文本）
  - 扫描本地库，把散落的文件配成一条条「语料」
  - 字幕格式转换（SRT -> VTT，给播放器用）
  - 索引与设置的读写

设计原则：
  - 纯标准库 + ffmpeg + curl，**不依赖任何 AI / 云服务**，完全离线可用
  - 所有文件操作都限制在「库目录」内，删除前一律要求显式确认
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 控制台是 GBK，日志里带 ✅ 之类字符会崩，统一转 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

def _app_dir():
    """程序所在目录（用户视角的「程序目录」，不是 PyInstaller 的解包目录）。

    坑：PyInstaller 6.x 的 onedir 模式里，**`__file__` 也落在 _internal 下**，
    所以不能拿 __file__ 推目录 —— 那会把 config.json 写进 _internal，
    用户既找不到也改不了。冻结时必须用 sys.executable 的所在目录。
    """
    import sys
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _app_dir()
CONFIG = APP_DIR / 'config.json'

# 源码目录（冻结时 __file__ 在 _internal 里，源码目录是它的上一层）
SRC_DIR = Path(__file__).resolve().parent

# 语料相关后缀（扫描时按这些配对）
AUDIO_EXT = {'.mp3', '.m4a', '.opus', '.wav', '.aac', '.flac'}
SUB_EXT = {'.srt', '.vtt'}


def _no_window():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


# ---------------------------------------------------------------- 外部工具
def find_ffmpeg():
    """找 ffmpeg：PATH → winget 的 Gyan.FFmpeg 安装（版本号变化用通配兼容）。"""
    cands = ['ffmpeg']
    local = os.environ.get('LOCALAPPDATA')
    if local:
        pkgs = Path(local) / 'Microsoft' / 'WinGet' / 'Packages'
        try:
            for d in pkgs.glob('Gyan.FFmpeg*'):
                cands += [str(x) for x in d.rglob('ffmpeg.exe')]
        except Exception:
            pass
    for c in cands:
        try:
            r = subprocess.run([c, '-version'], capture_output=True, creationflags=_no_window())
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


FFMPEG = find_ffmpeg()


def curl_text(url, timeout=60, retries=3):
    """抓网页文本。TED 偶发返回空响应（Bot 拦截/线路抖动），自动重试。"""
    for i in range(max(1, retries)):
        r = subprocess.run(['curl.exe', '-sSL', '--max-time', str(timeout), '-A', UA, url],
                           capture_output=True, timeout=timeout + 20, creationflags=_no_window())
        txt = r.stdout.decode('utf-8', errors='replace') if r.returncode == 0 else ''
        if txt.strip():
            return txt
        time.sleep(2 * (i + 1))
    return ''


# ---------------------------------------------------------------- 配置
def _guess_library():
    """猜一个合理的默认库目录。

    打包后程序跑到 dist\\听与读\\ 下，如果默认成那里的 downloads，
    用户之前下好的语料就等于「看不见」了——所以优先复用已经存在的库。
    """
    cands = [
        APP_DIR / 'downloads',
        APP_DIR.parent.parent / 'downloads',        # 打包后回到源码目录
        Path.home() / '听与读' / 'downloads',
    ]
    for c in cands:
        try:
            if c.is_dir() and any(c.glob('*.mp3')):
                return str(c)
        except Exception:
            continue
    return str(APP_DIR / 'downloads')


DEFAULT_CONFIG = {
    'libraryDir': _guess_library(),
    'extraDirs': [],
    'openFolderAfterDownload': False,
    'lastUrl': '',
    'downloadProxy': 'auto',    # auto = 自动探测系统/Clash 代理；'直连' = 不走代理；或显式 http://...
}


def load_config():
    """读配置。

    冻结后程序目录（exe 旁）可能还没有 config.json，这时回头找源码目录里那份，
    这样打包前设置好的库目录能自动继承，不会"打回默认"。
    """
    cfg = dict(DEFAULT_CONFIG)
    for p in (CONFIG, SRC_DIR / 'config.json'):
        try:
            if p.exists():
                cfg.update(json.loads(p.read_text(encoding='utf-8')))
                break
        except Exception:
            continue
    return cfg


def save_config(cfg):
    try:
        import store
        store.write_json(CONFIG, cfg)
    except Exception:
        # 程序目录不可写时（比如装在 Program Files），退回用户目录
        try:
            alt = Path.home() / '.ted-library' / 'config.json'
            alt.parent.mkdir(parents=True, exist_ok=True)
            alt.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding='utf-8')
        except Exception:
            pass


# ---------------------------------------------------------------- 下载
def slugify(s):
    s = re.sub(r'[\\/:*?"<>|]+', '_', str(s))
    s = re.sub(r'\s+', ' ', s).strip().rstrip('.')
    return s[:100] or 'ted'


def parse_ted_page(url):
    """解析 TED 页面，返回 标题/时长/音频直链/逐句文稿。"""
    html = curl_text(url)
    if not html:
        raise RuntimeError('页面抓取失败（网络或链接问题）')
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError('页面结构变了：找不到 __NEXT_DATA__（TED 可能又改版了）')
    data = json.loads(m.group(1))
    pp = (data.get('props') or {}).get('pageProps') or {}

    vd = pp.get('videoData') or {}
    title = vd.get('title') or slugify(url.rstrip('/').split('/')[-1])
    duration = vd.get('duration')

    blob = json.dumps(vd, ensure_ascii=False)
    mp4s = re.findall(r'https?://[^"]+\.mp4[^"]*', blob)
    audio_url = mp4s[0] if mp4s else None
    if not audio_url:
        hls = re.findall(r'https?://[^"]+\.m3u8[^"]*', blob)
        audio_url = hls[0] if hls else None
    if not audio_url:
        raise RuntimeError('这个页面里没找到可下载的音视频流')

    cues = extract_cues(pp.get('transcriptData') or {}, duration)

    # TED 每页只给一种语言，中英对照要抓两次
    has_cn = any(('zh' in (k or '').lower() or 'cn' in (k or '').lower()) for k in cues)
    if cues and not has_cn:
        base = re.sub(r'([?&])language=[^&]*', r'\1', url).rstrip('?&')
        sep = '&' if '?' in base else '?'
        html2 = curl_text(f'{base}{sep}language=zh-cn')
        if html2:
            m2 = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html2, re.S)
            if m2:
                try:
                    pp2 = (json.loads(m2.group(1)).get('props') or {}).get('pageProps') or {}
                    cues.update(extract_cues(pp2.get('transcriptData') or {}, duration))
                except Exception:
                    pass

    return {'title': title, 'duration': duration, 'audio': audio_url, 'cues': cues, 'url': url}


def extract_cues(td, duration=None):
    """从 transcriptData 取 {语言码: [{start,end,text}]}。

    结构：translation.language.internalLanguageCode + translation.paragraphs[].cues[].{text,time}
    坑：cue.time 单位是**毫秒**；且 cue 只给起点，终点要用下一句起点补。
    """
    if not isinstance(td, dict):
        return {}
    tr = td.get('translation')
    if not isinstance(tr, dict):
        return {}
    lang_node = tr.get('language')
    lang = None
    if isinstance(lang_node, dict):
        lang = lang_node.get('internalLanguageCode') or lang_node.get('englishName')

    items = []
    for para in (tr.get('paragraphs') or []):
        if not isinstance(para, dict):
            continue
        for c in (para.get('cues') or []):
            if not isinstance(c, dict):
                continue
            txt = (c.get('text') or '').strip()
            if not txt:
                continue
            try:
                start = float(c.get('time') or 0) / 1000.0     # 毫秒 -> 秒
            except (TypeError, ValueError):
                start = 0.0
            items.append({'start': start, 'end': 0.0, 'text': txt})

    if not items:
        return {}
    items.sort(key=lambda x: x['start'])
    for i, it in enumerate(items):
        if i + 1 < len(items):
            it['end'] = items[i + 1]['start']
        else:
            it['end'] = float(duration) if duration else it['start'] + 4.0
        if it['end'] <= it['start']:
            it['end'] = it['start'] + 2.0
    return {(lang or 'unknown'): items}


def pick_langs(cues):
    """挑出英文轨和中文轨。"""
    def best(key):
        cand = []
        for lg, items in cues.items():
            l = (lg or '').lower()
            if key == 'en' and l.startswith('en'):
                cand.append((3, items))
            elif key == 'zh' and ('zh' in l or 'cn' in l):
                cand.append((3, items))
        return max(cand, default=(0, None))[1]
    return best('en'), best('zh')


def fmt_ts(sec, srt=True):
    total = max(0.0, float(sec))
    # 先整体四舍五入到毫秒再拆分。旧写法 `int((total-int(total))*1000+0.5)` 在
    # total=5.9996 这类值上会得到 ms=1000，写出非法 SRT "00:00:05,1000"
    # （毫秒字段必须 000-999，且秒未进位）——解析器会读错或直接拒收。
    total_ms = int(total * 1000 + 0.5)            # 四舍五入到毫秒（消除浮点↔3位毫秒往返漂移）
    h, rem = divmod(total_ms // 1000, 3600)
    m, s = divmod(rem, 60)
    ms = total_ms % 1000
    if not srt:
        return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def write_srt(path, cues):
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{fmt_ts(c['start'])} --> {fmt_ts(c['end'])}\n{c['text']}\n")
    path.write_text('\n'.join(out), encoding='utf-8')
    return len(cues)


def srt_to_vtt(srt_path, vtt_path):
    """SRT -> WebVTT，供 HTML5 播放器用。已存在且不旧则跳过。"""
    if vtt_path.exists() and vtt_path.stat().st_mtime >= srt_path.stat().st_mtime:
        return vtt_path
    txt = srt_path.read_text(encoding='utf-8', errors='replace')
    txt = txt.replace('\r\n', '\n')
    txt = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', txt)   # 逗号 -> 点
    vtt_path.write_text('WEBVTT\n\n' + txt, encoding='utf-8')
    return vtt_path


def _ts_to_sec(t):
    """`00:01:02.345` 或 `01:02.345` -> 秒。"""
    parts = t.strip().split(':')
    try:
        vals = [float(x) for x in parts]
    except ValueError:
        return 0.0
    if len(vals) == 3:
        return vals[0] * 3600 + vals[1] * 60 + vals[2]
    if len(vals) == 2:
        return vals[0] * 60 + vals[1]
    return vals[0] if vals else 0.0


def parse_vtt_cues(text):
    """解析 WebVTT 文本 -> [{'start','end','text'}]。

    逐句练习要按句取文本，所以这里需要一个稳定的解析器
    （前端那份在 JS 里，Python 侧也要一份）。
    """
    out = []
    blocks = re.split(r'\n\s*\n', (text or '').replace('\r\n', '\n'))
    for b in blocks:
        lines = [x for x in b.split('\n') if x.strip() != '']
        if not lines:
            continue
        if lines[0].strip().upper().startswith('WEBVTT'):
            lines = lines[1:]
            if not lines:
                continue
        i = 0
        if '-->' not in lines[0]:
            i = 1                      # 跳过序号行
        if i >= len(lines) or '-->' not in lines[i]:
            continue
        a, _, rest = lines[i].partition('-->')
        end_raw = rest.strip().split(' ')[0]
        body = ' '.join(lines[i + 1:]).strip()
        body = re.sub(r'<[^>]+>', '', body)          # 去掉 <c> 之类标签
        if not body:
            continue
        out.append({'start': _ts_to_sec(a), 'end': _ts_to_sec(end_raw), 'text': body})
    return out


def merge_bilingual(en, zh):
    """中英配对：每句中文按「重叠最长」分配给某句英文（多对一），并去掉紧邻重复。"""
    if not en:
        return [(None, [c]) for c in (zh or [])]
    if not zh:
        return [(c, []) for c in en]
    buckets = {i: [] for i in range(len(en))}
    for z in zh:
        best_i, best_ov = None, 0.0
        for i, c in enumerate(en):
            ov = min(z['end'], c['end']) - max(z['start'], c['start'])
            if ov > best_ov:
                best_ov, best_i = ov, i
        if best_i is None:
            best_i = min(range(len(en)), key=lambda i: abs(en[i]['start'] - z['start']))
        buckets[best_i].append(z)
    pairs, last = [], None
    for i, c in enumerate(en):
        seg = []
        for z in buckets[i]:
            if z['text'] == last:
                continue
            seg.append(z)
            last = z['text']
        pairs.append((c, seg))
    return pairs


def write_bilingual(path, en, zh, title, meta):
    rows = [f'# {title}', '']
    if meta:
        rows += [meta, '']
    rows += ['---', '']
    for c, seg in merge_bilingual(en, zh):
        if c is not None:
            rows.append(f'**[{fmt_ts(c["start"], srt=False)}]** {c["text"]}')
        if seg:
            joined = ' '.join(s['text'] for s in seg)
            rows.append(f'> {joined}' if c is not None
                        else f'**[{fmt_ts(seg[0]["start"], srt=False)}]** {joined}')
        rows.append('')
    path.write_text('\n'.join(rows), encoding='utf-8')
    return len(rows)


def _win_suspend_threads(pid, suspend=True):
    """挂起 / 恢复一个进程的全部线程（Windows）。用于实现下载「暂停」。"""
    TH32CS_SNAPTHREAD = 0x4
    THREAD_SUSPEND_RESUME = 0x0002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    k32 = ctypes.windll.kernel32

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong),
                    ('th32ThreadID', ctypes.c_ulong), ('th32OwnerProcessID', ctypes.c_ulong),
                    ('tpBasePri', ctypes.c_long), ('tpDeltaPri', ctypes.c_long),
                    ('dwFlags', ctypes.c_ulong)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return False
    n = 0
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(THREADENTRY32)
        found = bool(k32.Thread32First(snap, ctypes.byref(te)))
        while found:
            if te.th32OwnerProcessID == pid:
                h = k32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                if h:
                    if suspend:
                        k32.SuspendThread(h)
                    else:
                        k32.ResumeThread(h)
                    k32.CloseHandle(h)
                    n += 1
            found = bool(k32.Thread32Next(snap, ctypes.byref(te)))
    finally:
        k32.CloseHandle(snap)
    return n > 0


def download_mp3(audio_url, dest, bitrate='128k', log=None, duration=None,
                 control=None, proxy='', progress=None):
    """只抽音频转 MP3，不保存视频。

    duration（秒）：把 ffmpeg 进度换算成百分比用；
    control：{'pause': threading.Event, 'cancel': bool}，调用方持有。
             暂停 = SuspendThread 挂起 ffmpeg 全部线程（真暂停，瞬时生效）；
    progress：fn(info_dict) 实时回报 {'percent','doneSec','totalSec','speedX'}。
    进度来源：stderr 的 stats 行（stderr 无缓冲立即可读；ffmpeg 8 往管道写
    -progress 会块缓冲十几秒才吐，不能用）。
    """
    if not FFMPEG:
        raise RuntimeError('找不到 ffmpeg，请先安装：winget install Gyan.FFmpeg')
    tmp = dest.with_suffix('.part.mp3')
    # 进度走 stderr 的 stats 行（stderr 无缓冲、立即可读）；stdout 保留给管道反压做「暂停」
    cmd = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-stats', '-y']
    if proxy:
        cmd += ['-http_proxy', proxy]   # 注意：ffmpeg 8 的选项名是 -http_proxy，不是 -proxy
    cmd += ['-i', audio_url, '-vn', '-acodec', 'libmp3lame', '-b:a', bitrate, str(tmp)]

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=_no_window())

    state = {'out_sec': 0.0, 'err_tail': bytearray(), 'cancelled': False}

    def _drain_err():
        """收 stderr：抓 time=HH:MM:SS.ms 作进度，滚动保留错误尾巴。"""
        tail = b''
        try:
            for chunk in iter(lambda: proc.stderr.read1(4096), b''):
                if not chunk:
                    continue
                state['err_tail'] = (state['err_tail'] + chunk)[-4096:]
                tail = (tail + chunk)[-4096:]
                for m in re.finditer(rb'time=(\d+):(\d+):(\d+(?:\.\d+)?)', tail):
                    try:
                        state['out_sec'] = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                            + float(m.group(3)))
                    except ValueError:
                        pass
        except Exception:
            pass

    threading.Thread(target=_drain_err, daemon=True).start()

    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    paused_on = False
    last_wall = time.time()
    last_out = 0.0
    last_report = 0.0
    duration_f = float(duration) if duration else None
    while proc.poll() is None:
        if control and control.get('cancel'):
            state['cancelled'] = True
            try:
                proc.terminate()
            except Exception:
                pass
            break
        ev = (control or {}).get('pause')
        if ev is not None and ev.is_set():
            if not paused_on:
                if _win_suspend_threads(proc.pid, True):
                    paused_on = True
                    say('⏸ 已暂停（点「继续」接着下）')
                else:
                    say('暂停失败：无法挂起 ffmpeg 进程')
                    time.sleep(1.5)
            else:
                time.sleep(0.3)
            continue
        if paused_on:
            _win_suspend_threads(proc.pid, False)
            paused_on = False
            last_wall = time.time()
            last_out = state['out_sec']
            say('▶ 已继续')
        now = time.time()
        out_sec = state['out_sec']
        if progress and out_sec > 0 and now - last_report >= 2.0:
            last_report = now
            dt = now - last_wall
            speed = (out_sec - last_out) / dt if dt > 0 else 0.0
            last_wall, last_out = now, out_sec
            info = {'doneSec': round(out_sec, 1),
                    'speedX': round(speed, 2) if speed > 0 else None}
            if duration_f:
                info['totalSec'] = duration_f
                info['percent'] = min(100.0, out_sec / duration_f * 100.0)
            try:
                progress(info)
            except Exception:
                pass
        time.sleep(0.3)
    try:
        proc.wait(timeout=30)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    if paused_on:
        _win_suspend_threads(proc.pid, False)   # 保险：进程结束前若还挂着，恢复回去再收尸

    if state['cancelled']:
        tmp.unlink(missing_ok=True)
        raise RuntimeError('已取消')
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        err = bytes(state['err_tail']).decode('utf-8', 'replace').strip()[-300:]
        raise RuntimeError('ffmpeg 转换失败：' + (err or '未知错误'))
    tmp.replace(dest)
    return dest.stat().st_size


def download_talk(url, outdir, log=None, bitrate='128k', control=None, proxy='', progress=None):
    """下载一场演讲，返回生成的文件列表。log(str) 用于回报进度。"""
    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    say('解析 TED 页面…')
    info = parse_ted_page(url)
    name = slugify(info['title'])
    en, zh = pick_langs(info['cues'])
    dur = info['duration']
    say(f"标题：{info['title']}")
    say(f"时长：{(dur or 0) // 60} 分 {(dur or 0) % 60} 秒")
    say(f"文稿：英文 {len(en) if en else 0} 句 / 中文 {len(zh) if zh else 0} 句")
    if not en:
        say('⚠ 这场演讲在 TED 上没有英文字幕（无法逐句练习），只下载音频')

    made = []
    mp3 = outdir / f'{name}.mp3'
    if mp3.exists():
        say(f'音频已存在，跳过：{mp3.name}')
    else:
        say('下载音轨并转 MP3（不保存视频）…')
        size = download_mp3(info['audio'], mp3, bitrate, log=say, duration=dur,
                            control=control, proxy=proxy, progress=progress)
        say(f"音频完成：{mp3.name}（{size / 1024 / 1024:.1f} MB）")
    made.append(mp3)

    if en:
        p = outdir / f'{name}.en.srt'
        write_srt(p, en)
        srt_to_vtt(p, outdir / f'{name}.en.vtt')
        made += [p, outdir / f'{name}.en.vtt']
        say(f'英文字幕：{len(en)} 句')
    if zh:
        p = outdir / f'{name}.zh.srt'
        write_srt(p, zh)
        srt_to_vtt(p, outdir / f'{name}.zh.vtt')
        made += [p, outdir / f'{name}.zh.vtt']
        say(f'中文字幕：{len(zh)} 句')

    meta = f"时长 {dur} 秒　·　来源 {url}　·　抓取于 {datetime.now():%Y-%m-%d %H:%M}"
    p = outdir / f'{name}.对照.md'
    write_bilingual(p, en, zh, info['title'], meta)
    made.append(p)
    say('中英对照文稿已生成')

    if en:
        p = outdir / f'{name}.en.txt'
        p.write_text('\n'.join(c['text'] for c in en), encoding='utf-8')
        made.append(p)

    # 元数据（打标签、记录进度用）
    (outdir / f'{name}.meta.json').write_text(json.dumps({
        'title': info['title'], 'url': url, 'duration': dur,
        'addedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'tags': [], 'note': '', 'enCues': len(en) if en else 0, 'zhCues': len(zh) if zh else 0,
    }, ensure_ascii=False, indent=1), encoding='utf-8')


    say('全部完成')
    return {'title': info['title'], 'dir': str(outdir), 'name': name,
            'duration': dur, 'files': [str(x) for x in made]}


# ---------------------------------------------------------------- 本地库
def count_srt_blocks(path):
    try:
        txt = path.read_text(encoding='utf-8', errors='replace')
        return len(re.findall(r'(?m)^\d+\s*$', txt))
    except Exception:
        return 0


def scan_library(dirs):
    """扫描目录，把散落文件配成一条条语料。

    配对规则：同名不同后缀归为一条；主名取自音频文件（没有音频就不算语料）。
    """
    items = []
    seen = set()
    for d in dirs:
        d = Path(d)
        if not d.exists() or not d.is_dir():
            continue
        groups = {}
        for f in sorted(d.iterdir()):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            # 排除转写中间文件（历史残留的 .16k.wav 曾被扫成重复语料）
            if f.stem.endswith('.16k') or f.name.endswith('.meta.json.tmp'):
                continue
            if ext in AUDIO_EXT:
                key, kind = f.stem, 'audio'
            elif ext in SUB_EXT:
                kind = 'zh' if re.search(r'\.(zh|cn)\b', f.stem, re.I) else 'en'
                key = re.sub(r'\.(zh|cn|en)$', '', f.stem, flags=re.I)
            elif ext == '.md' and f.stem.endswith('对照'):
                key, kind = re.sub(r'\.对照$', '', f.stem), 'bilingual'
            elif ext == '.txt':
                key, kind = re.sub(r'\.(en|zh)$', '', f.stem, flags=re.I), 'text'
            elif f.name.endswith('.meta.json'):
                key, kind = f.name[:-len('.meta.json')], 'meta'
            else:
                continue
            groups.setdefault(key, {})[kind] = f

        for key, g in groups.items():
            audio = g.get('audio')
            if not audio:
                continue        # 没音频不成语料
            uid = str(audio.resolve()).lower()
            if uid in seen:
                continue
            seen.add(uid)

            meta = {}
            if g.get('meta'):
                try:
                    meta = json.loads(g['meta'].read_text(encoding='utf-8'))
                except Exception:
                    meta = {}

            title = meta.get('title') or key
            dur = meta.get('duration')
            if not dur and FFMPEG:
                dur = probe_duration(audio)

            # ⑤ 来源类型：演讲/对话/读书/播客 的后处理参数不同，列表页要能显示出来。
            # meta 里没有就现猜一次（只读，不回写——回写发生在下载与手动改类时）
            try:
                import videokind as _vk
                _kind = meta.get('sourceKind')
                if _kind not in _vk.RULES:
                    _kind = _vk.classify(title, meta.get('url') or '', meta)
                _kind_label = _vk.label(_kind)
            except Exception:
                _kind, _kind_label = 'other', '未分类'

            # 确保有 vtt（播放器要用）
            for lg in ('en', 'zh'):
                srt = None
                for cand in g.values():
                    if cand.suffix.lower() == '.srt' and re.search(rf'\.{lg}\b', cand.stem, re.I):
                        srt = cand
                        break
                if srt and not srt.with_suffix('.vtt').exists():
                    try:
                        srt_to_vtt(srt, srt.with_suffix('.vtt'))
                    except Exception:
                        pass

            items.append({
                'id': key,
                'title': title,
                'dir': str(audio.parent),
                'audio': str(audio),
                'size': audio.stat().st_size,
                'duration': dur,
                'hasEn': bool(g.get('en')),
                'hasZh': bool(g.get('zh')),
                'hasBilingual': bool(g.get('bilingual')),
                'enCues': meta.get('enCues') or 0,
                'zhCues': meta.get('zhCues') or 0,
                'tags': meta.get('tags') or [],
                'note': meta.get('note') or '',
                'sourceKind': _kind,
                'sourceKindLabel': _kind_label,
                'url': meta.get('url') or '',
                'addedAt': meta.get('addedAt') or datetime.fromtimestamp(
                    audio.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                'mtime': audio.stat().st_mtime,
            })
    items.sort(key=lambda x: x['mtime'], reverse=True)
    return items


_dur_cache = {}


def probe_duration(audio):
    """读音频时长（秒），带缓存。

    优先 ffprobe；拿不到就退回解析 ffmpeg 的 Duration 输出。
    踩过的坑：ffmpeg 若是从 PATH 找到的裸命令名（就是 'ffmpeg'），
    对它做 .replace('ffmpeg.exe','ffprobe.exe') 会原样返回 'ffmpeg'，
    结果调的还是 ffmpeg，报 "Unrecognized option 'show_entries'"。
    所以必须用 shutil.which 真去找 ffprobe。
    """
    key = str(audio)
    if key in _dur_cache:
        return _dur_cache[key]

    dur = None
    if FFMPEG:
        exe_path = Path(FFMPEG)
        probe = None
        if exe_path.suffix:                      # 是完整路径
            cand = exe_path.with_name('ffprobe.exe')
            if cand.exists():
                probe = str(cand)
        if not probe:
            probe = shutil.which('ffprobe')

        if probe:
            try:
                r = subprocess.run([probe, '-v', 'error', '-show_entries', 'format=duration',
                                    '-of', 'default=nw=1:nk=1', key],
                                   capture_output=True, creationflags=_no_window(), timeout=30)
                if r.returncode == 0:
                    dur = float(r.stdout.decode().strip())
            except Exception:
                dur = None

        if dur is None:
            # 退路：ffmpeg 即使报错也会把 Duration 打到 stderr
            try:
                r = subprocess.run([FFMPEG, '-hide_banner', '-i', key, '-f', 'null', '-'],
                                   capture_output=True, creationflags=_no_window(), timeout=30)
                m = re.search(rb'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr or b'')
                if m:
                    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            except Exception:
                dur = None

    _dur_cache[key] = dur
    return dur


# ---------------------------------------------------------------- 文件操作
def reveal_in_explorer(path):
    """在资源管理器里定位到该文件（选中它）。"""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f'路径不存在：{p}')
    subprocess.Popen(['explorer', '/select,', str(p)])


def open_path(path):
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f'路径不存在：{p}')
    os.startfile(str(p))


def set_meta(item, tags=None, note=None, title=None):
    """写入 .meta.json（标签/备注/标题）。"""
    audio = Path(item['audio'])
    mp = audio.with_name(audio.stem + '.meta.json')
    data = {}
    if mp.exists():
        try:
            data = json.loads(mp.read_text(encoding='utf-8'))
        except Exception:
            data = {}
    if title is not None:
        data['title'] = title
    if tags is not None:
        data['tags'] = tags
    if note is not None:
        data['note'] = note
    mp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    return str(mp)
