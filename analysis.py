#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析层：LLM 纠音 / 释义 / 总结 + 生词本

LLM 走「OpenAI 兼容」协议，所以任何兼容接口都能用
（官方 OpenAI、DeepSeek、通义、方舟、本地 vLLM…），在设置里填 base_url + key + 模型名即可。
"""
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import store  # noqa: E402
import prompts  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


# ---------------------------------------------------------------- LLM
class LLM:
    def __init__(self, cfg=None, log=None):
        self.cfg = cfg or {}
        self.log = log
        self._client = None
        self.usage = {'calls': 0, 'prompt': 0, 'completion': 0}
        self.model_usage = {}
        self._usage_log_path = None

    def _say(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    @property
    def ready(self):
        return bool((self.cfg.get('llmApiKey') or '').strip()
                    and (self.cfg.get('llmBaseUrl') or '').strip())

    def _get_client(self):
        if self._client is not None:
            return self._client
        key = (self.cfg.get('llmApiKey') or '').strip()
        base = (self.cfg.get('llmBaseUrl') or '').strip()
        if not key or not base:
            raise RuntimeError('还没配 LLM（去 设置 → 对话与纠错 里填 base_url / key / 模型名）')
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError('缺少 openai 库：pip install openai')
        self._client = OpenAI(api_key=key, base_url=base, timeout=90)
        return self._client

    def set_usage_log(self, path):
        """设置 token 用量日志（JSONL）落盘路径。"""
        self._usage_log_path = path

    def _log_usage(self, model, u, tag):
        """按模型累计（内存）+ 追加 JSONL（持久化，Token 用量页数据源）。"""
        try:
            p = int(getattr(u, 'prompt_tokens', 0) or 0)
            c = int(getattr(u, 'completion_tokens', 0) or 0)
        except Exception:
            p = c = 0
        mu = self.model_usage.setdefault(model, {'calls': 0, 'prompt': 0, 'completion': 0})
        mu['calls'] += 1
        mu['prompt'] += p
        mu['completion'] += c
        if getattr(self, '_usage_log_path', None):
            try:
                from datetime import datetime as _dt
                line = json.dumps({'at': _dt.now().strftime('%Y-%m-%d %H:%M:%S'),
                                   'model': model, 'tag': tag or '其他',
                                   'prompt': p, 'completion': c},
                                  ensure_ascii=False)
                with open(self._usage_log_path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass

    def usage_by_model(self):
        return {k: dict(v) for k, v in self.model_usage.items()}

    def chat(self, system, user, model=None, temperature=0.3, max_tokens=1500, tag='', json_mode=False):
        client = self._get_client()
        model = model or (self.cfg.get('llmModel') or 'gpt-4o-mini').strip()
        self._say(f'调用 LLM（{model}）…')
        kw = dict(model=model,
                  messages=[{'role': 'system', 'content': system},
                            {'role': 'user', 'content': user}],
                  temperature=temperature,
                  max_tokens=max_tokens)
        # deepseek 系开 JSON 模式可减少围栏/废话；网关不认这个参数就去掉重试（其它模型不冒险）
        if json_mode and 'deepseek' in model.lower():
            kw['response_format'] = {'type': 'json_object'}
            try:
                r = client.chat.completions.create(**kw)
            except Exception:
                kw.pop('response_format', None)
                r = client.chat.completions.create(**kw)
        else:
            r = client.chat.completions.create(**kw)
        try:
            self._last_finish = (r.choices[0].finish_reason or '') if r.choices else ''
        except Exception:
            self._last_finish = ''
        u = getattr(r, 'usage', None)
        if u is not None:
            # 粗略记账：不追求精确，只为了让你看到"这个功能花不花 token"
            try:
                self.usage['calls'] += 1
                self.usage['prompt'] += int(getattr(u, 'prompt_tokens', 0) or 0)
                self.usage['completion'] += int(getattr(u, 'completion_tokens', 0) or 0)
            except Exception:
                pass
            self._log_usage(model, u, tag)
        return (r.choices[0].message.content or '').strip()

    def chat_json(self, system, user, model=None, max_tokens=2000, tag=''):
        """要求模型返回 JSON，并尽量稳健地解析出来。"""
        sys_p = (system + '\n\n只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块围栏。')
        txt = self.chat(sys_p, user, model=model, temperature=0.2, max_tokens=max_tokens,
                        tag=tag, json_mode=True)
        # 截断检测：finish_reason=length 说明 JSON 尾巴被砍（缺 ]}），解析必挂——翻倍重试一次
        if getattr(self, '_last_finish', '') == 'length' and max_tokens < 12000:
            self._say('输出被截断（finish_reason=length），加大 max_tokens 重试…')
            try:
                txt2 = self.chat(sys_p, user, model=model, temperature=0.2,
                                 max_tokens=min(max_tokens * 2, 12000),
                                 tag=tag, json_mode=True)
                # 重试仍截断但更长的也收：解析层还有截断修复兜底
                if len(txt2) > len(txt) or getattr(self, '_last_finish', '') != 'length':
                    txt = txt2
            except Exception:
                pass          # 重试失败就用原文本走修复解析
        return parse_json_loose(txt)

    def test(self):
        try:
            out = self.chat('你是一个测试助手。', '只回复两个字：正常', max_tokens=20)
            return {'ok': True, 'msg': f'连通正常，模型回复：{out[:40]}'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------- 模型管理 ----------
    def list_models(self):
        """拉取这个 API 端点提供了哪些模型（走 GET /models）。"""
        try:
            client = self._get_client()
            ms = client.models.list()
            ids = []
            for m in getattr(ms, 'data', []) or []:
                mid = getattr(m, 'id', None) or (m.get('id') if isinstance(m, dict) else None)
                if mid:
                    ids.append(str(mid))
            ids.sort()
            return {'ok': True, 'models': ids, 'count': len(ids),
                    'base': (self.cfg.get('llmBaseUrl') or '').strip()}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:250],
                    'hint': '有些网关不提供 /models 接口；那就手动填模型名，下面的测速仍然能用。'}

    def speed_test(self, models=None, prompt='只回复一个词：ok', max_tokens=8, rounds=1):
        """逐个模型测首字延迟 + 总耗时。用来挑"又快又便宜"的那个。"""
        models = models or []
        out = []
        for mid in models:
            rec = {'model': mid}
            try:
                client = self._get_client()
                t0 = time.perf_counter()
                r = client.chat.completions.create(
                    model=mid,
                    messages=[{'role': 'user', 'content': prompt}],
                    max_tokens=max_tokens, temperature=0,
                    stream=True,
                )
                first = None
                text = ''
                for chunk in r:
                    try:
                        d = chunk.choices[0].delta.content
                    except Exception:
                        d = None
                    if d:
                        if first is None:
                            first = time.perf_counter() - t0
                        text += d
                total = time.perf_counter() - t0
                rec.update({'ok': True,
                            'firstMs': round((first or total) * 1000),
                            'totalMs': round(total * 1000),
                            'reply': text.strip()[:30]})
            except Exception as e:
                rec.update({'ok': False, 'msg': str(e)[:150]})
            out.append(rec)
            self._say(f"测速 {mid}: " + ("OK" if rec.get('ok') else rec.get('msg', '')[:40]))
        ok = [x for x in out if x.get('ok')]
        ok.sort(key=lambda x: x['totalMs'])
        return {'ok': True, 'results': out, 'fastest': ok[0]['model'] if ok else ''}

    def usage_info(self):
        u = dict(self.usage)
        u['total'] = u['prompt'] + u['completion']
        u['estCostNote'] = '按 token 数估算，具体单价看你的服务商'
        return u



def _repair_truncated_json(t):
    """尽力修复被截断的 JSON（finish_reason=length 时尾巴被砍）。
    思路：JSON 语法里字符串之外的逗号只会出现在完整值之后——在最后一个
    安全逗号处截断，按括号栈补闭合。丢掉残缺的最后一个键，保住前面完整的字段。
    返回解析成功的 dict，救不回来返回 None。"""
    start = t.find('{')
    if start < 0:
        return None
    body = t[start:]
    stack = []
    in_str = False
    esc = False
    safe = -1                    # 最后一个字符串外逗号的位置
    safe_stack = ''              # 该逗号处的括号栈快照
    for i, ch in enumerate(body):
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in '{[':
            stack.append(ch)
        elif ch in '}]':
            if not stack:
                return None      # 括号多过开头，结构没救
            stack.pop()
            if not stack:        # 顶层完整闭合（只是后面跟着垃圾尾巴）
                try:
                    obj = json.loads(body[:i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
        elif ch == ',':
            safe = i
            safe_stack = ''.join(stack)
    if safe < 0:
        return None
    cut = body[:safe].rstrip()
    if cut.endswith(','):
        cut = cut[:-1].rstrip()
    closers = ''.join('}' if c == '{' else ']' for c in reversed(safe_stack))
    try:
        obj = json.loads(cut + closers)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def parse_json_loose(txt):
    """从模型输出里尽力抠出 JSON（模型常会加 ```json 围栏或前后废话）。
    三层递进：直接解析 → 平衡大括号截取 → 截断修复（本地）→ json-repair 库兜底。"""
    if not txt:
        raise RuntimeError('模型返回为空')
    t = txt.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except Exception:
        pass
    # 退化：抓第一个平衡的 { ... }
    start = t.find('{')
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        break
    # 截断修复：max_tokens 不够时 JSON 缺尾巴——安全截断+补闭合
    obj = _repair_truncated_json(t)
    if isinstance(obj, dict):
        return obj
    # 最后兜底：json-repair 库（MIT，装了就用，没装跳过）
    try:
        from json_repair import repair_json
        obj = json.loads(repair_json(t))
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    raise RuntimeError('模型没返回可解析的 JSON：' + txt[:160])



# ---------------------------------------------------------------- 词典缓存
class DefineCache:
    """单词释义的永久缓存（词典）。

    同一个词只让 LLM 生成一次，之后所有地方（练习页点词、生词本、导出）
    都直接读缓存。这是省 token 最有效的一招 —— 常用词翻来覆去就那些。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = None
        self._lock = threading.Lock()

    def _load(self):
        if self._data is not None:
            return self._data
        try:
            self._data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self._data, dict):
                self._data = {}
        except Exception:
            self._data = {}
        return self._data

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            store.write_json(self.path, self._data)
        except Exception:
            pass

    @staticmethod
    def key(text):
        return re.sub(r'\s+', ' ', (text or '').strip().lower())

    def get(self, text, prompt_version=''):
        """取缓存。prompt_version 进 key：提示词升版后旧缓存自动失效。"""
        with self._lock:
            d = self._load()
            it = d.get(self.key(text))
            if not it:
                return None
            if prompt_version and it.get('_pv') != prompt_version:
                return None                      # 旧版本提示词生成的 → 视为未命中
            return it

    def size(self):
        with self._lock:
            return len(self._load())

    def stats(self):
        with self._lock:
            d = self._load()
            today = datetime.now().strftime('%Y-%m-%d')
            return {'total': len(d),
                    'today': sum(1 for v in d.values()
                                 if (v.get('_cachedAt') or '')[:10] == today)}

    def all(self):
        with self._lock:
            return dict(self._load())

    def clear(self):
        with self._lock:
            self._data = {}
            self._save()

    def put(self, text, detail, model='', prompt_version=''):
        with self._lock:
            d = self._load()
            if isinstance(detail, dict):
                detail['_cachedAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')
                detail['_model'] = model or ''
                if prompt_version:
                    detail['_pv'] = prompt_version
            d[self.key(text)] = detail
            self._save()

# ---------------------------------------------------------------- 生词本
class Vocab:
    """生词本 / 收藏。存成一个 JSON 文件，界面直接读。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self):
        if not self.path.exists():
            return []
        try:
            d = json.loads(self.path.read_text(encoding='utf-8'))
            return d if isinstance(d, list) else d.get('items', [])
        except Exception:
            return []

    def _save(self, items):
        store.write_json(self.path, items)

    def all(self):
        with self._lock:
            return self._load()

    def add(self, text, context='', detail=None, source=None, vtype='word', meaning='',
            count_review=True):
        """收藏一个词/短语。已存在则合并（count_review=True 时记一次 reviewCount——
        表示"真实又遇到了一次"；手动重复点收藏按钮应传 False，幂等不刷计数）。

        vtype: word=单词 / phrase=短语·搭配（记忆卡按短语卡渲染）
        meaning: 收藏时已知的中文含义（句法解析/片段分析带来），省一次 LLM
        """
        text = (text or '').strip()
        if not text:
            raise RuntimeError('内容为空')
        vtype = vtype or 'word'
        with self._lock:
            items = self._load()
            key = text.lower()
            for it in items:
                if (it.get('text') or '').lower() == key:
                    if count_review:
                        it['reviewCount'] = int(it.get('reviewCount') or 1) + 1
                    it['lastSeen'] = datetime.now().strftime('%Y-%m-%d %H:%M')
                    if context and not it.get('context'):
                        it['context'] = context
                    if context:
                        # 多出处例句：同一词在不同句子里反复遇到时都留下（最多 5 条）
                        ctxs = it.get('contexts') or []
                        if not any(c.get('text') == context[:200] for c in ctxs):
                            ctxs.append({'text': context[:200], 'itemId': (source or '')[:60],
                                         'at': datetime.now().strftime('%Y-%m-%d %H:%M')})
                            it['contexts'] = ctxs[-5:]
                    if detail:
                        it['detail'] = detail
                    if vtype != 'word' and not it.get('type'):
                        it['type'] = vtype
                    if meaning and not it.get('meaning'):
                        it['meaning'] = meaning
                    self._save(items)
                    return {'ok': True, 'existed': True, 'item': it}
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            item = {
                'id': f'{int(time.time() * 1000)}',
                'text': text,
                'context': context,
                'contexts': ([{'text': context[:200], 'itemId': (source or '')[:60],
                               'at': now}] if context else []),
                'detail': detail,
                'source': source or '',
                'type': vtype,
                'meaning': meaning or '',
                'addedAt': now,
                'lastSeen': now,
                'reviewCount': 1,
                'mastered': False,
                'tags': [],
            }
            items.append(item)
            self._save(items)
            return {'ok': True, 'existed': False, 'item': item}

    def remove(self, vid):
        with self._lock:
            items = self._load()
            n = len(items)
            items = [x for x in items if x.get('id') != vid]
            self._save(items)
            return {'ok': True, 'removed': n - len(items)}

    def update(self, vid, **fields):
        with self._lock:
            items = self._load()
            for it in items:
                if it.get('id') == vid:
                    it.update(fields)
                    self._save(items)
                    return {'ok': True, 'item': it}
            return {'ok': False, 'msg': '找不到该条目'}

    def stats(self):
        items = self.all()
        today = datetime.now().strftime('%Y-%m-%d')
        return {
            'total': len(items),
            'mastered': sum(1 for x in items if x.get('mastered')),
            'today': sum(1 for x in items if (x.get('addedAt') or '')[:10] == today),
        }


# ---------------------------------------------------------------- 高阶接口
class Coach:
    """把比对结果交给 LLM，得到可读的纠音建议。"""

    def __init__(self, llm, log=None, defcache=None, lookup_llm=None):
        self.llm = llm
        self.lookup_llm = lookup_llm
        self.log = log
        self._cache = {}
        # 持久化的单词词典缓存，见 DefineCache 的说明
        self.defcache = defcache or DefineCache(
            Path(llm.cfg.get('_appDir') or '.') / 'define_cache.json')

    def diagnose(self, reference, recognized, cmp_result):
        key = f'{reference}||{recognized}'
        if key in self._cache:
            return self._cache[key]

        table_lines = []
        for x in cmp_result['items']:
            t = x['type']
            if t == 'ok':
                continue
            mark = {'wrong': '读错', 'missed': '漏读', 'extra': '多读'}.get(t, t)
            r = x.get('ref') or '—'
            g = x.get('got') or '—'
            table_lines.append(f'  应为「{r}」/ 实为「{g}」 -> {mark}')
        table = '\n'.join(table_lines) or '  （逐词都能对上，没有明显错漏）'

        user = prompts.fmt('coach',
            reference=reference,
            recognized=recognized or '（没识别到内容）',
            table=table,
            accuracy=cmp_result.get('accuracy', 0),
        )
        sys_p, _u = prompts.get('coach')
        data = self.llm.chat_json(sys_p, user, max_tokens=1800, tag='发音纠音')
        self._cache[key] = data
        return data

    def define(self, text, context='', compact=None):
        """生成释义。

        省 token 的两道闸：
          1. **词典缓存**（持久化到 define_cache.json）—— 同一个词只生成一次。
             这是最有效的：常用词翻来覆去就那些，第二次开始零成本。
          2. **精简模板** —— 只保留学习真正要用的字段。
        compact=None 时读配置（默认精简）；要完整版就传 compact=False。
        """
        if compact is None:
            compact = (self.llm.cfg.get('defineCompact') is not False)

        # 1) 词典缓存。键只用词本身 —— 上下文只影响 ctx 一个字段，
        #    为一个字段重复付费不划算。
        pv = prompts.version_of('define_lite' if compact else 'define')
        hit = self.defcache.get(text, prompt_version=pv)
        if hit:
            return dict(hit, _fromCache=True)

        use_lookup = self.lookup_llm is not None and getattr(self.lookup_llm, 'ready', False)
        target = self.lookup_llm if use_lookup else self.llm
        try:
            key = 'define_lite' if compact else 'define'
            sys_p, _u = prompts.get(key)
            user = prompts.fmt(key, text=text, context=context or '（无）')
            data = target.chat_json(
                sys_p, user,
                max_tokens=(2000 if compact else 2200),   # 1200 不够复杂多义词（截断→JSON 解析失败的实锤）
                tag='查词释义' + ('(专用API)' if use_lookup else ''))
        except Exception as e:
            if use_lookup:
                # 用户拍板：独立查词 API 失败时提示手动重试，不回落主模型
                raise RuntimeError('查词专用 API 失败：' + str(e)[:150] + '（可在设置里检查专用 API，或暂时关闭专用开关改用主模型重试）')
            raise

        if isinstance(data, dict):
            # 精简版字段名短，这里统一成长名字段，界面不用改
            alias = {'cn': 'meaning_cn', 'en': 'meaning_en', 'ctx': 'context_meaning',
                     'col': 'collocations', 'ex': 'examples', 'note': 'notes',
                     'mem': 'memory'}
            for short, long in alias.items():
                if short in data and long not in data:
                    data[long] = data.pop(short)
            if not data.get('text'):
                data['text'] = text
            self.defcache.put(text, data, model=(self.llm.cfg.get('llmModel') or ''), prompt_version=pv)

        self._cache[f'{text}||{context}'] = data
        return data

    def analyze_fragment(self, text, context=''):
        """片段分析：与 define 同一套专用 API 策略 —— 启用则走查词 API，失败不回落。"""
        user = ('分析这个英语片段：「' + text + '」'
                + (('（出自句子：' + str(context)[:200] + '）') if context else '')
                + '\n输出：{"kind_zh":"判定：介词短语|动词短语|习语|固定搭配|从句|完整句|残缺片段",'
                + '"meaning_zh":"含义，中文",'
                + '"scenes":[{"scene_zh":"什么情景/话题用它","example_en":"英文例句","example_zh":"例句翻译"}],'
                + '"similar":["近义表达，最多3个"],'
                + '"memo":"记忆要点，中文一句话"}')
        use_lookup = self.lookup_llm is not None and getattr(self.lookup_llm, 'ready', False)
        target = self.lookup_llm if use_lookup else self.llm
        try:
            return target.chat_json(
                '你是英语语言学者，服务中文母语者，只输出 JSON。', user,
                max_tokens=1200, tag='划选分析' + ('(专用API)' if use_lookup else ''))
        except Exception as e:
            if use_lookup:
                # 用户拍板：专用 API 失败提示手动处理，不静默回落主模型
                raise RuntimeError('片段分析专用 API 失败：' + str(e)[:150]
                                   + '（可在设置里检查专用 API，或暂时关闭专用开关改用主模型重试）')
            raise

    def summarize(self, records, words, difficulty_note=''):
        rec = json.dumps(records[-60:], ensure_ascii=False, indent=1) if records else '[]'
        wd = json.dumps([w.get('text') for w in (words or [])][-80:], ensure_ascii=False)
        sys_p, _u = prompts.get('summary')
        user = prompts.fmt('summary', records=rec, words=wd,
                           difficulty=difficulty_note or '（无）')
        return self.llm.chat_json(sys_p, user, max_tokens=2200, tag='练习总结')
