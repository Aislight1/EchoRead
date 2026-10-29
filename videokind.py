# -*- coding: utf-8 -*-
"""来源类型分类（第 ⑤ 问：演讲 / 对话 / 读书 / 播客 应该用不同的处理方式）

为什么需要：同一条转写管线在吃四种完全不同的音视频——
  · talk      演讲（TED 类）：单人、有掌声笑声、语速中偏快、允许长句
  · reading   读书/朗读：单人、无背景声、语速稳、按章节；标点最规整，允许最长句
  · dialogue  对话/访谈对谈：两人以上、会抢话、停顿极短；**绝不能跨人并句**
  · podcast   播客/口播：单人、口语冗词与语气词多；并句可以长，但不能删口水词
用一个统一参数去处理它们，必然一边切太碎（读书被切碎）、一边并错人（对话被并成一句）。

设计原则：
1. **只调参数与提示词，不改管线结构。** 分类结果只影响
   `_split_long_cues` 的 max_words/max_dur/min_words、`assemble_sentences` 的
   max_words、转写模式倾向、以及 LLM 润色的提示词风格。
2. **落盘可追溯。** 分类写进 `<slug>.meta.json` 的 `sourceKind`（现有 meta 契约
   里加一个键，不新增文件），重跑后处理时读 meta 优先，不二次瞎猜。
3. **猜错也不致命。** 全部回落到 `other`＝今天的行为（max_words 45 / dur 12 / min 3），
   分类器只做"往更合适的方向挪一格"。

用法：
    import videokind
    kind = videokind.classify(title, url, meta)      # -> 'talk'|'reading'|'dialogue'|'podcast'|'other'
    r = videokind.rules(kind)                        # -> dict（见 RULES）
    videokind.label(kind)                            # -> '演讲'
"""
import re

# 类型 → 后处理参数
#   max_words  单句最多词数（超过在弱连接处再切）
#   max_dur    单句最多秒数（仅供 _split_long_cues 参考）
#   min_words  切出来的碎片不足这么多词就并回前段（防逐词念）
#   merge_gap  句子组装时允许合并的最大静音间隔（秒）：对话要小，读书可以大
#   para_min/para_max  含义段落的目标词数区间（第11问：同一段落的相邻完整句合到
#                      这个区间；读书 35-55 会让"整段长句流"合成一条，对话 8-16）
#   para_gap   含义段落合并允许的最大句间静音（秒），超过即断段
#   asr_mode   'auto'｜'parallel'｜'traditional'（长音频路由提示；auto=按模型能力自动）
#   style      LLM 润色/翻译提示词风格开关
RULES = {
    'talk':     dict(max_words=45, max_dur=14.0, min_words=3, merge_gap=1.2,
                     para_min=18, para_max=30, para_gap=1.2, asr_mode='auto', style='talk'),
    'reading':  dict(max_words=55, max_dur=18.0, min_words=3, merge_gap=1.6,
                     para_min=35, para_max=55, para_gap=1.6, asr_mode='auto', style='reading'),
    'dialogue': dict(max_words=28, max_dur=9.0,  min_words=2, merge_gap=0.45,
                     para_min=8,  para_max=16, para_gap=0.45, asr_mode='auto', style='dialogue'),
    'podcast':  dict(max_words=36, max_dur=11.0, min_words=2, merge_gap=0.9,
                     para_min=22, para_max=36, para_gap=0.9, asr_mode='auto', style='podcast'),
    'other':    dict(max_words=45, max_dur=12.0, min_words=3, merge_gap=1.0,
                     para_min=20, para_max=40, para_gap=1.0, asr_mode='auto', style='other'),
}

LABELS = {
    'talk': '演讲', 'reading': '读书朗读', 'dialogue': '对话访谈',
    'podcast': '播客口播', 'other': '未分类',
}

# 关键词表：先长后短、先专后泛，命中即返回
_KEYWORDS = [
    ('reading', ['全文朗读', '英音朗读', '美音朗读', '朗读', '读书', '有声书', '有声读物',
                 'audiobook', 'audio book', 'chapter', '章节', '原著', '小说', 'novel',
                 'read aloud', 'reading of']),
    ('dialogue', ['对话', '访谈', '对谈', '圆桌', '问答', '聊', '采访',
                  'interview', 'conversation', 'dialogue', 'discussion', 'roundtable',
                  'q&a', 'talk show', '对谈节目', '双人']),
    ('podcast', ['播客', '电台', '口播', '博客', 'podcast', 'radio', 'blog', 'monologue']),
    ('talk', ['ted', '演讲', 'talk', 'lecture', 'keynote', 'speech', '开讲', '公开课',
              'tutorial', 'course']),
]


def _norm(s):
    return (s or '').strip().lower()


def classify(title='', url='', meta=None):
    """按 标题 + 链接（+ 已有 meta）判断来源类型。永远返回一个合法 kind。"""
    meta = meta if isinstance(meta, dict) else {}
    # 1) meta 里已经判定过就直接沿用（人工 override 优先级最高）
    k = meta.get('sourceKind')
    if k in RULES:
        return k
    t = _norm(title) or _norm(meta.get('title'))
    u = _norm(url) or _norm(meta.get('url'))
    plat = _norm(meta.get('source') or meta.get('platform'))
    both = t + ' ' + u

    # 2) 平台/域名强特征
    if 'ted.com' in u or '/talks/' in u:
        return 'talk'
    if 'podcast' in both or 'xiaoyuzhoufm' in u:
        return 'podcast'

    # 3) 关键词（顺序＝优先级：读书 > 对话 > 播客 > 演讲）
    for kind, words in _KEYWORDS:
        for w in words:
            if w in both:
                return kind

    # 4) 结构特征兜底：超长单文件 + 章节编号像有声书；B 站合集且标题含"第 N 集"多为剧集/对话
    dur = 0
    try:
        dur = float(meta.get('duration') or 0)
    except Exception:
        dur = 0
    if dur >= 1800 and re.search(r'(chapter|chapter\s*\d+|第\s*[0-9一二三四五六七八九十]+\s*章)', _norm(t)):
        return 'reading'
    if plat in ('bilibili', 'youtube') and re.search(r'(合集|全集|ep?\s*\d+|第\s*\d+\s*[集期])', _norm(t)):
        return 'dialogue'
    return 'other'


def rules(kind):
    """取某类型的后处理参数（未知类型回落 other，永不抛错）。"""
    return dict(RULES.get(kind) or RULES['other'])


def label(kind):
    return LABELS.get(kind) or LABELS['other']


def describe(kind):
    """给人看的一行说明（写进日志/报告，让用户知道"为什么这次不一样"）。"""
    r = rules(kind)
    return (f'{label(kind)}（单句≤{r["max_words"]}词/≤{r["max_dur"]:.0f}s，'
            f'含义段落 {r["para_min"]}-{r["para_max"]} 词，合并静音≤{r["merge_gap"]}s）')
