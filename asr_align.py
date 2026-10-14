# -*- coding: utf-8 -*-
"""ASR 强制对齐（P3-1 架构改造）：字幕句时间轴 ← paraformer 词级时间戳。

用户拍板的架构（业界验证：haoone-app qwen3-asr 自研对齐 98%）：
  现状 = 字幕句切音频段 → 猜测时间轴（LLM 只能改文本改不了时间）；
  目标 = 音频段送 ASR 转写 → **只取词级时间戳、不采转写文本** →
         字幕词序与 ASR 词序贪心对齐 → 句子的 start/end 替换为
         首末匹配词的实际时间。文本永远以字幕为准。

成本：paraformer 录音文件识别约 0.29 元/时，一句 10 秒的修正 ≈ 0.0008 元。

设计底线：
  - 匹配率低于 50% 的句**保留原时间戳**（宁缺毋滥，不拿烂匹配硬改）；
  - 对齐结果保持单调不减，与窗外句子不重叠；
  - 任何失败（无 ffmpeg / ASR 报错 / 结果为空）→ 原样返回，绝不妨碍主流程。

P0-A 提速：分组并行（ThreadPoolExecutor，默认 3 路上限 6）。
线程安全：recognize() 每次调用自建 Recognition 实例、api_key 全局只读，
任务间无共享可变状态；日志攒列表回主线程统一输出。

P0-B 失配证据：per_cue 逐句记录匹配率/偏移，供 LLM 提示词优先处理低匹配句。
"""
import re
import subprocess
import tempfile
import shutil
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


def _norm(w):
    """词归一化：小写 + 去掉非字母数字撇号（ASR 与字幕的标点/大小写差异全抹平）。"""
    return re.sub(r"[^a-z0-9']", '', (w or '').lower())


def _cut_segment(ffmpeg, audio, s, e, out):
    """ffmpeg 截 [s,e] → 16k 单声道 wav（ASR 输入格式）。"""
    c = subprocess.run(
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
         '-ss', f'{max(0.0, s):.3f}', '-to', f'{max(0.0, e):.3f}', '-i', str(audio),
         '-ac', '1', '-ar', '16000', str(out)],
        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return c.returncode == 0 and out.exists() and out.stat().st_size > 44


