# -*- coding: utf-8 -*-
"""Token 计价：按 (渠道 host, 模型) 匹配官方单价折算费用。

- 官方价内置在 PRICING（随版本更新，每条带 source_url/checked_at）；
- 用户自定义渠道/倍率放 config 的 pricingOverrides（手工编辑 config.json）；
- 匹配不到的模型返回 None，界面显示「未定价」，不冒充 0。

价格单位：每百万 token，人民币。价格为编写时公开页面的近似值，
请以各官方定价页为准（usage 页会标注估算）。
"""

PRICING = {
    'channels': {
        'dashscope': {'label': '阿里云百炼', 'hosts': ['dashscope.aliyuncs.com'], 'currency': 'CNY',
                      'source': 'https://help.aliyun.com/zh/model-studio/models', 'checked_at': '2026-10-09'},
        'deepseek': {'label': 'DeepSeek 官方', 'hosts': ['api.deepseek.com'], 'currency': 'CNY',
                     'source': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing', 'checked_at': '2026-10-09'},
        'volc': {'label': '火山方舟', 'hosts': ['ark.cn-beijing.volces.com'], 'currency': 'CNY',
                 'source': 'https://www.volcengine.com/docs/82379', 'checked_at': '2026-10-09'},
        'mimo': {'label': '小米 MiMo', 'hosts': ['api.xiaomimimo.com'], 'currency': 'CNY',
                 'source': 'https://platform.xiaomimimo.com', 'checked_at': '2026-10-09'},
        'openai': {'label': 'OpenAI 官方', 'hosts': ['api.openai.com'], 'currency': 'USD',
                   'source': 'https://openai.com/api/pricing', 'checked_at': '2026-10-09'},
    },
    # input/output：每百万 token 价格；None=未知（显示未定价）
    'prices': [
        # 阿里云百炼
        {'channel': 'dashscope', 'match': ['qwen-plus', 'qwen-plus-latest'], 'input': 0.8, 'output': 2.0},
        {'channel': 'dashscope', 'match': ['qwen-turbo', 'qwen-turbo-latest'], 'input': 0.3, 'output': 0.6},
        {'channel': 'dashscope', 'match': ['qwen-max'], 'input': 2.4, 'output': 9.6},
        # DeepSeek 官方
        {'channel': 'deepseek', 'match': ['deepseek-chat', 'deepseek-v3'], 'input': 2.0, 'output': 8.0},
        {'channel': 'deepseek', 'match': ['deepseek-reasoner', 'deepseek-r1'], 'input': 4.0, 'output': 16.0},
        # 火山方舟（model 常是接入点 ep-xxx，请在 config pricingOverrides 里映射）
        {'channel': 'volc', 'match': ['doubao-pro-32k', 'doubao-1.5-pro-32k'], 'input': 0.8, 'output': 2.0},
        {'channel': 'volc', 'match': ['doubao-lite-32k'], 'input': 0.3, 'output': 0.6},
        # 小米 MiMo（限时免费 → 0）
        {'channel': 'mimo', 'match': ['mimo-v2.5'], 'input': 0.0, 'output': 0.0},
    ],
}


def _host_of(base_url):
    import re as _re
    m = _re.search(r'https?://([^/]+)', base_url or '')
    return (m.group(1) if m else '').lower()


def _price_for(channel_id, model):
    """按模型名匹配单价（精确 → 前缀）。"""
    model = (model or '').lower()
    for p in PRICING['prices']:
        if p.get('channel') != channel_id:
            continue
        for name in p.get('match', []):
            if model == name or model.startswith(name):
                return p
    return None


def channel_of(base_url):
    """按 host 反查渠道 id。"""
    host = _host_of(base_url)
    for cid, ch in PRICING['channels'].items():
        for h in ch['hosts']:
            if host == h.lower() or host.endswith('.' + h.lower()):
                return cid
    return None


def estimate(model, base_url, prompt_tokens, completion_tokens, overrides=None):
    """估算一次调用的费用。返回 (cost, currency, channel_id, priced)。"""
    prompt_tokens = prompt_tokens or 0
    completion_tokens = completion_tokens or 0
    cid = channel_of(base_url)
    cur = PRICING['channels'].get(cid, {}).get('currency', 'CNY')
    p = _price_for(cid, model) if cid else None

    # 用户自定义覆盖（config.pricingOverrides：[{match, input, output, currency, multiplier}]）
    for o in (overrides or []):
        names = o.get('match') or []
        if isinstance(names, str):
            names = [names]
        for name in names:
            if model.lower() == str(name).lower():
                inp, outp = o.get('input'), o.get('output')
                if inp is None or outp is None:
                    break
                cost = (prompt_tokens * inp + completion_tokens * outp) / 1e6
                mult = o.get('multiplier')
                if mult:
                    cost *= float(mult)
                return (cost, o.get('currency') or 'CNY', o.get('channel') or cid or 'custom', True)

    if p and p.get('input') is not None:
        cost = (prompt_tokens * (p['input'] or 0) + completion_tokens * (p['output'] or 0)) / 1e6
        return (cost, cur, cid, True)
    return (None, cur, cid, False)
