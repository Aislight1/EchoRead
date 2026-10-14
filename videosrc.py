# -*- coding: utf-8 -*-
"""通用视频源：B站 / YouTube 链接 → 音频 + 中英字幕，进语料库。

字幕三级策略：
  ① yt-dlp 抓视频自带的手工 CC 字幕（en / zh）
  ② 没有英文字幕 → ffmpeg 转 16k wav → 阿里云 paraformer 转写（逐句时间戳）
  ③ 没有中文字幕 → LLM 批量把英文句翻译成中文（复用设置里配好的 LLM）

产物和 TED 下载完全同构（slug.mp3 + .en.vtt + .zh.vtt + .对照.md + .meta.json），
scan_library 直接识别，跟读 / 发音练习 / 听写全部照常可用。

进度回报（progress 回调）：{'stage': 'audio'|'asr'|'translate', 'percent', ...}
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import tedlibrarian as tl
import prompts
from tedlibrarian import _no_window, _win_suspend_threads, slugify
from urllib.request import Request as _urllib_request, urlopen as _urlopen

APP_DIR = Path(__file__).resolve().parent



def find_ytdlp():
    """找 yt-dlp：PATH → 程序目录 → PyInstaller 解包目录。"""
    exe = shutil.which('yt-dlp') or shutil.which('yt-dlp.exe')
    if exe:
        return exe
    for base in (APP_DIR, Path(getattr(sys, '_MEIPASS', '') or '')):
        if not str(base):
            continue
        p = Path(base) / 'yt-dlp.exe'
        if p.exists():
            return str(p)
    raise RuntimeError('找不到 yt-dlp.exe（应放在程序目录里）')


def detect_platform(url):
    """返回 'bilibili' / 'youtube' / None（None 走 TED 逻辑）。"""
    u = (url or '').lower()
    if 'bilibili.com' in u or 'b23.tv' in u:
        return 'bilibili'
    if 'youtube.com' in u or 'youtu.be' in u:
        return 'youtube'
    return None


def _auto_cookies():
    """自动找一个 Firefox 系浏览器的 cookie（Waterfox / Firefox）。

    YouTube 对数据中心 IP 强制 bot 校验，带浏览器 cookie 才有机会过。
    返回 yt-dlp 的 --cookies-from-browser 参数值，找不到返回 ''。
    """
    appdata = Path(os.environ.get('APPDATA', ''))
    for base in (appdata / 'Waterfox' / 'Profiles', appdata / 'Mozilla' / 'Firefox' / 'Profiles'):
        try:
            for d in base.iterdir():
                if (d / 'cookies.sqlite').exists():
                    return f'firefox:{d}'
        except Exception:
            continue
    return ''


def _yt_extra_args(proxy, cookies, client_arg=None):
    args = []
    if proxy:
        args += ['--proxy', proxy]
    if cookies:
        args += ['--cookies-from-browser', cookies]
    if client_arg:
        args += ['--extractor-args', client_arg]
    return args


def _ffmpeg_for_ytdlp():
    """yt-dlp 的 --ffmpeg-location：需要能同时找到 ffprobe，给目录最稳。"""
    f = tl.FFMPEG
    if not f:
        raise RuntimeError('找不到 ffmpeg')
    p = Path(f)
    if not p.suffix:                      # 只是 'ffmpeg'（在 PATH 上）
        w = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
        if w:
            p = Path(w)
    if p.exists() and (p.parent / 'ffprobe.exe').exists():
        return str(p.parent)
    return str(p)


def _ytdlp_meta(url, extra):
    """读视频标题/时长等元数据。extra = 平台附加参数列表。"""
    exe = find_ytdlp()
    cmd = [exe, '--dump-single-json', '--no-playlist', '--no-warnings'] + list(extra)
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=180, creationflags=_no_window())
    if r.returncode != 0:
        err = (r.stderr.decode('utf-8', 'replace') or '').strip()[-300:]
        raise RuntimeError('读取视频信息失败：' + (err or '未知错误'))
    try:
        return json.loads(r.stdout.decode('utf-8', 'replace'))
    except Exception as e:
        raise RuntimeError('视频信息解析失败：' + str(e))


def _ytdlp_audio(url, dest, extra, log, control, progress):
    """下载音轨并转 128k mp3（带进度 / 暂停 / 取消）。"""
    exe = find_ytdlp()
    # dest 是最终 mp3 全名；outtmpl 必须用不含扩展名的词干——dest 已带 .mp3
    # 再拼 .%(ext)s 会让 yt-dlp 产出 slug.mp3.mp3（2026-10 P38 实证回归）。
    # 字符串剥尾，不用 Path.with_suffix（标题含点会把标题尾部当后缀吞掉，41批铁律）
    dest_s = str(dest)
    stem_s = dest_s[:-4] if dest_s.lower().endswith('.mp3') else dest_s
    cmd = [exe, '--no-playlist', '--no-warnings', '--newline',
           '-x', '--audio-format', 'mp3', '--audio-quality', '128K',
           '-o', stem_s + '.%(ext)s',
           '--ffmpeg-location', _ffmpeg_for_ytdlp()] + list(extra)
    cmd.append(url)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=_no_window())
    state = {'percent': None, 'tail': bytearray(), 'cancelled': False}

    def _reader():
        try:
            for raw in iter(proc.stdout.readline, b''):
                line = raw.decode('utf-8', 'replace').strip()
                if line:
                    state['tail'] = (state['tail'] + raw)[-4096:]
                m = re.search(r'\[download\]\s+([\d.]+)%', line)
                if m:
                    state['percent'] = float(m.group(1))
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()

    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    paused_on = False
    last_report = 0.0
    while proc.poll() is None:
        if control and control.get('cancel'):
            state['cancelled'] = True
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                           capture_output=True, creationflags=_no_window())
            break
        ev = (control or {}).get('pause')
        if ev is not None and ev.is_set():
            if not paused_on:
                if _win_suspend_threads(proc.pid, True):
                    paused_on = True
                    say('⏸ 已暂停（点「继续」接着下）')
                else:
                    say('暂停失败：无法挂起 yt-dlp 进程')
                    time.sleep(1.5)
            else:
                time.sleep(0.3)
            continue
        if paused_on:
            _win_suspend_threads(proc.pid, False)
            paused_on = False
            say('▶ 已继续')
        now = time.time()
        if progress and state['percent'] is not None and now - last_report >= 2.0:
            last_report = now
            try:
                progress({'stage': 'audio', 'percent': min(99.0, state['percent'])})
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
        _win_suspend_threads(proc.pid, False)

    if state['cancelled']:
        dest.unlink(missing_ok=True)
        raise RuntimeError('已取消')
    # 兜底：旧 outtmpl 回归留下的 slug.mp3.mp3 残骸——yt-dlp 成功就改回正名，
    # 失败就清掉（不挡下次重试）
    stray = Path(str(dest) + '.mp3')
    if not dest.exists() and stray.exists():
        if proc.returncode == 0:
            stray.replace(dest)
        else:
            stray.unlink(missing_ok=True)
    if proc.returncode != 0 or not dest.exists():
        tail = bytes(state['tail']).decode('utf-8', 'replace')
        m = None
        for m in re.finditer(r'ERROR:[^\r\n]+', tail):
            pass
        err = (m.group(0) if m else tail.strip()[-300:])
        raise RuntimeError('音频下载失败：' + (err or '未知错误'))


def _find_sub(outdir, slug):
    """在 outdir 里找 yt-dlp 刚写出的 en / zh 字幕，返回 (en_path, zh_path)。"""
    en = zh = None
    for f in Path(outdir).glob(slug + '.*'):
        if f.suffix.lower() not in ('.vtt', '.srt'):
            continue
        stem = f.stem.lower()
        is_en = bool(re.search(r'\.en\b', stem)) or stem.endswith('.en')
        is_zh = bool(re.search(r'(\.zh|zh-hans|zh-cn|\.cn\b)', stem))
        if is_en and not en:
            en = f
        elif is_zh and not zh:
            zh = f
    return en, zh


def _load_cues(path):
    """vtt/srt → cues；srt 先转 vtt。"""
    path = Path(path)
    if path.suffix.lower() == '.srt':
        vtt = path.with_suffix('.vtt')
        try:
            tl.srt_to_vtt(path, vtt)
            path = vtt
        except Exception:
            pass
    cues = tl.parse_vtt_cues(path.read_text(encoding='utf-8', errors='replace'))
    return [c for c in cues if (c.get('text') or '').strip()]


def _ytdlp_subs(url, outdir, slug, extra, log):
    """只抓手工 CC 字幕（自动字幕质量差且格式坑多，宁可用 ASR）。"""
    exe = find_ytdlp()
    cmd = [exe, '--no-playlist', '--no-warnings', '--write-subs', '--skip-download',
           '--sub-langs', 'en.*,zh.*,zh-Hans.*,zh-CN.*',
           '--sub-format', 'vtt/srt/best',
           '-o', str(Path(outdir) / f'{slug}.%(ext)s')] + list(extra)
    cmd.append(url)
    subprocess.run(cmd, capture_output=True, timeout=300, creationflags=_no_window())
    return _find_sub(outdir, slug)


def sanitize_cues(cues):
    """时间戳净化：倒挂/负值/超短句/超长空转段修复。

    规则：start<0 清零；end<=start 按 2.5 词/秒重标；<0.5 秒的句子并入下一句；
    单句 >25 秒且词数 <10 的（时间戳明显失真）按 2.5 词/秒重标。
    """
    out = []
    for c in cues:
        st = max(0.0, float(c.get('start') or 0))
        en = float(c.get('end') or 0)
        text = (c.get('text') or '').strip()
        if not text:
            continue
        n = len(text.split())
        if en <= st:
            en = st + max(0.6, n / 2.5)
        if en - st > 25 and n < 10:            # 25 秒装不下 10 个词 → 时间戳失真
            en = st + max(1.0, n / 2.5)
        out.append({'start': round(st, 3), 'end': round(en, 3), 'text': text})
    # 超短句并入下一句
    merged = []
    for c in out:
        if merged and c['end'] - c['start'] < 0.5 and merged[-1]['end'] <= c['start'] + 0.01:
            merged[-1]['end'] = c['end']
            merged[-1]['text'] = (merged[-1]['text'] + ' ' + c['text']).strip()
        else:
            merged.append(c)
    merged.sort(key=lambda x: x['start'])          # 防御：乱序/负值清零后先排再压平
    # 相邻重叠压平
    for i in range(len(merged) - 1):
        if merged[i]['end'] > merged[i + 1]['start']:
            merged[i]['end'] = max(merged[i]['start'] + 0.4, merged[i + 1]['start'])
    return merged


def _split_long_cues(cues, max_words=22, max_dur=12.0, min_words=3):
    """把过长的转写句在标点处二次切分（Gummy 等一体模型按语义分段，句会很长）。

    切点只在 ', ; and but because so when' 这类弱连接处；时间按词数线性分。
    min_words：切出的片段不足 min_words 词时并回前段（防逐词念——合成示范
    拿到单词级文本会崩坏）。
    """
    import re as _re
    out = []
    for c in cues:
        text = (c.get('text') or '').strip()
        words = text.split()
        dur = float(c.get('end') or 0) - float(c.get('start') or 0)
        # 入口：词数超限才切（dur 条件会放行'时间戳失真的坏长句'——那正是要修的）
        if len(words) <= max_words:
            out.append(c)
            continue
        parts = _re.split(r'(?<=[,;.!?])\s+|\s+(?=and |but |because |so |when |which |that |this |where |if |or )',
                          text)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) <= 1:
            out.append(c)
            continue
        total_w = sum(len(p.split()) for p in parts) or 1
        # 累积成段：不足 min_words 词的片段滚入当前段（防逐词念：
        # 合成示范拿到单词级文本会崩坏）；单段仍超长就接受（宁长勿碎）
        merged_parts, buf = [], []
        for p in parts:
            buf.append(p)
            if len(' '.join(buf).split()) >= min_words:
                merged_parts.append(' '.join(buf))
                buf = []
        if buf:
            if merged_parts:
                merged_parts[-1] = merged_parts[-1] + ' ' + ' '.join(buf)
            else:
                merged_parts.append(' '.join(buf))
        t = float(c.get('start') or 0)
        d = dur
        total_m = sum(len(p.split()) for p in merged_parts) or 1
        for p in merged_parts:
            w = len(p.split())
            seg_d = d * w / total_m
            nc = {'start': round(t, 3), 'end': round(t + seg_d, 3), 'text': p}
            if c.get('level'):                  # 难度随切分段继承（组装合并时再众数表决）
                nc['level'] = c['level']
            out.append(nc)
            t += seg_d
    return out


def _ends_terminal(text):
    """句末标点判定：剥离尾部引号/括号后看 . ! ? …（"results.'" 也算句末）。"""
    t = (text or '').rstrip().rstrip('\'"\u2018\u2019\u201c\u201d)]）》」』')
    return t.endswith(('.', '!', '?', '…'))


def _merge_fragments(cues, max_words=45):
    """A6 碎片合并：把被转写腰斩的句拼回整句（B站/ASR 语料的"逐句精读出现独立单词"根治）。

    合并条件（满足其一，且合并后 ≤max_words 词、句间空档 <5s）：
      - 前句不以句末标点结尾（逗号/悬空从句——实测 67 集有 52% 这种腰斩句）
      - 后句 ≤2 词且自身不以句末标点结尾（"the IT department" 式碎块）
    合并时记录 _bt/_be（各成员原始起止，供拆分 pass 按真实边界切时间）；
    _lv 收集成员的 AI 难度（全部一致才保留，否则丢弃回退本地规则）。
    """
    out = []
    for c in cues:
        c = dict(c)
        text = (c.get('text') or '').strip()
        if not text:
            continue
        if out:
            prev = out[-1]
            cw = len(text.split())
            join = ((not _ends_terminal(prev.get('text')))
                    or (cw <= 2 and not _ends_terminal(text)))
            if join:
                pw = len((prev.get('text') or '').split())
                gap = float(c.get('start') or 0) - float(prev.get('end') or 0)
                if pw + cw <= max_words and gap < 5.0:
                    prev['_bt'] = (prev.get('_bt') or []) + [float(c.get('start') or 0)]
                    prev['_be'] = (prev.get('_be') or []) + [float(c.get('end') or 0)]
                    prev['_lv'] = (prev.get('_lv') or [prev.get('level')]) + [c.get('level')]
                    prev['end'] = max(float(prev.get('end') or 0), float(c.get('end') or 0))
                    prev['text'] = ((prev.get('text') or '').rstrip() + ' ' + text).strip()
                    continue
        out.append(c)
    for c in out:
        lv = c.pop('_lv', None)
        lvs = [x for x in (lv or []) if x]
        if lvs:
            # 众数表决：同句切出的碎片可能被 LLM 标了不同难度，取多数
            from collections import Counter
            c['level'] = Counter(lvs).most_common(1)[0][0]
        # 无 _lv（未参与合并）→ 保留原值；成员全无 level → 原值本来就没有，不硬清
    return out


def _split_sentences(cues, min_words=1):
    """A2 句末标点即拆：一条 cue 里含 ≥2 个完整句子时按 [.!?]+空格+大写 拆开。

    对话轮次 cue（"So let's begin. Emma, have you…?"）靠这刀拆回真句子；
    时间边界优先用 _merge_fragments 记录的 _bt/_be（成员真实起止），
    数量对不上（或无记录）按词数线性分；min_words=1——句末标点是强边界，
    "Hello!" 这类单词句是合法句子（与 _split_long_cues 的弱连接切分不同，不需要防逐词）。
    level 由各子句继承。
    """
    out = []
    for c in cues:
        text = (c.get('text') or '').strip()
        bt = c.pop('_bt', None) or []
        be = c.pop('_be', None) or []
        bounds = [m.end() for m in re.finditer(r"[.!?]['\u2019\"\u201d]?\s+(?=[A-Z0-9])", text)]
        if not bounds:
            out.append(c)
            continue
        parts, pos = [], 0
        for b in bounds:
            parts.append(text[pos:b].strip())
            pos = b
        parts.append(text[pos:].strip())
        parts = [p for p in parts if p]
        if len(parts) <= 1:
            out.append(c)
            continue
        if min_words > 1:                          # 词数下限并回前段（默认不启用）
            merged_parts, buf = [], []
            for p in parts:
                buf.append(p)
                if len(' '.join(buf).split()) >= min_words:
                    merged_parts.append(' '.join(buf))
                    buf = []
            if buf and merged_parts:
                merged_parts[-1] += ' ' + ' '.join(buf)
            elif buf:
                merged_parts.append(' '.join(buf))
            parts = merged_parts
        t0 = float(c.get('start') or 0)
        t1 = max(float(c.get('end') or 0), t0 + 0.3)
        lv = c.get('level')
        segs = None
        if len(bt) == len(parts) - 1:              # 合并记录与拆分点对上：真实边界
            starts = [t0] + list(bt)
            ends = list(be) + [t1]
            if all(starts[k] < starts[k + 1] for k in range(len(starts) - 1)):
                segs = [[starts[k], ends[k] if k < len(ends) else t1]
                        for k in range(len(parts))]
                for k in range(len(segs) - 1):     # 边界重叠：剪前段 end 保后段起点
                    if segs[k][1] > segs[k + 1][0]:
                        segs[k][1] = max(segs[k][0] + 0.2, segs[k + 1][0])
                if any(e - s < 0.15 for s, e in segs):
                    segs = None                    # 某段被挤没了 → 退化为线性
        if segs is None:
            total_w = sum(len(p.split()) for p in parts) or 1
            dur = t1 - t0
            t = t0
            segs = []
            for p in parts:
                w = len(p.split())
                d = dur * w / total_w
                segs.append([t, t + d])
                t += d
        for p, (s, e) in zip(parts, segs):
            nc = {'start': round(s, 3), 'end': round(e, 3), 'text': p}
            if lv:
                nc['level'] = lv
            out.append(nc)
    return out


def _dedup_repeats(cues, look=3, min_chars=20):
    """A6 去重：ASR 并行转写合并产生的重复句（同文本紧邻/近邻交错，典型 A-A-B-A-C，
    一份时间正常一份塌缩——67集实测 9 种 22 句，含 'Today, we have…' ×3）。

    规则：规范化后完全同文本（≥min_chars，"Yes!" 这类真实口头重复不碰）且在
    look 句范围内 → 保留时间 sane 的那份（0.5-6 词/s）；都 sane 留先出现的。
    用户已确认这不是教学重复（教学重复不会是紧邻交错+时间塌缩）。
    """
    def _nt(t):
        return re.sub(r"[^a-z0-9']", '', (t or '').lower())

    def _sane(c):
        n = len((c.get('text') or '').split())
        d = float(c.get('end') or 0) - float(c.get('start') or 0)
        return d > 0 and 0.5 <= n / d <= 6.0

    n = len(cues)
    drop = set()
    for i in range(n):
        if i in drop:
            continue
        ka = _nt(cues[i].get('text'))
        if len(ka) < min_chars:
            continue
        for j in range(i + 1, min(n, i + 1 + look)):
            if j in drop:
                continue
            if _nt(cues[j].get('text')) != ka:
                continue
            if _sane(cues[i]) and not _sane(cues[j]):
                drop.add(j)
            elif _sane(cues[j]) and not _sane(cues[i]):
                drop.add(i)
                break
            else:
                drop.add(j)
    if not drop:
        return cues
    return [c for k, c in enumerate(cues) if k not in drop]


def assemble_sentences(cues, max_words=45):
    """句子组装层（A6+A2 组合入口）：去重 → 碎片合并 → 句末标点拆分 → 终局排序+重叠钳制。

    幂等：对已组装过的字幕无副作用（没有腰斩可合、没有多句可拆、没有重复可删）。
    使用点：videosrc 生成出口（治本）、app._cues_of 读取出口（治标——
    不动字幕文件，逐句精读/听力/精讲立即恢复连贯）、doctor 落盘出口（统一句口径，
    保证 盘=展=lvl 三者 cueCount 永远一致——B2 的根治）。
    """
    cues = _dedup_repeats(cues)                   # 先按原始粒度去重（交错重复在合并前删）
    cues = _merge_fragments(cues, max_words=max_words)
    cues = _dedup_repeats(cues)                   # 合并可能拼出近邻同文本，再扫一遍
    cues = _split_sentences(cues)
    cues.sort(key=lambda x: float(x.get('start') or 0))
    for i in range(len(cues) - 1):                 # 终局钳制：拆分边界异常时不把重叠带给下游
        s = float(cues[i].get('start') or 0)
        ns = float(cues[i + 1].get('start') or 0)
        if float(cues[i].get('end') or 0) > ns:
            cues[i]['end'] = round(max(s + 0.2, ns), 3)
        if float(cues[i].get('end') or 0) <= s:
            cues[i]['end'] = round(s + 0.3, 3)
    return cues


def _timestamp_asr(asr, log=None):
    """转写管线专用 ASR（57 批起优先「音频处理模型」config.audioModel，与练习识别分开配置）：
    - 配了 audioModel → 直接用它（如 qwen3.8-omni-flash 长音频文件转写；无词级时间戳，
      出口有插值时间轴兜底 + doctor 对齐环节定稿校准）
    - 没配 → 沿用旧逻辑：MiMo 只返回纯文本（0-0 假时间），有阿里云 Key 就自动换
      paraformer 专用实例；没有 Key 硬报错拦截，不产假时间轴（Bob p01 事故根源）。"""
    cfg = getattr(asr, 'cfg', None) or {}
    audio_model = (cfg.get('audioModel') or '').strip()
    if audio_model:
        import speech_asr
        ncfg = dict(cfg)
        ncfg['asrProvider'] = 'dashscope'
        ncfg['asrModel'] = audio_model
        if log:
            log(f'音频处理模型：{audio_model}（生成字幕/转写专用，与练习识别分开配置）')
        return speech_asr.ASR(ncfg, log)
    try:
        prov = (asr.provider() or '').lower()
    except Exception:
        prov = ''
    if prov != 'mimo':
        return asr
    key = (cfg.get('dashscopeApiKey') or '').strip()
    if not key:
        raise RuntimeError('生成字幕必须用带时间戳的识别引擎：当前识别是 MiMo（只有纯文本、没有时间戳，'
                           '生成的字幕时间轴会全错）。请在设置里填阿里云 Key（paraformer）或配置「音频处理模型」后再试。')
    import speech_asr
    ncfg = dict(getattr(asr, 'cfg', None) or {})
    ncfg['asrProvider'] = 'dashscope'
    ncfg['asrModel'] = 'paraformer-realtime-v2'
    if log:
        log('转写需要逐句时间戳：MiMo 没有时间戳，自动改用阿里云 paraformer'
            '（你练习用的 MiMo 识别不受影响）')
    return speech_asr.ASR(ncfg, log)


def _asr_to_cues(mp3, workdir, asr, log, duration, mode='traditional'):
    """没有字幕时：wav → 阿里云转写 → 逐句 cues。

    mode: traditional=整段顺序 / parallel=静音切片并行 / gummy=转写翻译一体。
    gummy 返回 (en_cues, zh_cues)；其余返回 (en_cues, None)。
    中间 WAV 放系统临时目录——绝不落语料目录（曾把 .16k.wav 扫成重复语料）。
    并行已加固（合并 v2：词集相似判重保完整 + 1.5s 重叠），长音频不再强制单流
    ——当年 >20min 禁并行是旧合并 (start,text) 精确键去不掉半句/全句双版的
    权宜之计，根因已修（67集 A-A-B-A-C 交错事故不再复现）。
    """
    import tempfile
    asr = _timestamp_asr(asr, log)      # MiMo 无时间戳 → 自动换 paraformer / 硬拦截
    tmpdir = Path(tempfile.mkdtemp(prefix='ted_asr_'))
    wav = tmpdir / (Path(mp3).stem + '.16k.wav')
    try:
        if log:
            log('转换音频为 16k WAV…')
        cmd = [tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(mp3),
               '-ac', '1', '-ar', '16000', str(wav)]
        r = subprocess.run(cmd, capture_output=True, creationflags=_no_window())
        if r.returncode != 0 or not wav.exists():
            err = (r.stderr.decode('utf-8', 'replace') or '').strip()[-200:]
            raise RuntimeError('WAV 转换失败：' + (err or '未知错误'))

        zh_cues = None
        if mode == 'gummy':
            if log:
                log('Gummy 一体模式：转写+翻译同时进行…')
            try:
                res = asr.recognize_gummy(str(wav), log=log)
                sents = (res.get('en') or {}).get('sentences') or []
                zh_sents = ((res.get('zh') or {}).get('sentences')) or []
                if not sents:
                    raise RuntimeError('Gummy 转写结果为空')
                cues = [{'start': float(s['start'] or 0),
                         'end': max(float(s['end'] or 0), float(s['start'] or 0) + 0.5),
                         'text': s.get('text', '')} for s in sents]
                cues = [c for c in cues if c['text']]
                if zh_sents:
                    zh_cues = [{'start': float(z['start'] or 0),
                                'end': float(z['end'] or 0), 'text': z.get('text', '')}
                               for z in zh_sents]
                    zh_cues = [c for c in zh_cues if c['text']]
            except Exception as e:
                if log:
                    log(f'Gummy 失败（{str(e)[:120]}），回落分片/整段转写')
                cues = _plain_asr_cues(asr, str(wav), log, mode, workdir)
        else:
            cues = _plain_asr_cues(asr, str(wav), log, mode, workdir)
        cues = sanitize_cues(cues)
        cues = assemble_sentences(cues)      # A6/A2：碎片合并+句末拆分（新导入不再产腰斩/轮次句）
        cues = _split_long_cues(cues)
        return cues, zh_cues
    finally:
        import shutil as _sh
        _sh.rmtree(tmpdir, ignore_errors=True)


def _plain_asr_cues(asr, wav, log, mode, workdir=None):
    """传统/分片并行共用：调 recognize 或 recognize_parallel → cues。"""
    if mode == 'parallel' and hasattr(asr, 'recognize_parallel'):
        if log:
            log('分片并行模式（约快 3 倍）…')
        res = asr.recognize_parallel(wav, language='en', workers=4, log=log)
    else:
        if log:
            log('阿里云语音转写中（英文，逐句时间戳）…')
        res = asr.recognize(wav, language='en')
    sents = res.get('sentences') or []
    if not sents:
        raise RuntimeError('转写结果为空（这段视频可能没有清晰人声）')
    cues = []
    for s in sents:
        st, en = float(s.get('start') or 0), float(s.get('end') or 0)
        cues.append({'start': st, 'end': max(en, st + 0.5), 'text': (s.get('text') or '').strip()})
    cues = [c for c in cues if c['text']]
    # 出口兜底：识别引擎没给时间戳（过半句子 0 时长）→ 拦截，绝不让假时间轴落盘
    _zero = sum(1 for c in cues if c['end'] - c['start'] < 0.05)
    if _zero > len(cues) * 0.5:
        raise RuntimeError('识别结果没有时间戳（%d/%d 句零时长）——生成字幕需要带词级时间戳的'
                            '引擎（阿里云 paraformer）' % (_zero, len(cues)))
    return cues


def _translate_cues(en_cues, llm, log, control, progress):
    """LLM 批量翻译英文 cues → 中文 cues（结构对齐）。失败则返回 None。"""
    if llm is None or not getattr(llm, 'ready', False):
        if log:
            log('⚠ 未配置 LLM（设置 → 对话与纠错），跳过中文字幕')
        return None
    n = len(en_cues)
    zh_cues = [{'start': c['start'], 'end': c['end'], 'text': ''} for c in en_cues]
    B = 40
    total = max(1, math.ceil(n / B))
    for bi in range(total):
        if control and control.get('cancel'):
            raise RuntimeError('已取消')
        chunk = en_cues[bi * B:(bi + 1) * B]
        lines = '\n'.join(f'{i}|{c["text"]}' for i, c in enumerate(chunk))
        try:
            sys_p, _u = prompts.get('subtitle_translate')
            user = prompts.fmt('subtitle_translate', n=len(chunk), lines=lines)
            data = llm.chat_json(sys_p, user, max_tokens=4000)
            items = data if isinstance(data, list) else (data.get('items') or [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    idx = int(it.get('i', -1))
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(chunk):
                    zh_cues[bi * B + idx]['text'] = (it.get('zh') or '').strip()
        except Exception as e:
            if log:
                log(f'翻译批次 {bi + 1}/{total} 失败：{str(e)[:120]}')
        if progress:
            try:
                progress({'stage': 'translate', 'percent': (bi + 1) / total * 100.0,
                          'done': bi + 1, 'total': total})
            except Exception:
                pass
    zh_cues = [c for c in zh_cues if c['text']]
    if not zh_cues:
        return None
    return zh_cues


def _bili_api(url_api):
    """B站 web API 请求：先 GET 主页拿 buvid3 cookie（否则 412 反爬）。"""
    cj = {}
    try:
        req0 = _urllib_request('https://www.bilibili.com',
                               headers={'User-Agent': 'Mozilla/5.0'})
        with _urlopen(req0, timeout=10) as r0:
            for sc in (r0.headers.get_all('Set-Cookie') or []):
                k, _, v = sc.split(';')[0].partition('=')
                cj[k.strip()] = v.strip()
    except Exception:
        pass
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
    if cj:
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in cj.items())
    return _urlopen(_urllib_request(url_api, headers=headers), timeout=15)


def probe_collection(url, log=None):
    """探测 B站链接是不是合集/多P（不下载）。

    ① pagelist API 优先：多P视频（如"【1000集全】"单 BV 千 P）一个请求返回全部分集，
      秒级且不受 yt-dlp 分页超时影响；单 P 直接判非合集。
    ② yt-dlp --flat-playlist 兜底：season 类合集（跨 BV）走这条，超时放宽到 150s。
    返回 {'is_collection': bool, 'count': N,
          'entries': [{'p': 分集号(1起), 'title', 'duration秒, 'url': 该集直链}]}
    探测失败按"非合集"处理——不挡正常下载。"""
    # ① pagelist API（B站官方接口，直连免鉴权）
    try:
        m = re.search(r'/(BV[a-zA-Z0-9]+)', url)
        if m:
            bv = m.group(1)
            api = f'https://api.bilibili.com/x/player/pagelist?bvid={bv}'
            with _bili_api(api) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            parts = (data.get('data') or []) if isinstance(data, dict) and data.get('code') == 0 else []
            if len(parts) >= 2:
                base = f'https://www.bilibili.com/video/{bv}'
                entries = [{'p': int(p.get('page') or i + 1),
                            'title': str(p.get('part') or f'P{i + 1}')[:70],
                            'duration': int(p.get('duration') or 0),
                            'url': f'{base}?p={p.get("page") or i + 1}'}
                           for i, p in enumerate(parts) if isinstance(p, dict)]
                if log:
                    log(f'多P视频：{len(entries)} 个分集（pagelist API）')
                return {'is_collection': True, 'count': len(entries), 'entries': entries}
            if len(parts) == 1:
                return {'is_collection': False, 'count': 0, 'entries': []}
    except Exception as e:
        if log:
            log(f'pagelist 探测失败，改走 yt-dlp：{str(e)[:80]}')
    # ② yt-dlp flat 兜底
    try:
        exe = find_ytdlp()
        cmd = [exe, '-J', '--flat-playlist', '--no-warnings'] + _yt_extra_args('', '') + [url]
        r = subprocess.run(cmd, capture_output=True, timeout=150, creationflags=_no_window())
        if r.returncode != 0:
            return {'is_collection': False, 'count': 0, 'entries': []}
        data = json.loads(r.stdout.decode('utf-8', 'replace'))
        entries = data.get('entries') if isinstance(data, dict) else None
        if not entries:
            return {'is_collection': False, 'count': 0, 'entries': []}
        base = None
        mm = re.search(r'(https?://www\.bilibili\.com/video/BV[a-zA-Z0-9]+)', url)
        if mm:
            base = mm.group(1)
        out = []
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            eurl = str(e.get('url') or e.get('webpage_url') or '')
            p_no = int(e.get('playlist_index') or i + 1)
            if base and not eurl.startswith('http'):
                eurl = f'{base}?p={p_no}'
            out.append({'p': p_no,
                        'title': str(e.get('title') or f'第{i + 1}集')[:70],
                        'duration': int(e.get('duration') or 0),
                        'url': eurl})
        return {'is_collection': len(out) > 1, 'count': len(out), 'entries': out}
    except Exception as e:
        if log:
            log(f'合集探测失败（按单视频处理）：{str(e)[:80]}')
        return {'is_collection': False, 'count': 0, 'entries': []}


def download_video(url, outdir, log=None, control=None, proxy='', progress=None,
                   asr=None, llm=None, cookies=None, episodes=None):
    """B站 / YouTube 视频 → 音频 + 中英字幕，返回与 download_talk 同构的结果。

    episodes: 合集分集号列表（如 [3] 或 [1,2,5]）——逐集顺序下载，
    每集走完整管线（音频→字幕→练习就绪），日志标注 第k/N集。None=普通单视频。"""
    if not episodes:
        return _download_one(url, outdir, log, control, proxy, progress, asr, llm, cookies)
    results = []
    for k, p in enumerate(episodes):
        if control and control.get('cancel'):
            if log:
                log('合集下载已取消（后面的集不再下载）')
            break
        sep = '&' if '?' in url else '?'
        u = f'{url}{sep}p={p}'
        if log:
            log(f'━━━━ 合集第 {k + 1}/{len(episodes)} 集（P{p}）━━━━')
        try:
            r = _download_one(u, outdir, log, control, proxy, progress, asr, llm, cookies)
            results.append(r)
        except Exception as e:
            if log:
                log(f'P{p} 失败（跳过继续）：{str(e)[:120]}')
            if control and control.get('cancel'):
                break
    if not results:
        raise RuntimeError('合集全部分集下载失败')
    return results[-1]


def _download_one(url, outdir, log=None, control=None, proxy='', progress=None,
                  asr=None, llm=None, cookies=None):
    """单视频完整管线（download_video 的原实现）。"""
    def say(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    plat = detect_platform(url)
    if not plat:
        raise RuntimeError('不是 B站 / YouTube 链接')
    plat_name = 'B站' if plat == 'bilibili' else 'YouTube'
    use_proxy = (proxy or '').strip()

    # 平台参数：B站直连即可；YouTube 需要代理 + 浏览器 cookie，
    # 且反爬是猫鼠游戏，备多套参数轮着试。
    attempts = []
    if plat == 'bilibili':
        attempts = [_yt_extra_args('', '')]
    else:
        if not use_proxy:
            say('⚠ YouTube 直连大概率失败，建议在「下载代理」里填 Clash 地址')
        ck = cookies if cookies else _auto_cookies()
        if ck:
            say('已启用浏览器 cookie（过 YouTube 人机校验）')
        attempts = [
            _yt_extra_args(use_proxy, ck, 'youtube:player_client=android,web_safari'),
            _yt_extra_args(use_proxy, ck),
        ]

    say(f'平台：{plat_name}（yt-dlp）')
    info = None
    ok_extra = None
    last_err = ''
    for i, extra in enumerate(attempts):
        try:
            info = _ytdlp_meta(url, extra)
            ok_extra = extra
            if plat == 'youtube' and i > 0:
                say('第 %d 套参数成功' % (i + 1))
            break
        except Exception as e:
            last_err = str(e)
            if log:
                log(f'参数方案 {i + 1}/{len(attempts)} 失败：{str(e)[:140]}')
    if info is None:
        raise RuntimeError(
            'YouTube 读取失败（反爬限制，时好时坏）。\n'
            '  可尝试：① 确认在 Waterfox/Firefox 里登录过 YouTube；'
            '② 更新 yt-dlp（程序目录运行 yt-dlp.exe -U）；\n'
            '  ③ B站是当前最稳的学习视频来源。\n'
            '  最后错误：' + last_err[:200])
    title = (info.get('title') or 'video').strip()
    dur = int(info.get('duration') or 0)
    slug = slugify(title)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    say(f'标题：{title}')
    say(f'时长：{dur // 60} 分 {dur % 60} 秒')

    made = []

    # ① 音频
    mp3 = outdir / f'{slug}.mp3'
    if mp3.exists():
        say(f'音频已存在，跳过：{mp3.name}')
    else:
        say('下载音频并转 MP3（128k）…')
        _ytdlp_audio(url, mp3, ok_extra, log, control, progress)
        say(f'音频完成：{mp3.name}（{mp3.stat().st_size / 1024 / 1024:.1f} MB）')
    made.append(mp3)

    # ② 英文字幕：自带 CC → 没有则 ASR 转写
    en_cues, zh_cues = [], []
    en_sub, zh_sub = _ytdlp_subs(url, outdir, slug, ok_extra, log)
    if en_sub:
        en_cues = _load_cues(en_sub)
        say(f'视频自带英文字幕：{len(en_cues)} 句')
    if zh_sub:
        zh_cues = _load_cues(zh_sub)
        say(f'视频自带中文字幕：{len(zh_cues)} 句')
    if not en_cues:
        if asr is None:
            raise RuntimeError('这个视频没有自带字幕，且应用未配置语音识别'
                               '（设置 → 语音识别 填阿里云 Key）')
        say('视频没有自带英文字幕，改用语音识别转写…')
        asr_mode = getattr(asr, 'mode_hint', '') or 'traditional'
        # 估算倍速：分片并行 ≈ 12x×4路，一体 ≈ 12x，传统 12x
        est_x = 12.0 * (4.0 if asr_mode == 'parallel' else 1.0)
        # 转写是黑盒接口：起监控线程按估算倍速算进度（封顶 92%），完成置 100
        mon = {'on': True}
        t0 = time.time()
        if progress and dur and dur > 0:
            def _asr_monitor():
                time.sleep(2.0)
                while mon['on']:
                    el = time.time() - t0 - 2.0
                    if el < 0:
                        time.sleep(1.0)
                        continue
                    est = min(95.0, el * est_x / dur * 100.0)
                    spd = (el > 1.5) and round(dur / el, 1) or 0
                    try:
                        progress({'stage': 'asr', 'percent': round(est, 1),
                                  'speedX': spd, 'estimated': True})
                    except Exception:
                        pass
                    time.sleep(2.0)
            threading.Thread(target=_asr_monitor, daemon=True).start()
        else:
            if progress:
                progress({'stage': 'asr', 'percent': None})
        en_cues, gummy_zh = _asr_to_cues(mp3, outdir, asr, log, dur, mode=asr_mode)
        if gummy_zh and not zh_cues:
            zh_cues = gummy_zh
            say(f'Gummy 自带中文翻译：{len(zh_cues)} 句（跳过 LLM 翻译）')
        mon['on'] = False
        if progress:
            try:
                progress({'stage': 'asr', 'percent': 100.0})
            except Exception:
                pass
        say(f'转写完成：{len(en_cues)} 句')

    # ③ 中文字幕：自带 → LLM 翻译
    if not zh_cues and en_cues:
        say('没有自带中文字幕，用 LLM 翻译…')
        zh_cues = _translate_cues(en_cues, llm, log, control, progress) or []
        if zh_cues:
            say(f'翻译完成：{len(zh_cues)} 句')

    # ④ 落盘（与 TED 下载同构）
    if en_cues:
        p = outdir / f'{slug}.en.srt'
        tl.write_srt(p, en_cues)
        tl.srt_to_vtt(p, outdir / f'{slug}.en.vtt')
        made += [p, outdir / f'{slug}.en.vtt']
    if zh_cues:
        p = outdir / f'{slug}.zh.srt'
        tl.write_srt(p, zh_cues)
        tl.srt_to_vtt(p, outdir / f'{slug}.zh.vtt')
        made += [p, outdir / f'{slug}.zh.vtt']

    meta_line = f'时长 {dur} 秒　·　来源 {url}　·　抓取于 {datetime.now():%Y-%m-%d %H:%M}'
    tl.write_bilingual(outdir / f'{slug}.对照.md', en_cues, zh_cues, title, meta_line)
    made.append(outdir / f'{slug}.对照.md')

    (outdir / f'{slug}.meta.json').write_text(json.dumps({
        'title': title, 'url': url, 'duration': dur, 'source': plat,
        'addedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'tags': [], 'note': '',
        'enCues': len(en_cues), 'zhCues': len(zh_cues),
    }, ensure_ascii=False, indent=1), encoding='utf-8')

    if not en_cues:
        say('⚠ 未获得英文字幕，只下载了音频（无法逐句练习）')
    say('全部完成')
    return {'title': title, 'dir': str(outdir), 'name': slug,
            'duration': dur, 'files': [str(x) for x in made]}
