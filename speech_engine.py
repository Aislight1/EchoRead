# -*- coding: utf-8 -*-
"""统一语音合成引擎层

解决的问题：之前"用哪个引擎"散落在好几个地方 ——
MiMo 在 speech_tts、Edge 又是另一条路，界面上的音色下拉把两种东西
混在一起，用户根本不知道选了会发生什么。

现在统一成：**先选引擎，再选该引擎下的音色**。

引擎清单：
    mimo  小米 MiMo-V2.5-TTS（在线 API，靠文字描述控制音色，24+ 内置音色）
    edge  Edge TTS（微软在线免费，无需 key，音质一般）

    （local / GPT-SoVITS 本地部署已按终稿 P0 移除：占 2.2GB 内存、
     冷启动慢、中文模型念英文口音差；配置残留由 current_engine 自动
     迁移到 mimo。）

配置项（config.json）：
    ttsEngine        当前引擎："mimo" | "edge"
    ttsEngineOverride  每个页面/场景的覆写，如 {"listen": "edge"}
    testSentences   {en: "...", zh: "..."} 固定的试听句

"""

import subprocess  # noqa: E402
from pathlib import Path

DEFAULT_ENGINE = "mimo"

# 静态底座（描述最好的两家）；其余供应商从 speech_providers 注册表动态并入，
# 这样「语音输出」引擎网格 = 供应商中心全量（不再只有 mimo/edge 两项）
_ENGINES_STATIC = {
    "mimo": {
        "id": "mimo", "name": "小米 MiMo（在线 API）",
        "desc": "靠文字描述控制音色，24+ 内置音色，首次约 1-3 秒，几乎不占内存",
        "needsKey": "mimoApiKey", "offline": False,
        "pros": "音色多、英文口音标准、不占内存",
        "cons": "要联网、消耗 API（目前免费）",
    },
    # "local"（GPT-SoVITS 本地部署）已按终稿 P0 移除：占 2.2GB 内存、冷启动慢、
    # 中文模型念英文口音差；配置残留由 current_engine 自动迁移到 mimo
    "edge": {
        "id": "edge", "name": "Edge TTS（微软，免费）",
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
                "cons": "需配置密钥" if p.get('auth') == 'key' else "免费",
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

    local（GPT-SoVITS）已移除：配置残留自动迁移 mimo；
    未知/下线引擎一律落回默认，绝不让 None 冒出去。
    """
    cfg = cfg or {}
    ov = (cfg.get("ttsEngineOverride") or {})
    e = str(ov.get(scene)) if (scene and ov.get(scene)) else str(cfg.get("ttsEngine") or DEFAULT_ENGINE)
    if e == "local" or e not in ENGINES:            # 旧配置残留/未知 → 落回默认
        e = DEFAULT_ENGINE
    return e


class SpeechEngine:
    """统一入口。内部按 engine 分派到 MiMo / Edge。"""

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
        return ENGINES.get(eid, ENGINES[DEFAULT_ENGINE])

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
            if not (self.cfg.get("mimoApiKey") or "").strip():
                return "还没填 MiMo API Key"
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
        # mimo / edge 都用 speech_tts 的预设
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
        """合成一段文本 -> 本地 wav 路径（mimo / edge 双引擎）。"""
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

        孤立短英文用 MiMo 效果差（短词崩），Edge en-US 最自然且免费；
        失败退回 MiMo 慢速教学音。走 TTS 层磁盘缓存。
        """
        text = (text or "").strip()
        if not text:
            raise RuntimeError("文本为空")
        if not self._tts:
            raise RuntimeError("TTS 模块没初始化")
        try:
            return self._tts.synthesize(text, 'narrator', provider='edge',
                                        voice_override='en-US-AriaNeural')
        except Exception as e:
            self._say(f'Edge 单词朗读失败，退回 MiMo 慢速教学音：{e}')
            return self._tts.synthesize(text, 'slow_teacher', provider='mimo')

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
        key = f"{eid}|{vid}|{lang}|{speed}|{text}"
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
