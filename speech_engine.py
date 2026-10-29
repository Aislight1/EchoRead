# -*- coding: utf-8 -*-
"""统一语音合成引擎层

解决的问题：之前"用哪个引擎"散落在好几个地方 ——
MiMo 在 speech_tts、Edge 又是另一条路，界面上的音色下拉把两种东西
混在一起，用户根本不知道选了会发生什么。

现在统一成：**先选引擎，再选该引擎下的音色**。

引擎清单：
    dashscope 阿里云 Qwen TTS（默认；qwen3-tts-flash 稳定款）
    edge      Edge TTS（微软在线，无需 Key，兜底）
    mimo      小米 MiMo TTS（V2.5 全系 2026-10-21 下线；v2.6-tts 发布后可手填模型名）

    （local / GPT-SoVITS 本地部署已按终稿 P0 移除：占 2.2GB 内存、
     冷启动慢、中文模型念英文口音差；配置残留由 current_engine 自动
     迁移到当前可用引擎。）

配置项（config.json）：
    ttsEngine        当前引擎："dashscope" | "edge" | "mimo" | ...
    ttsEngineOverride  每个页面/场景的覆写，如 {"listen": "edge"}
    testSentences   {en: "...", zh: "..."} 固定的试听句

"""

import subprocess  # noqa: E402
from pathlib import Path

DEFAULT_ENGINE = "dashscope"


def _retired_mimo_models():
    """MiMo 已下线模型集合（单一数据源在 speech_providers，读不到就按空集处理）。"""
    try:
        import speech_providers as _spv
        return {str(x).lower() for x in getattr(_spv, 'MIMO_RETIRED_MODELS', set())}
    except Exception:
        return set()


def preferred_engine(cfg):
    """没有显式可用配置时该落到哪个引擎：有百炼 Key → dashscope；否则 → edge。
    （MiMo V2.5 已于 2026-10-21 下线，不能再当兜底——旧代码处处回退 mimo 会静默失败。）"""
    cfg = cfg or {}
    if (cfg.get('dashscopeApiKey') or '').strip():
        return 'dashscope'
    # 供应商中心里可能只存了 speechProviders.dashscope.apiKey
    uc = (cfg.get('speechProviders') or {}).get('dashscope') or {}
    if (uc.get('apiKey') or '').strip():
        return 'dashscope'
    return 'edge'


def _mimo_usable(cfg):
    """MiMo 现在还能不能用：要有 Key，且模型必须显式配好、不在下线名单里。"""
    cfg = cfg or {}
    key = (cfg.get('mimoApiKey') or '').strip()
    if not key:
        uc = (cfg.get('speechProviders') or {}).get('mimo') or {}
        key = (uc.get('apiKey') or '').strip()
    if not key:
        return False
    model = ((cfg.get('ttsEngineModels') or {}).get('mimo')
             or cfg.get('mimoTtsModel') or '').strip()
    if not model:
        return False                      # v2.5 默认值已移除，必须手填新模型
    return model.lower() not in _retired_mimo_models()


# 静态底座（描述最好的两家）；其余供应商从 speech_providers 注册表动态并入，
# 这样「语音输出」引擎网格 = 供应商中心全量（不再只有 mimo/edge 两项）
_ENGINES_STATIC = {
    "mimo": {
        "id": "mimo", "name": "小米 MiMo（在线 API）",
        "desc": "V2.5 全系已于 2026-10-21 下线；若已发布 v2.6-tts，可手填模型名继续使用",
        "needsKey": "mimoApiKey", "offline": False,
        "pros": "英文口音标准、不占内存",
        "cons": "V2.5 已下线，需手填新模型名（如 mimo-v2.6-tts）",
    },
    # "local"（GPT-SoVITS 本地部署）已按终稿 P0 移除：占 2.2GB 内存、冷启动慢、
    # 中文模型念英文口音差；配置残留由 current_engine 自动迁移到 mimo
    "edge": {
        "id": "edge", "name": "Edge TTS（微软在线）",
        "desc": "不需要 key 的在线合成，音质一般",
        "needsKey": None, "offline": False,
        "pros": "不用 key、不花钱",
        "cons": "音色平淡、要联网",
    },
}


