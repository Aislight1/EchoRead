#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
听力练习模块

三种模式（逐句）：
    dictation  全文听写 —— 把整句打出来
    cloze      填空模式 —— 挖掉部分词，只打空缺
    first      首字母提示 —— 每个词只给首字母

三个难度（按句子本身自动分级）：
    simple / normal / complex
"""
import math
import random
import re

LEVELS = [
    ('simple',  '简单', '短句、常见词'),
    ('normal',  '普通', '常规句长，有一些从句'),
    ('complex', '复杂', '长句、难词、多重从句'),
]

BLANK_RATIO = {'simple': 0.20, 'normal': 0.33, 'complex': 0.50}

CLUE_WORDS = {
    'that', 'which', 'who', 'whom', 'whose', 'because', 'although', 'though',
    'while', 'whereas', 'unless', 'until', 'since', 'if', 'when', 'where',
    'however', 'therefore', 'moreover', 'nevertheless',
}


def classify(text):
    """按句子本身估难度，返回 simple / normal / complex。

    只看三个客观信号：词数、平均词长、是否有从句/连接词。
    启发式，不调用模型，完全离线、零成本。

    调参记录：最初把 because 这类连接词一律当"复杂"，结果
    "because I was going to do a speaking event."（9 个词）也被判成复杂，
    而且简单 166 / 普通 57 / 复杂 166，分布很歪。
    改成：连接词只在句子本身够长时才算复杂信号。
    """
    t = (text or '').strip()
    words = [w for w in re.split(r'\s+', t) if w]
    n = len(words)
    if n == 0:
        return 'simple'
    letters = re.sub(r"[^A-Za-z]", '', t)
    avg = len(letters) / max(1, n)
    low = t.lower()
    clues = any(re.search(r'\b' + c + r'\b', low) for c in CLUE_WORDS)
    commas = t.count(',') + t.count(';') + t.count(' - ')

    if n <= 8 and avg < 5.5:
        return 'simple'
    # 复杂：真的长，或者够长且带从句/多标点（超长句切分到 ≤16 词后仍能三档分布）
    if n >= 16 or (n >= 13 and (clues or commas >= 2)) or (n >= 10 and avg >= 7.2):
        return 'complex'
    return 'normal'


def level_of(text):
    return classify(text)


def make_cloze(parts, ratio):
    """从句子部件里挖空。parts 是 split_parts() 的结果。

    返回 (items, blanks)，items 里每个元素带 blank 标记。
    blanks 只装被挖掉的原词，给判分用。
    """
    idx = [i for i, p in enumerate(parts)
           if p.get('kind') == 'word' and len(p['text']) > 2]
    if not idx:
        idx = [i for i, p in enumerate(parts) if p.get('kind') == 'word']
    k = max(1, int(math.ceil(len(idx) * ratio))) if idx else 0
    picked = set(random.sample(idx, min(k, len(idx)))) if k else set()

    items, blanks = [], []
    for i, p in enumerate(parts):
        it = dict(p)
        it['blank'] = i in picked
        if it['blank'] and p.get('kind') == 'word':
            blanks.append(p['text'])
        items.append(it)
    return items, blanks


def split_parts(text):
    """把句子切成部件，标出哪些是词。

    词后面的标点/空格原样挂在该词的 tail 上（前端渲染 text+tail 即可
    还原完整句子，修复过"单词全连在一起"的显示 bug）；判分用的
    blanks/mask 只含纯词文本，不受影响。
    """
    out = []
    for tok in re.findall(r"[A-Za-z0-9']+|[^A-Za-z0-9']+", text or ''):
        if re.fullmatch(r"[A-Za-z0-9']+", tok):
            out.append({'kind': 'word', 'text': tok, 'tail': ''})
        elif tok.strip():
            if out and out[-1].get('kind') == 'word':
                out[-1]['tail'] = tok        # 标点（含其后空格）挂在词尾
            else:
                out.append({'kind': 'sep', 'text': tok})
        elif out and out[-1].get('kind') == 'word':
            out[-1]['tail'] += tok           # 词与词之间的纯空格也挂词尾
        # 开头纯空白：丢弃
    return [p for p in out if p['kind'] == 'word' or p['text'].strip() != '']


def first_letter_map(items):
    """首字母模式：给每个词的首字母（非空白的词都算）。"""
    return {i: (it['text'][:1] if it.get('kind') == 'word' else '')
            for i, it in enumerate(items)}


def build_exercise(text, mode='dictation', level=None, seed=None):
    """生成一道题。

    返回 dict：
        mode / level / answer(原句) / items(部件，带 blank) /
        prompt(要用户打的东西) / hint(提示) / blanks(被挖的词)
    """
    if seed is not None:
        random.seed(seed)
    lv = level or classify(text)
    parts = split_parts(text)
    items, blanks = make_cloze(parts, BLANK_RATIO.get(lv, 0.33))

    if mode == 'dictation':
        # 全文听写：不打散，只是把整句打出来
        for it in items:
            it['blank'] = False
        blanks = []
        prompt = ''
        hint = '把整句听写下来'

    elif mode == 'cloze':
        prompt = ' '.join('____' for _ in blanks)
        hint = f'共挖空 {len(blanks)} 个词'

    elif mode == 'first':
        # 首字母模式：每个词给首字母，其余用下划线
        for it in items:
            if it.get('kind') == 'word':
                it['blank'] = True
                w = it['text']
                it['mask'] = w[:1] + '_' * max(1, len(w) - 1) + it.get('tail', '')
            else:
                it['mask'] = it['text']
        blanks = [it['text'] for it in items if it.get('kind') == 'word']
        prompt = ''
        hint = f'共 {len(blanks)} 个词，只给首字母'

    else:
        prompt = ''
        hint = ''

    return {
        'ok': True,
        'mode': mode,
        'level': lv,
        'levelName': dict((k, n) for k, n, _d in LEVELS).get(lv, lv),
        'answer': text,
        'items': items,
        'blanks': blanks,
        'prompt': prompt,
        'hint': hint,
    }


def grade(answer, user_text, mode='dictation', blanks=None):
    """本地判分：把用户输入和标准答案逐词比对。

    复用 speech_asr.compare 的算法，保证和发音练习的判分口径一致。
    """
    import speech_asr
    if mode == 'cloze' and blanks:
        # 填空模式：只比被挖掉的那些词（按顺序）
        ref = ' '.join(blanks)
    else:
        ref = answer
    return speech_asr.compare(ref, user_text or '')


def score_of(cmp_result):
    """把比对结果折成 0-100 的整数分。"""
    if not cmp_result:
        return 0
    acc = float(cmp_result.get('accuracy') or 0)
    extra = cmp_result.get('extra') or 0
    pen = min(15, extra * 3)          # 多打词轻微扣分
    return max(0, min(100, int(round(acc - pen))))


def pick_sentences(cues, level=None, limit=200, offset=0):
    """从字幕句里按难度挑句子。

    cues 是 tl.parse_vtt_cues 的结果（en.vtt），句里可带 'level' 键
    （诊断修正写入的 AI 分级，见 app.subtitle_doctor 的 .lvl.json）——
    有 AI 分级优先用，没有回退本地 classify() 规则。
    返回带 index / level 的列表，index 指向原始句序，方便播放原音。
    """
    out = []
    for i, c in enumerate(cues):
        t = (c.get('text') or '').strip()
        if len(t) < 8:
            continue
        lv = (c.get('level') or '').strip()
        if lv not in ('simple', 'normal', 'complex'):
            lv = classify(t)
        if level and lv != level:
            continue
        out.append({'index': i, 'level': lv, 'text': t,
                    'start': float(c.get('start') or 0), 'end': float(c.get('end') or 0)})
    out = out[offset:] if offset else out
    return out[:limit]


def stats(cues):
    """统计一段材料里各难度的句子数。"""
    counts = {'simple': 0, 'normal': 0, 'complex': 0}
    for c in cues:
        t = (c.get('text') or '').strip()
        if len(t) < 8:
            continue
        lv = (c.get('level') or '').strip()
        if lv not in ('simple', 'normal', 'complex'):
            lv = classify(t)
        counts[lv] += 1
    counts['total'] = sum(counts.values())
    return counts