def _align_group(cue_words, asr_words):
    """一组句（时间连续）与一段 ASR 词时间线做顺序贪心对齐，产出证据。

    cue_words: [[w1,w2,...] 每句的归一化词表]
    asr_words: [{w,start,end}...]（已加偏移，绝对时间）
    返回每句 ((new_start, new_end) or None, matched, total)。
    matched=对上的词数，total=该句词数；未对齐的 new_start/end=None。
    """
    n = len(asr_words)
    p = 0
    out = []
    for idx, ws in enumerate(cue_words):
        total = len(ws)
        need = max(1, (total + 1) // 2)
        if not ws:
            out.append((None, 0, 0))
            continue
        anchor_pos = None
        for k in range(p, min(n, p + 25)):
            if asr_words[k]['w'] == ws[0] or (len(ws) > 1 and asr_words[k]['w'] == ws[1]):
                anchor_pos = k
                break
        if anchor_pos is None:
            out.append((None, 0, total))
            continue
        matched_idx = []
        ai = anchor_pos
        for w in ws:
            hit = None
            for k in range(ai, min(n, ai + 12)):
                if asr_words[k]['w'] == w:
                    hit = k
                    break
            if hit is None:
                continue
            matched_idx.append(hit)
            ai = hit + 1
        matched = len(matched_idx)
        if matched < need:
            out.append((None, matched, total))
            continue
        first, last = matched_idx[0], matched_idx[-1]
        s = max(0.0, asr_words[first]['start'] - 0.05)
        e = asr_words[last]['end'] + 0.10
        if e - s < 0.15:
            out.append((None, matched, total))
            continue
        out.append(((s, e), matched, total))
        p = last + 1
    return out


def _is_pathological(c):
    """A1：语速异常句判定（时间戳塌缩/虚长）——统一口径 >5 词/s 或 <0.9 词/s（≥4 词）。"""
    n = len((c.get('text') or '').split())
    if not n:
        return False
    s = float(c.get('start') or 0)
    e = float(c.get('end') or 0)
    if e <= s:
        return True
    rate = n / (e - s)
    return rate > 5.0 or (n >= 4 and rate < 0.9)


def _repair_pathological(cues, audio, asr, ffmpeg, tmpdir, log, win, pause_wait):
    """A1 修复 pass：语速异常句（塌缩/虚长）的邻域文稿定位。

    死锁根源：常规对齐拿句自己的坏时间窗截音频→窗里没语音→匹配 0→保留坏时间。
    修法（haoone 文稿匹配路线）：对异常句簇取前后正常句为锚的邻域整段转写，
    在转写词流里重新定位每句文本的实际位置回填时间。定位不到的保留原时间
    （由 per_cue 证据标记，v4 模板下 LLM 不再据此改文本）。
    """
    path_set = set(i for i, c in enumerate(cues) if _is_pathological(c))
    if not path_set:
        return 0
    # 窗口模式只修与窗相关的异常句（省钱）；全文模式全修
    if win:
        path_set = set(i for i in path_set
                       if float(cues[i]['end']) > win[0] - 30 and float(cues[i]['start']) < win[1] + 30)
        if not path_set:
            return 0
    # 连续/近邻异常句并簇
    clusters, cur = [], []
    for i in sorted(path_set):
        if cur and i - cur[-1] <= 2:
            cur.append(i)
        else:
            if cur:
                clusters.append(cur)
            cur = [i]
    if cur:
        clusters.append(cur)

    repaired = 0
    for cl in clusters:
        try:
            if pause_wait:
                pause_wait()
        except Exception:
            break          # 取消信号：立即停止邻域转写（已修好的保留，费用已花的不退）
        i0, i1 = cl[0], cl[-1]
        li = i0 - 1
        while li >= 0 and li in path_set:
            li -= 1
        ri = i1 + 1
        while ri < len(cues) and ri in path_set:
            ri += 1
        ws = float(cues[li]['end']) + 0.2 if li >= 0 else max(0.0, float(cues[i0]['start']) - 8.0)
        we = float(cues[ri]['start']) - 0.2 if ri < len(cues) else float(cues[i1]['end']) + 20.0
        # 窗口保底：按簇内词量估时长（0.4s/词），封顶 90s
        need = max(8.0, sum(len((cues[i].get('text') or '').split()) for i in cl) * 0.4)
        if we - ws < need:
            we = min(ws + need, ws + 90.0)
        if we - ws < 2.0:
            continue
        seg = tmpdir / f'rep{i0:04d}.wav'
        if not _cut_segment(ffmpeg, audio, ws, we, seg):
            continue
        try:
            res = asr.recognize(seg, 'en')
        except Exception:
            time.sleep(1.0)
            try:
                res = asr.recognize(seg, 'en')
            except Exception:
                continue
        off = ws
        asr_words = [{'w': _norm(w.get('text')), 'start': float(w.get('start') or 0) + off,
                      'end': float(w.get('end') or 0) + off}
                     for w in (res.get('words') or [])]
        asr_words = [w for w in asr_words if w['w']]
        if not asr_words:
            continue
        cue_words = [[_norm(t) for t in re.split(r"\s+", (cues[i].get('text') or '')) if _norm(t)]
                     for i in cl]
        results = _align_group(cue_words, asr_words)
        for i, (pr, matched, total) in zip(cl, results):
            if pr:
                s = min(max(pr[0], ws), we - 0.2)
                e = min(max(pr[1], s + 0.3), we)
                cues[i]['start'] = round(s, 3)
                cues[i]['end'] = round(e, 3)
                repaired += 1
    if log:
        log(f'A1 修复：{len(path_set)} 句语速异常（塌缩/虚长），邻域定位成功 {repaired} 句')
    return repaired


def align_cues(audio, cues, asr, log=None, win=None, ffmpeg=None, pause_wait=None, workers=3):
    """对字幕句做 ASR 强制对齐（分组并行），产出逐句证据。

    audio: 音频文件路径；cues: [{start,end,text}...]（浅拷贝改写 start/end，不动调用方原列表）
    win: (s,e) 只对齐这个时间窗（scope=one/multi），None=全部（按 ≤240s 分组）
    ffmpeg: ffmpeg 路径；pause_wait: 每组任务开头调用的可阻塞回调（doctor 暂停点）
    workers: 并发路数（默认 3，钳到 1..6）
    返回 (new_cues, stats)。stats 新增 'per_cue'=[{matched,total,shift_ms,aligned}...]（与入参 cues 等长）。
    任何失败 → (原 cues, {'aligned':0,..., 'per_cue':[None]*len})。
    """
    if not ffmpeg:
        if log:
            log('ASR 对齐跳过：未找到 ffmpeg')
        return cues, {'total': len(cues), 'aligned': 0, 'kept': len(cues), 'error': 'no-ffmpeg',
                      'per_cue': [None] * len(cues)}
    if getattr(asr, 'provider', lambda: '')() == 'mimo':
        if log:
            log('ASR 对齐跳过：MiMo 不返回词级时间戳（设置里换阿里云识别即可启用）')
        return cues, {'total': len(cues), 'aligned': 0, 'kept': len(cues), 'error': 'no-word-ts',
                      'per_cue': [None] * len(cues)}

    audio = Path(audio)
    orig_cues = cues
    cues = [dict(c) for c in cues]
    per_cue = [None] * len(cues)
    cue_to_idx = {id(c): i for i, c in enumerate(cues)}

    tmpdir = Path(tempfile.mkdtemp(prefix='ted_align_'))
    # ---- A1 修复 pass（先修坏窗，常规对齐才有意义）----
    try:
        _repair_pathological(cues, audio, asr, ffmpeg, tmpdir, log, win, pause_wait)
    except Exception as e:
        if log:
            log(f'A1 修复 pass 异常（跳过，不影响主流程）：{str(e)[:80]}')

    if win:
        groups = [[c for c in cues
                   if float(c['start']) < win[1] + 0.05 and float(c['end']) > win[0] - 0.05]]
    else:
        groups, cur, span = [], [], 240.0
        for c in cues:
            if cur and float(c['end']) - float(cur[0]['start']) > span:
                groups.append(cur)
                cur = []
            cur.append(c)
        if cur:
            groups.append(cur)

    workers = max(1, min(int(workers or 3), 6))
    aligned_n = kept_n = 0

    def _group_task(gi):
        """单组任务：暂停检查→截音频→recognize→贪心对齐→返回 (gi, results, kept, err)。
        results=[(pair, matched, total), ...]（与 group 等长）；工作线程不调 log。"""
        group = groups[gi]
        if not group:
            return (gi, [], 0, None)
        try:
            if pause_wait:
                pause_wait()
        except Exception:
            # 取消信号在工人线程里不能吞掉——立刻空手返回，否则每组的 ASR 照跑，
            # 用户点 ⏹ 后对齐还在继续烧识别
            return (gi, [(None, 0, 0)] * len(group), len(group), '已取消')
        gs = min(float(c['start']) for c in group)
        ge = max(float(c['end']) for c in group)
        seg = tmpdir / f'g{gi:03d}.wav'
        if not _cut_segment(ffmpeg, audio, gs - 0.3, ge + 0.5, seg):
            return (gi, [(None, 0, 0)] * len(group), len(group), '截音频失败')

        def _do_recognize():
            res = asr.recognize(seg, 'en')
            return res.get('words') or []

        try:
            try:
                words = _do_recognize()
            except Exception as e:
                time.sleep(1.0 + gi * 0.3)
                try:
                    words = _do_recognize()
                except Exception:
                    return (gi, [(None, 0, 0)] * len(group), len(group), f'ASR失败({str(e)[:50]})')
        except Exception as e:
            return (gi, [(None, 0, 0)] * len(group), len(group), f'ASR失败({str(e)[:50]})')
        if not words:
            return (gi, [(None, 0, 0)] * len(group), len(group), None)

        off = gs - 0.3
        asr_words = [{'w': _norm(w.get('text')), 'start': float(w.get('start') or 0) + off,
                      'end': float(w.get('end') or 0) + off} for w in words]
        asr_words = [w for w in asr_words if w['w']]
        cue_words = [[_norm(t) for t in re.split(r"\s+", (c.get('text') or '')) if _norm(t)]
                     for c in group]
        results = _align_group(cue_words, asr_words)
        ok = sum(1 for x in results if x[0])
        return (gi, results, len(group) - ok, None)

    try:
        results_all = [None] * len(groups)
        log_msgs = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_group_task, gi): gi for gi in range(len(groups))}
            for fut in as_completed(futs):
                gi = futs[fut]
                try:
                    gi, results, kept, err = fut.result()
                except Exception as e:
                    results, kept, err = [(None, 0, 0)] * len(groups[gi]), len(groups[gi]), f'任务异常({str(e)[:40]})'
                results_all[gi] = (results, kept, err)
                if err:
                    log_msgs.append(f'对齐分组 {gi + 1}/{len(groups)}：{err}，{len(groups[gi])} 句保留原时间')
                else:
                    log_msgs.append(f'对齐分组 {gi + 1}/{len(groups)}：'
                                    f'{len(groups[gi]) - kept}/{len(groups[gi])} 句校准')
        for msg in log_msgs:
            if log:
                log(msg)

        # 按组序应用对齐结果（主线程，无竞争）。prev_end 跨组连续：
        # 若每组重置为组首句原 start，句首就"只能后移不能前移"——时间戳偏慢的句修不了
        prev_end = 0.0
        for gi, group in enumerate(groups):
            if results_all[gi] is None:
                kept_n += len(group)
                for c in group:
                    idx = cue_to_idx.get(id(c))
                    if idx is not None:
                        per_cue[idx] = {'matched': 0, 'total': 0, 'shift_ms': 0, 'aligned': False}
                continue
            results, kept, err = results_all[gi]
            kept_n += kept
            for c, (pr, matched, total) in zip(group, results):
                idx = cue_to_idx.get(id(c))
                orig_start = float(c['start'])
                if pr:
                    s = max(pr[0], prev_end)
                    e = max(s + 0.15, pr[1])
                    shift_ms = int(round((s - orig_start) * 1000))
                    c['start'] = round(s, 3)
                    c['end'] = round(e, 3)
                    prev_end = e
                    aligned_n += 1
                    if idx is not None:
                        per_cue[idx] = {'matched': matched, 'total': total, 'shift_ms': shift_ms, 'aligned': True}
                else:
                    prev_end = max(prev_end, float(c.get('end') or 0))
                    if idx is not None:
                        per_cue[idx] = {'matched': matched, 'total': total, 'shift_ms': 0, 'aligned': False}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return cues, {'total': len(cues), 'aligned': aligned_n, 'kept': kept_n, 'per_cue': per_cue}


