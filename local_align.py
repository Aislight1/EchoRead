# -*- coding: utf-8 -*-
"""本地强制对齐引擎（MMS_FA）：定稿文本 + 原音频 → 词级时间戳 → 句级时间轴。

对标 Echogarden / WhisperX 的"转写与对齐解耦"：
- 文本是输入不是输出——判别式 CTC 对齐，结构上不可能交错/漏句；
- 词级时间戳精度几十毫秒，远优于生成式 ASR 顺带的时间戳（插值 873ms 那种）；
- 副产品 words（词级时间流）落 .words.json sidecar，跟读页卡拉OK高亮可直接用。

依赖策略：torch/torchaudio 缺失或模型加载失败时 available()=False，
调用方（doctor）回落现有 paraformer 两遍对齐——永远不比现状差。

实测量（本机 CPU，2026-10）：前向 ≈3.9x 实时（37min 音频 ≈14min，一次性），
对齐 DP 本身 0.1s/百句量级；模型首载 2.4s（缓存后），首次下载约 1.2GB。
"""
import hashlib
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

_MODEL = None          # (model, tokenizer, aligner, sample_rate) 进程内单例
_AVAIL = None          # None=未测 / True / False

# 窗级缓存版本：改了切窗口径 / 归一化口径 / 单位换算就 +1，旧缓存自动失效
_CACHE_VER = 'fa1'


def prewarm():
    """主线程预热：import torch + 一次小张量运算。

    Windows 上 torch 的 OpenMP 运行时必须在主线程完成首载初始化，
    后台线程（doctor run）里首次 import 会段错误（本机 2026-10 实证）。
    没装 torch 时静默返回（available() 将为 False）。
    """
    try:
        import torch
        torch.zeros(4)
        return True
    except Exception:
        return False


def available(refresh=False, log=None):
    """torch/torchaudio 可 import 即认为可用（快速；结果进程内缓存）。

    模型加载/首次下载（约 1.2GB）延迟到 align_cues 真正用时——doctor 启动检测
    不能被下载卡住。加载失败在 align_cues 里抛异常，调用方回落 paraformer。
    """
    global _AVAIL
    if _AVAIL is not None and not refresh:
        return _AVAIL
    try:
        import torch                   # noqa: F401
        import torchaudio              # noqa: F401
        from torchaudio.pipelines import MMS_FA   # noqa: F401
        _AVAIL = True
    except Exception as e:
        _AVAIL = False
        if log:
            log(f'本地强制对齐不可用（{str(e)[:90]}），回落 paraformer 管线')
    return _AVAIL


def _get_model():
    global _MODEL
    if _MODEL is None:
        import os
        import torch
        from torchaudio.pipelines import MMS_FA as bundle
        # 多核 Windows 上 OpenMP 全线程在本机有随机段错误记录——限 4 线程
        # （对齐前向本身算力小，限线程对耗时影响很小）
        torch.set_num_threads(min(4, os.cpu_count() or 4))
        m = bundle.get_model()
        m.eval()
        _MODEL = (m, bundle.get_tokenizer(), bundle.get_aligner(), bundle.sample_rate)
    return _MODEL


_WORD_RE = re.compile(r"[A-Za-z']+")


def norm_words(text):
    """对齐用词归一化：只留纯字母词（MMS 字典无数字/标点），撇号收边。

    数字词丢弃（2019 等由句内其余词承担定位），纯数字句调用方按 kept 处理。
    """
    out = []
    for w in _WORD_RE.findall((text or '').lower()):
        w = w.strip("'")
        if w:
            out.append(w)
    return out


def _windows(cues, target=120.0):
    """按句累积时长切窗（句子不跨窗），返回 [[cue_idx,...], ...]。

    每窗 ≤target 秒（最后一段可短）；窗间互相独立，起点偏移加回。
    120s 而非 300s：窗越大 wav2vec2 前向的中间激活越大（300s 在 doctor
    进程里 OOM 实证），120s 是内存/速度的折中（冒烟 100s 稳过）。
    """
    if not cues:
        return []
    wins, cur, span = [], [], 0.0
    for i, c in enumerate(cues):
        dur = max(0.0, float(c['end']) - float(c['start']))
        if cur and span + dur > target:
            wins.append(cur)
            cur, span = [], 0.0
        cur.append(i)
        span += dur
    if cur:
        wins.append(cur)
    # 边界微调：若窗界线两侧 gap < 0.15s（话音连着），尝试向后挪到最近的 gap>=0.35s
    # ——实现成本高收益小，v1 不做（对齐窗各含 0.2s padding 已够）。
    return wins


