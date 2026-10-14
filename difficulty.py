# -*- coding: utf-8 -*-
"""听力难度离线评级（零 token）：语速 / 句子复杂度 / 词汇难度 三维各 0-2 分求和映射 L1-L5。

分档标准来自 Opus 5.5 会诊（_tools/_opus_gist.md）：
  语速  <2.0=0分  2.0-2.5=1分  >2.5=2分（词/秒）
  句长  均<8且P90<12=0分  均>12或P90>18=2分  其余=1分（词）
  长词  <15%=0分  15-30%=1分  >30%=2分（≥8字母词占比）
  求和 0-6 → L1 L1 L2 L3 L4 L5 L5
已知坑：字幕时间戳含停顿会让语速偏低（偏保守，宁易勿难，可接受）。
"""

import re

LEVELS = {
    'L1': ('入门', '慢速清晰、短句高频词，类似儿童故事/教学视频'),
    'L2': ('初级', '自然对话速，日常表达为主，约 CEFR B1'),
    'L3': ('中级', '标准演讲语速+中等句长，需要抓细节，约 B1-B2'),
    'L4': ('高级', '语速快/长句多/学术词密集，约 B2-C1'),
    'L5': ('征服', '高速+复杂句式+生僻概念，约 C1-C2'),
}


def _words(text):
    return re.findall(r"[A-Za-z']+", str(text or ''))


def compute(cues):
    """cues: [{start,end,text}]（英文字幕句）。样本太少返回 None。"""
    rows = []
    for c in cues or []:
        ws = _words(c.get('text'))
        if not ws:
            continue
        dur = float(c.get('end') or 0) - float(c.get('start') or 0)
        rows.append({'n': len(ws), 'dur': max(0.3, dur)})
    total_w = sum(r['n'] for r in rows)
    if total_w < 60 or len(rows) < 5:
        return None
    total_t = sum(r['dur'] for r in rows)
    rate = total_w / max(1.0, total_t)
    lens = sorted(r['n'] for r in rows)
    avg_len = total_w / len(rows)
    p90 = lens[min(len(lens) - 1, int(len(lens) * 0.9))]
    allw = [w for c in (cues or []) for w in _words(c.get('text'))]
    long_ratio = sum(1 for w in allw if len(w) >= 8) / max(1, len(allw))

    s_rate = 0 if rate < 2.0 else (1 if rate <= 2.5 else 2)
    s_len = 0 if (avg_len < 8 and p90 < 12) else (2 if (avg_len > 12 or p90 > 18) else 1)
    s_lex = 0 if long_ratio < 0.15 else (1 if long_ratio <= 0.30 else 2)
    score = s_rate + s_len + s_lex
    level = ('L1', 'L1', 'L2', 'L3', 'L4', 'L5', 'L5')[max(0, min(6, score))]
    name, desc = LEVELS[level]
    return {'level': level, 'name': name, 'desc': desc,
            'score': score, 'sRate': s_rate, 'sLen': s_len, 'sLex': s_lex,
            'rate': round(rate, 2), 'avgLen': round(avg_len, 1), 'p90': p90,
            'longRatio': round(long_ratio, 2), 'sentences': len(rows), 'words': total_w}