def _dynamic_engines():
    """静态底座 + speech_providers['tts'] 里的其余供应商（dashscope/volc/siliconflow/custom）。"""
    engs = dict(_ENGINES_STATIC)
    try:
        import speech_providers as _spv
        for pid, p in (_spv.PROVIDERS.get('tts') or {}).items():
            if pid in engs:
                continue
            engs[pid] = {
                "id": pid, "name": p.get('label', pid),
                "desc": '；'.join(x for x in [p.get('note', ''),
                                              next((b.get('label') for b in (p.get('builtin') or [])
                                                    if b.get('label')), '')] if x) or 'OpenAI 兼容合成',
                "needsKey": p.get('keyName') or None, "offline": False,
                "pros": next((b.get('label') for b in (p.get('builtin') or [])), '多供应商可选'),
                "cons": "需配置密钥" if p.get('auth') == 'key' else "无需密钥",
            }
    except Exception:
        pass
    return engs


ENGINES = _dynamic_engines()

# 固定试听句 —— 每个音色只需要合成一次，之后一直用缓存。
# 选这两句是因为它们覆盖了：常见音素、连读、不同句调。
DEFAULT_TEST = {
    "en": ("Hello, this is a voice test. The quick brown fox jumps over the lazy dog. "
           "I practice my English pronunciation every day."),
    "zh": "你好，这是一段中文语音测试。我每天都会花十五分钟练习英语口语。",
}


def get_test_sentences(cfg):
    t = (cfg or {}).get("testSentences") or {}
    return {"en": (t.get("en") or "").strip() or DEFAULT_TEST["en"],
            "zh": (t.get("zh") or "").strip() or DEFAULT_TEST["zh"]}


def current_engine(cfg, scene=None):
    """当前该用哪个引擎。scene 可以覆写（比如听力页固定用某个）。

    local（GPT-SoVITS）已移除；MiMo V2.5 已于 2026-10-21 下线。
    未知/下线的引擎一律落回 preferred_engine，绝不让 None 或死模型冒出去。
    """
    cfg = cfg or {}
    ov = (cfg.get("ttsEngineOverride") or {})
    e = str(ov.get(scene)) if (scene and ov.get(scene)) else str(cfg.get("ttsEngine") or "")
    if not e:
        # 没显式选过引擎：按「有没有配 Key」自动落位——有百炼 Key 用 dashscope，
        # 否则用不需要 Key 的 Edge（MiMo V2.5 下线后不能再当默认）。
        e = preferred_engine(cfg)
    elif e == "local" or e not in ENGINES:          # 旧配置残留/未知 → 落回可用引擎
        e = preferred_engine(cfg)
    if e == "mimo" and not _mimo_usable(cfg):       # V2.5 已下线 / 没填新模型 → 不再硬用它
        e = preferred_engine(cfg)
    return e