def _cut_wav(ffmpeg, audio, s, e, out_wav):
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
           '-ss', f'{max(0.0, s):.3f}', '-to', f'{e:.3f}', '-i', str(audio),
           '-ac', '1', '-ar', '16000', str(out_wav)]
    r = subprocess.run(cmd, capture_output=True,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return r.returncode == 0 and out_wav.exists()


# ---------------- 窗级结果缓存（H-04：语句修正重复跑别把整个音频再算一遍） ----------------
# 缓存粒度 = 一个对齐窗。键 = 音频身份 + 窗绝对区间 + 窗内归一化词序列。
# 只要这三样没变，MMS_FA 前向的结果必然相同（纯函数），可以整段跳过切音频+前向+DP。
# 改句子的效果：只有含该句的窗失效，其余窗秒回——这才是「修一句不该等 15-30 秒」的正解。

def _win_key(audio, ws, we, wlist):
    h = hashlib.sha1()
    h.update(_CACHE_VER.encode('utf-8'))
    try:
        st = Path(audio).stat()
        h.update(f'|{Path(audio).name}|{st.st_size}|{int(st.st_mtime)}'.encode('utf-8'))
    except Exception:
        h.update(f'|{audio}'.encode('utf-8'))
    h.update(f'|{ws:.3f}|{we:.3f}|'.encode('utf-8'))
    h.update(' '.join(wlist).encode('utf-8'))
    return h.hexdigest()[:20]


def _win_load(cache_dir, key):
    """返回 [(s0, e0, score), ...]，未命中/损坏返回 None。"""
    try:
        p = Path(cache_dir) / 'fa' / f'w_{key}.json'
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding='utf-8'))
        if d.get('v') != _CACHE_VER:
            return None
        t = d.get('t') or []
        if not t:
            return None
        return [(float(a), float(b), float(c)) for a, b, c in t]
    except Exception:
        return None


def _win_save(cache_dir, key, triples):
    try:
        d = Path(cache_dir) / 'fa'
        d.mkdir(parents=True, exist_ok=True)
        p = d / f'w_{key}.json'
        tmp = p.with_suffix('.tmp')
        # 存全精度：二次四舍五入会让冷/热两路在第 3 位上分叉（0.1235→0.123 vs 0.124）
        tmp.write_text(json.dumps({'v': _CACHE_VER,
                                   't': [[float(a), float(b), float(c)]
                                         for a, b, c in triples]}), encoding='utf-8')
        tmp.replace(p)
        files = sorted(d.glob('w_*.json'), key=lambda x: x.stat().st_mtime)
        if len(files) > 800:               # 上限：超 800 个窗清最旧 300 个
            for f in files[:300]:
                f.unlink(missing_ok=True)
    except Exception:
        pass