def realign_all(audio, cues, asr, ffmpeg, tmpdir, duration, log=None, pause_wait=None):
    r"""时间轴出生即坏时的治本对齐（把 agent 手工修复流程产品化）。

    场景：转写时没拿到时间戳（如 MiMo 只回纯文本），字幕时间是按词数编造的
    ——中位语速 5+ 词/秒、后半段音频完全没有字幕对应。此时原地微调/校准/
    逐窗对齐都无意义：句子的音频根本不在它声称的窗口里。唯一解法 = 整段
    重新转写拿真实词级时间流，把现有字幕文本（可能已被 LLM 校对过）逐句
    在词流里重新定位。文本以字幕为准（一个字不动），时间以重转写为准。

    返回 (cues, stats)，stats 与 align_cues 同构（aligned/total/kept/per_cue）。
    未锚定（匹配率<50%）的句在最近两个锚点间按词数线性插值，绝不保留假时间。
    """
    if not cues:
        return cues, {'total': 0, 'aligned': 0, 'kept': 0, 'per_cue': []}
    dur = float(duration or 0)
    if not dur or dur < 2:
        raise RuntimeError('音频时长未知，无法全文重定位')
    if dur > 5400:
        raise RuntimeError('音频超过 90 分钟，整段重转写定位耗时过长——建议重新生成字幕')
    seg = Path(tmpdir) / 'reall.wav'
    if not _cut_segment(ffmpeg, audio, 0, dur, seg):
        raise RuntimeError('截取整段音频失败')
    if pause_wait:
        pause_wait()
    try:
        try:
            res = asr.recognize(str(seg), 'en')
        except Exception:
            time.sleep(1.5)
            res = asr.recognize(str(seg), 'en')
    except Exception as e:
        raise RuntimeError('整段重转写失败：' + str(e)[:120])
    words = res.get('words') or []
    if not words:
        raise RuntimeError('重转写没有返回词级时间戳（对齐引擎须为阿里云 paraformer）')
    asr_words = [{'w': _norm(w.get('text')),
                  'start': float(w.get('start') or 0),
                  'end': float(w.get('end') or 0)} for w in words]
    asr_words = [w for w in asr_words if w['w']]
    if not asr_words:
        raise RuntimeError('重转写词流为空')
    if pause_wait:
        pause_wait()
    if log:
        log(f'重转写完成：{len(asr_words)} 个词的时间流，开始逐句定位…')
    cue_words = [[_norm(t) for t in re.split(r"\s+", (c.get('text') or '')) if _norm(t)]
                 for c in cues]
    orig_starts = [float(c.get('start') or 0) for c in cues]
    results = _align_group(cue_words, asr_words)
    per_cue = [None] * len(cues)
    anchored = []                     # [(idx, s, e), ...] 已定位句
    for i, (pair, matched, total) in enumerate(results):
        if pair:
            s = min(max(pair[0], 0.0), dur - 0.2)
            e = min(max(pair[1], s + 0.3), dur)
            cues[i]['start'], cues[i]['end'] = round(s, 3), round(e, 3)
            anchored.append((i, s, e))
            per_cue[i] = {'matched': matched, 'total': total,
                          'shift_ms': round((s - orig_starts[i]) * 1000), 'aligned': True}
    # 未锚定句：在锚点间按词数线性插值（绝不保留编造的旧时间）
    wc = [len(w) for w in cue_words]
    for i, (pair, _m, _t) in enumerate(results):
        if pair:
            continue
        li = max((a for a in anchored if a[0] < i), default=None, key=lambda a: a[0])
        ri = min((a for a in anchored if a[0] > i), default=None, key=lambda a: a[0])
        s0 = li[2] if li else 0.0
        e0 = ri[1] if ri else dur
        gap = max(0.3, e0 - s0)
        span = range((li[0] + 1) if li else 0, (ri[0]) if ri else len(cues))
        tw = sum(wc[j] for j in span) or 1
        acc = 0.0
        for j in span:
            share = gap * (wc[j] / tw)
            s = s0 + acc
            acc += share
            cues[j]['start'] = round(s, 3)
            cues[j]['end'] = round(min(s + max(share, 0.3), e0 if ri else dur), 3)
        per_cue[i] = {'matched': _m, 'total': _t, 'shift_ms': 0, 'aligned': False}
    # 单调化 + 地板（插值与锚点交界可能轻微重叠）
    prev_end = 0.0
    for c in cues:
        s = float(c['start'] or 0)
        e = float(c['end'] or 0)
        if s < prev_end:
            s = prev_end
        if e <= s:
            e = s + 0.3
        c['start'], c['end'] = round(s, 3), round(e, 3)
        prev_end = e
    if log:
        log(f'全文重定位：{len(anchored)}/{len(cues)} 句锚定到真实时间流，'
            f'其余按词数在锚点间插值')
    return cues, {'total': len(cues), 'aligned': len(anchored),
                  'kept': len(cues) - len(anchored), 'per_cue': per_cue}