class SpeechEngine:
    """统一入口。内部按 engine 分派到阿里云 / Edge / MiMo 等供应商。"""

    def __init__(self, cfg=None, cache_dir=None, log=None,
                 tts=None, extra_presets=None):
        self.cfg = cfg or {}
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self._tts = tts                    # speech_tts.TTS
        self.extra_presets = extra_presets or []

    def _say(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    # ---------- 引擎信息
    def engine(self, scene=None):
        return current_engine(self.cfg, scene)

    def engine_info(self, eid=None):
        eid = eid or self.engine()
        return ENGINES.get(eid) or ENGINES.get(preferred_engine(self.cfg)) \
            or ENGINES.get(DEFAULT_ENGINE) or next(iter(ENGINES.values()))

    def all_engines(self):
        """给界面用：每个引擎 + 当前可用性。"""
        out = []
        for k, v in ENGINES.items():
            item = dict(v)
            item["available"] = self.available(k)
            item["reason"] = self.unavailable_reason(k)
            out.append(item)
        return out

    def available(self, eid=None):
        return self.unavailable_reason(eid) is None

    def unavailable_reason(self, eid):
        """为什么这个引擎现在不能用。None 表示可用。
        动态供应商按注册表 keyName 查 key；没查到 key 的在真正合成时才报错（可能走自定义配置）。"""
        if eid == "mimo":
            if not _mimo_usable(self.cfg):
                try:
                    import speech_providers as _spv
                    return getattr(_spv, 'MIMO_RETIRED_NOTE', 'MiMo V2.5 已下线，请换用阿里云 Qwen TTS 或 Edge')
                except Exception:
                    return 'MiMo V2.5 已下线，请换用阿里云 Qwen TTS 或 Edge'
            return None
        if eid == "edge":
            try:
                import edge_tts  # noqa
            except ImportError:
                return "没装 edge-tts（pip install edge-tts）"
            return None
        if eid in ENGINES:
            kn = (ENGINES.get(eid) or {}).get("needsKey")
            if not kn:
                return None                     # custom：可能在 speechProviders 里存了 key
            if not (self.cfg.get(kn) or '').strip():
                # speechProviders 旧存法兜底
                uc = (self.cfg.get("speechProviders") or {}).get(eid) or {}
                if (uc.get("apiKey") or '').strip():
                    return None
                return f"还没填 {ENGINES[eid]['name']} 的 API Key（key 名：{kn}）"
            return None
        return f"未知引擎 {eid}"

    # ---------- 统一音色列表
    def voices(self, scene=None):
        """返回当前引擎下的音色列表。界面只显示这一份。"""
        eid = self.engine(scene)
        # 阿里云/Edge 返回注册表里的真实音色；MiMo 返回风格预设
        if not self._tts:
            return []
        out = []
        for p in self._tts.list_presets():
            if eid == "edge":
                p = dict(p)
                p["group"] = p.get("group") or "Edge"
            out.append(p)
        return out

    # ---------- 语言与安全长度
    @staticmethod
    def detect_lang(text):
        """判断文本主要语言，返回 en / zh / ja / other。

        用 fast_langdetect。注意它返回的是 dict 不是 list：{'lang': 'en', 'score': 0.9}
        """
        t = (text or "").strip()
        if not t:
            return "other"
        han = sum(1 for ch in t if '\u4e00' <= ch <= '\u9fff')
        letters = sum(1 for ch in t if ch.isalpha())
        if han and han >= max(2, letters * 0.2):
            # 汉字占比够高就直接当中文，比探测器可靠
            return "zh"
        try:
            from fast_langdetect import detect
            d = detect(t)
            if isinstance(d, dict):
                lang = d.get("lang")
            elif isinstance(d, list) and d:
                lang = d[0].get("lang")
            else:
                lang = None
            if lang in ("en", "zh", "ja"):
                return lang
            return "other"
        except Exception:
            asc = sum(1 for ch in t if ord(ch) < 128)
            return "en" if asc / max(1, len(t)) > 0.8 else "other"

    # ---------- 统一合成
    def synthesize(self, text, voice_id=None, scene=None, speed=1.0, force=False,
                   allow_risky=False):
        """合成一段文本 -> 本地 wav 路径（按当前供应商路由）。"""
        text = (text or "").strip()
        if not text:
            raise RuntimeError("文本为空")
        eid = self.engine(scene)
        reason = self.unavailable_reason(eid)
        if reason:
            raise RuntimeError(f"{self.engine_info(eid)['name']} 现在用不了：{reason}")
        return self._synth_tts(text, voice_id, eid, force)

    def synthesize_word(self, text):
        """单词/短语朗读：强制 Edge 神经音色，绕过场景引擎覆盖。

        孤立短英文用 MiMo 效果差（短词崩），Edge en-US 最自然；
        Edge 失败时退回「当前可用引擎」（阿里云优先）。走 TTS 层磁盘缓存。

        返回 (path, via)：via 是真实生效的引擎——曾经调用方恒报 'edge'，
        Edge 挂了换引擎时前端仍显示 edge（谎报，与 ⑥ 模型假显示同类）。2026-10-16 修复。
        MiMo V2.5 下线后，这里的兜底不再写死 MiMo（否则 Edge 一挂就静默失败）。
        """
        text = (text or "").strip()
        if not text:
            raise RuntimeError("文本为空")
        if not self._tts:
            raise RuntimeError("TTS 模块没初始化")
        try:
            p = self._tts.synthesize(text, 'narrator', provider='edge',
                                     voice_override='en-US-AriaNeural')
            return p, 'edge'
        except Exception as e:
            fb = preferred_engine(self.cfg)
            if fb == 'edge' or self.unavailable_reason(fb):
                fb = 'dashscope' if not self.unavailable_reason('dashscope') else ''
            if not fb:
                raise
            self._say(f'Edge 单词朗读失败，改用 {fb} 合成：{e}')
            mo = (self.cfg.get('ttsEngineModels') or {}).get(fb) or None
            p = self._tts.synthesize(text, 'narrator', provider=fb,
                                     model_override=mo)
            return p, fb

    def _synth_tts(self, text, voice_id, eid, force):
        if not self._tts:
            raise RuntimeError("TTS 模块没初始化")
        # 供应商直通：speech_tts.synthesize 自己按 provider 路由
        # （edge→本地神经音 / dashscope→阿里云 / mimo+volc+siliconflow+custom→OpenAI 兼容口）。
        # 曾经"非 edge 一律按 mimo"——选了新供应商音色却还是 MiMo，音色错乱的真凶。
        provider = eid
        vid = voice_id or self.cfg.get("ttsPreset") or "narrator"
        mo = (self.cfg.get("ttsEngineModels") or {}).get(eid) or None
        return self._tts.synthesize(text, vid, provider=provider, force=force,
                                    model_override=mo or None)

    # ---------- 固定试听句（一次合成，永久缓存）
    def preview(self, voice_id=None, lang="en", scene=None, speed=1.0, allow_risky=False):
        """用**固定的**试听句合成，结果按（引擎+音色+语速）缓存，不再重复调 API。"""
        eid = self.engine(scene)
        sents = get_test_sentences(self.cfg)
        text = sents.get(lang) or sents["en"]

        vid = voice_id or ""
        # 缓存键必须带「实际生效的模型」：只按引擎+音色缓存，切模型/换 Key 后还会命中旧音频
        # （用户报「音节设置怎么改都是老声音」的隐藏原因之一）
        try:
            import speech_providers as _spv
            _m = (self.cfg.get("ttsEngineModels") or {}).get(eid) or _spv.tts_default_model(eid)
        except Exception:
            _m = ""
        key = f"{eid}|{_m}|{vid}|{lang}|{speed}|{text}"
        import hashlib
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        cached = (self.cache_dir or Path(".")) / f"preview_{h}.wav"

        if cached.exists() and cached.stat().st_size > 2000:
            self._say(f"试听用缓存：{cached.name}")
            return {"ok": True, "url": None, "file": str(cached), "text": text,
                    "lang": lang, "engine": eid, "cached": True}

        p = self.synthesize(text, vid, scene=scene, speed=speed, force=True,
                            allow_risky=allow_risky)
        # 把结果另存成稳定的预览文件，这样下次一定命中
        try:
            Path(cached).write_bytes(Path(p).read_bytes())
        except Exception:
            cached = Path(p)
        return {"ok": True, "url": None, "file": str(cached), "text": text,
                "lang": lang, "engine": eid, "cached": False}

    # ---------- 语音输出管理面板要的数据
    def overview(self, scene=None):
        eid = self.engine(scene)
        return {
            "engine": eid,
            "engineInfo": self.engine_info(eid),
            "engines": self.all_engines(),
            "overrides": self.cfg.get("ttsEngineOverride") or {},
            "voices": self.voices(scene),
            "testSentences": get_test_sentences(self.cfg),
        }