def align_cues(audio, cues, ffmpeg, log=None, pause_wait=None, progress=None,
               cache_dir=None):
    """定稿 cues + 原音频 → 全量词级对齐。

    返回 (new_cues, stats, words)：
      new_cues  [{'start','end','text'},...] start/end=句首末词时间戳（词全丢的句保留原值）
      stats     {'total','aligned','kept','windows','secs'}
      words     [{'w','s','e','i'}]  i=句号（卡拉OK sidecar 直接可写）
    取消：pause_wait 返回真时抛 RuntimeError('已取消')。
    progress(done_wins, total_wins, phase_text)
    cache_dir 给了就按窗缓存前向结果（H-04），未变的窗秒回；不给则每次全算。
    """
    import torch

    t_all = time.time()
    model, tokenizer, aligner, sr = _get_model()
    import soundfile as _sf

    wins = _windows(cues)
    if log:
        est = sum(max(0.0, float(c['end']) - float(c['start'])) for c in cues) / 120.0 * 4
        log(f'本地强制对齐：{len(cues)} 句分 {len(wins)} 窗'
            f'（CPU 前向约 {est:.0f} 分钟，只算时间不动文本）')

    tmpdir = Path(tempfile.mkdtemp(prefix='ted_fa_'))
    out = [dict(c) for c in cues]
    words = []
    aligned = kept = cache_hits = 0
    cue_scores = {}                        # {句号: [词score,...]}（音文连贯性门用）
    try:
        for wi, idxs in enumerate(wins):
            if pause_wait and pause_wait():
                raise RuntimeError('已取消')
            ws = max(0.0, float(cues[idxs[0]]['start']) - 0.2)
            we = float(cues[idxs[-1]]['end']) + 0.2

            # 窗内词展开（句归属记录）——先算词再决定要不要前向：缓存键要用它
            wlist, owner = [], []
            for i in idxs:
                ws_i = norm_words(cues[i]['text'])
                owner.extend([i] * len(ws_i))
                wlist.extend(ws_i)
            if not wlist:                           # 窗内没有可对齐词（纯数字/空）
                kept += len(idxs)
                if progress:
                    progress(wi + 1, len(wins), '')
                continue

            # H-04：先查窗级缓存。命中就完全跳过切音频 + 前向 + CTC DP。
            wkey = _win_key(audio, ws, we, wlist) if cache_dir else None
            trip = _win_load(cache_dir, wkey) if wkey else None
            if trip is not None and len(trip) != len(wlist):
                trip = None                         # 词数对不上=损坏/口径变了，重算
            if trip is None:
                wav = tmpdir / f'w{wi:03d}.wav'
                if not _cut_wav(ffmpeg, audio, ws, we, wav):
                    raise RuntimeError(f'切窗 {wi} 失败（ffmpeg）')
                data, sr_w = _sf.read(str(wav), dtype='float32')
                if sr_w != sr:
                    import torchaudio
                    data = torchaudio.functional.resample(
                        torch.from_numpy(data).unsqueeze(0), sr_w, sr)[0].numpy()
                wave = torch.from_numpy(data).unsqueeze(0)
                with torch.inference_mode():
                    emission, _ = model(wave)      # (1, frames, cls)
                ratio = wave.size(1) / emission.size(1)

                toks = tokenizer(wlist)             # List[List[int]] per word
                spans = aligner(emission[0], toks)  # List[List[TokenSpan]] per word
                trip = []
                for k in range(len(wlist)):
                    if not spans[k]:
                        trip.append((-1.0, -1.0, -1.0))   # 该词无 span（对齐不上）
                        continue
                    s0 = spans[k][0].start * ratio / sr + ws
                    e0 = spans[k][-1].end * ratio / sr + ws
                    # 词 score = 该词各 token 的 CTC 平均概率：真说了的词 0.84-0.99，
                    # 音频里不存在的词（交错重复/多余文本）≈0.00（Bob .bak 实测）
                    wsc = sum(float(sp.score) for sp in spans[k]) / max(1, len(spans[k]))
                    trip.append((s0, e0, wsc))
                if wkey:
                    _win_save(cache_dir, wkey, trip)
            else:
                cache_hits += 1

            by_sent = {}
            for k, gi in enumerate(owner):
                s0, e0, wsc = trip[k]
                if s0 < 0:
                    continue
                by_sent.setdefault(gi, []).append((s0, e0))
                cue_scores.setdefault(gi, []).append(wsc)
                words.append({'w': wlist[k], 's': round(s0, 3), 'e': round(e0, 3),
                              'i': gi, 'sc': round(wsc, 3)})
            for i in idxs:
                if by_sent.get(i):
                    out[i]['start'] = round(by_sent[i][0][0], 3)
                    out[i]['end'] = round(by_sent[i][-1][1], 3)
                    aligned += 1
                else:
                    kept += 1
            if progress:
                progress(wi + 1, len(wins), f'{wi + 1}/{len(wins)} 窗')
        words.sort(key=lambda x: x['s'])
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    secs = round(time.time() - t_all, 1)
    # 句级连贯性证据（doctor 阶段 2.5' 音文校对门 / LLM 提示词消费）：
    #   matched 语义做了句判据二值化——健康句填满 total（无失配证据），
    #   可疑句给真实高分词数（如 2/20），避免 19/20 的轻微失配句被标⚠。
    per_cue = []
    for i in range(len(cues)):
        ss = cue_scores.get(i) or []
        if not ss:
            per_cue.append({'total': 0, 'matched': 0, 'shift_ms': 0, 'score': None})
            continue
        hi = sum(1 for x in ss if x >= 0.5)
        ok = hi * 2 >= len(ss)              # 过半词对上=健康
        per_cue.append({'total': len(ss), 'matched': len(ss) if ok else hi,
                        'shift_ms': 0, 'score': round(sum(ss) / len(ss), 3)})
    stats = {'total': len(cues), 'aligned': aligned, 'kept': kept,
             'windows': len(wins), 'secs': secs, 'per_cue': per_cue,
             'cacheHits': cache_hits}
    if log:
        _ch = f'，{cache_hits} 窗命中缓存跳过前向' if cache_hits else ''
        log(f'本地对齐完成：{aligned}/{len(cues)} 句锚定词级时间戳'
            f'（{kept} 句无可对齐词保留原时间，{secs:.0f}s{_ch}）')
    return out, stats, words
