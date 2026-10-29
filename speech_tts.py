#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音合成（TTS）——多供应商路由层。

主用小米 MiMo-V2.5-TTS（OpenAI 兼容协议）；另留 Edge TTS 作后备（无需 Key）；
阿里云 Qwen TTS（qwen3 / CosyVoice / MiniMax 三系，端点各不同）由 speech_providers 注册表驱动。

#9「音色工作台」（声音设计 voicedesign / 声音复刻 voiceclone / 我的音色）已整块下线：
  · 合成按「供应商 → 该供应商最新一代模型 → 该模型系的音色」三级解析，目录只有一份
    （speech_providers.PROVIDERS['tts']），TTS 层不再自带第二份音色表；
  · 老配置里的 voicedesign / voiceclone 模型名 → aliases 自动映射回普通合成模型；
  · design_voice / clone_voice 只保留抛「功能已移除」的 stub，给 app.py 侧同步删除的过渡期用。
"""
import base64
import hashlib
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


def _no_window():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _normalize_wav(data):
    """把流式 WAV 的占位长度改回真实大小。

    DashScope SSE 会把音频分块下发，第一块的 RIFF/data 长度常是 0x7FFFFFFF 占位，
    拼完后没人回填 —— 播放器能容忍，但 Python wave 会把 0.3 秒读成 12 小时，
    时长统计/切片全跟着错（真机合成验证时抓到）。只改长度字段，不动音频数据。
    """
    try:
        if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
            return data
        pos = 12
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            csz = int.from_bytes(data[pos + 4:pos + 8], 'little')
            body = pos + 8
            if cid == b'data':
                real = len(data) - body
                if csz != real:
                    data = data[:pos + 4] + real.to_bytes(4, 'little') + data[pos + 8:]
                break
            if csz == 0x7FFFFFFF or csz > len(data) - body:
                break
            pos = body + csz + (csz & 1)
        real_riff = len(data) - 8
        if int.from_bytes(data[4:8], 'little') != real_riff:
            data = data[:4] + real_riff.to_bytes(4, 'little') + data[8:]
        return data
    except Exception:
        return data


# ---------------------------------------------------------------- 内置音色
# 每个预设 = 一段「风格指令」，MiMo 用它控制语气/音色/语速。
VOICE_PRESETS = [
    # ---------- 跟读主力 ----------
    {'id': 'narrator', 'name': '中性旁白（美音）', 'group': '跟读主力',
     'desc': '标准美音，语速适中，无情绪起伏 —— 最适合跟读',
     'voice': 'Chloe',
     'instruct': ('Neutral, clear narration. Standard American accent, moderate pace, '
                  'no strong emotion, even intonation. Suitable for shadowing practice.')},
    {'id': 'narrator_uk', 'name': '英音旁白', 'group': '跟读主力',
     'desc': '标准英式发音（RP），适合想练英音',
     'voice': 'Mia',
     'instruct': ('Neutral narration with a standard British (RP) accent, moderate pace, '
                  'clear articulation, no strong emotion.')},
    {'id': 'slow_teacher', 'name': '慢速教学', 'group': '跟读主力',
     'desc': '刻意放慢，每个音都清楚 —— 听不清时用',
     'voice': 'Chloe',
     'instruct': ('Speak slowly and very clearly, as a patient language teacher dictating '
                  'to a beginner. Exaggerate each sound slightly. Slow pace.')},
    {'id': 'slow_uk', 'name': '慢速英音教学', 'group': '跟读主力',
     'desc': '慢速 + 英音，练英音跟读用',
     'voice': 'Mia',
     'instruct': ('Slow, very clear British RP pronunciation for a language learner. '
                  'Distinct consonants, careful vowels, unhurried pace.')},
    {'id': 'news_anchor', 'name': '新闻主播', 'group': '跟读主力',
     'desc': '新闻播报腔，字正腔圆、节奏稳',
     'voice': 'Dean',
     'instruct': ('Professional news anchor delivery. Crisp, authoritative, precise diction, '
                  'steady measured pace, minimal emotion.')},
    {'id': 'podcast', 'name': '播客独白', 'group': '跟读主力',
     'desc': '自然口语，像 TED 演讲 —— 最贴近真实材料',
     'voice': 'Milo',
     'instruct': ('Natural conversational tone, like a TED talk speaker addressing an '
                  'audience. Relaxed but articulate, natural pauses.')},

    # ---------- 女声 ----------
    {'id': 'bright_female', 'name': '清脆女声', 'group': '女声',
     'desc': '明亮、轻快、上扬，有活力', 'voice': '冰糖',
     'instruct': ('Bright, crisp, youthful female voice. Lively and slightly upbeat, '
                  'clear articulation, natural rising intonation.')},
    {'id': 'gentle_female', 'name': '温柔女声', 'group': '女声',
     'desc': '柔和、慢节奏、气声多，适合放松听', 'voice': '茉莉',
     'instruct': ('Gentle, soft female voice with a warm breathy quality, calm and soothing, '
                  'slightly slower pace.')},
    {'id': 'mature_female', 'name': '知性女声', 'group': '女声',
     'desc': '成熟稳重，适合讲解类内容', 'voice': 'Mia',
     'instruct': ('Mature, intellectual female voice. Composed and confident, clear diction, '
                  'measured pace, like a university lecturer.')},
    {'id': 'anime', 'name': '二次元 / 元气', 'group': '女声',
     'desc': '动漫角色感，情绪外放、语气夸张', 'voice': '冰糖',
     'instruct': ('Anime-style energetic character voice. Expressive, exaggerated emotion, '
                  'bouncy rhythm, higher pitch, playful.')},
    {'id': 'cheerful_girl', 'name': '俏皮少女', 'group': '女声',
     'desc': '年轻活泼带点调皮，中高音、语速偏快', 'voice': '冰糖',
     'instruct': ('A cheerful young female voice with a slightly mischievous, playful tone. '
                  'Medium-high pitch, light and quick, giggly warmth.')},

    # ---------- 男声 ----------
    {'id': 'warm_male', 'name': '温厚男声', 'group': '男声',
     'desc': '低沉温和，像播客主持', 'voice': '苏打',
     'instruct': ('Warm, deep, calm male voice, like a podcast host. Relaxed pace, '
                  'friendly and steady.')},
    {'id': 'deep_male', 'name': '低沉磁性男声', 'group': '男声',
     'desc': '低音炮，纪录片旁白感', 'voice': 'Dean',
     'instruct': ('Deep, resonant male voice with rich bass timbre. Documentary narrator '
                  'style, deliberate and weighty.')},
    {'id': 'young_male', 'name': '年轻男声', 'group': '男声',
     'desc': '轻松随意的年轻人口吻', 'voice': 'Milo',
     'instruct': ('Young male voice, casual and relaxed, like talking with a friend. '
                  'Natural pace, light tone.')},
    {'id': 'storyteller', 'name': '说书人', 'group': '男声',
     'desc': '讲故事的口吻，抑扬顿挫', 'voice': '白桦',
     'instruct': ('Engaging storyteller voice with dramatic rises and falls, '
                  'drawing the listener in. Moderate pace, rich intonation.')},
    {'id': 'professor', 'name': '教授讲解', 'group': '男声',
     'desc': '学术讲解腔，慢而清楚，逻辑停顿明显', 'voice': '白桦',
     'instruct': ('Elderly professor explaining a concept. Slow, deliberate, clear, '
                  'with meaningful pauses between ideas.')},

    # ---------- 特殊场景 ----------
    {'id': 'whisper', 'name': '耳语', 'group': '特殊场景',
     'desc': '轻声细语 —— 练细节音时很有用', 'voice': '茉莉',
     'instruct': ('Soft whisper, very close and intimate, quiet but clearly articulated. '
                  'Slow and careful.')},
    {'id': 'excited', 'name': '兴奋激动', 'group': '特殊场景',
     'desc': '高能量、语速快、音调起伏大', 'voice': '冰糖',
     'instruct': ('Excited and high-energy. Fast pace, wide pitch range, '
                  'genuine enthusiasm and surprise.')},
    {'id': 'sad', 'name': '低落沉重', 'group': '特殊场景',
     'desc': '情绪低沉，句尾下坠', 'voice': '茉莉',
     'instruct': ('Sad, low-energy voice. Slow pace, falling intonation at sentence ends, '
                  'subdued and heavy.')},
    {'id': 'business', 'name': '商务正式', 'group': '特殊场景',
     'desc': '正式克制，适合商务英语', 'voice': 'Dean',
     'instruct': ('Formal business presentation voice. Professional, restrained, '
                  'clear and confident, moderate pace.')},
    {'id': 'child', 'name': '儿童声音', 'group': '特殊场景',
     'desc': '小朋友的声音，适合儿童内容', 'voice': '茉莉',
     'instruct': ('A child voice, around 7 years old. High pitch, innocent and curious, '
                  'slightly imperfect articulation, lively.')},

    # ---------- 中文 ----------
    {'id': 'cn_female', 'name': '中文女声', 'group': '中文',
     'desc': '标准普通话女声，清晰自然',
     'voice': '茉莉',
     'instruct': '用标准普通话朗读，语气自然、清晰，语速适中。'},
    {'id': 'cn_male', 'name': '中文男声', 'group': '中文',
     'desc': '标准普通话男声，稳重',
     'voice': '苏打',
     'instruct': '用标准普通话朗读，男声，稳重清晰，语速稍慢。'},
    {'id': 'cn_news', 'name': '中文播报', 'group': '中文',
     'desc': '新闻播报腔，字正腔圆',
     'voice': 'Dean',
     'instruct': '用标准的新闻播报腔朗读，字正腔圆，节奏稳定，不带情绪。'},
]


# MiMo 风格预设自带的音色名 → 阿里云 qwen3 系的等价音色（同一角色/性别，避免跨系 411）
_MIMO_VOICE_MAP = {'Chloe': 'Cherry', 'Mia': 'Serena', 'Dean': 'Ethan', 'Milo': 'Ethan'}
# 同一批风格名 → Edge 神经音（Edge 目录里没有 Chloe/Mia，直接发会 Invalid voice）
_MIMO_EDGE_MAP = {'Chloe': 'en-US-AriaNeural', 'Mia': 'en-GB-SoniaNeural',
                  'Dean': 'en-US-GuyNeural', 'Milo': 'en-US-GuyNeural',
                  '冰糖': 'zh-CN-XiaoxiaoNeural', '茉莉': 'zh-CN-XiaoxiaoNeural',
                  '苏打': 'zh-CN-YunxiNeural', '白桦': 'zh-CN-YunxiNeural'}


def preset_by_id(pid):
    for p in VOICE_PRESETS:
        if p['id'] == pid:
            return p
    # 自定义音色（存在 voices.json，由 app 层注入）
    return VOICE_PRESETS[0]


# 测试朗读样例（中英各一段）
TEST_SAMPLES = {
    'en': ('Hello. This is a voice test. The quick brown fox jumps over the lazy dog. '
           'I would like to practice my English pronunciation every single day.'),
    'zh': ('你好，这是一段中文语音测试。请听清楚每一个字的发音和语调。'
           '我每天都会花十五分钟练习英语口语。'),
}


class TTS:
    def __init__(self, cfg=None, cache_dir=None, log=None, extra_presets=None):
        self.cfg = cfg or {}
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self._client = None
        # extra_presets = app.py 的 voices.json。音色工作台下线后，只剩「练习页自定义风格」
        # 这一类（custom_ 开头、没有 kind）还有效；design_/clone_ 两类是已移除的能力，
        # 直接过滤掉——老配置文件里躺着也不会在界面出现，更不会去调 moot 的接口。
        legacy = [p for p in (extra_presets or [])
                  if isinstance(p, dict) and p.get('id')
                  and (p.get('kind') not in ('design', 'clone'))]
        self._legacy_presets = len(extra_presets or []) - len(legacy)   # 被丢掉的旧音色数（仅日志用）
        self.extra_presets = legacy

    def _say(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    # ---------- 音色查找：内置风格预设 → 供应商音色目录 → 兜底
    def _preset(self, pid, provider=None, model=None):
        """id → 预设 dict（永远返回副本，绝不改 VOICE_PRESETS 里的共享项）。

        带 _cat=True 表示「用户直接在音色下拉里点了这个真实音色」：合成时它的 voice
        优先于全局 dashscopeVoice（否则选了音色也被全局值顶回去，试听全是同一个声音）。
        """
        for p in VOICE_PRESETS:
            if p['id'] == pid:
                return dict(p, _cat=False)
        provider = (provider or self._provider()).lower()
        try:
            import speech_providers as _spv
            for v in _spv.tts_voices(provider, model, with_default=False):
                if v.get('id') and v['id'] == pid:
                    return {'id': pid, 'name': v.get('label') or pid,
                            'desc': v.get('desc') or v.get('group') or '',
                            'group': v.get('group') or '当前引擎音色',
                            'voice': pid, 'instruct': '', '_cat': True}
        except Exception:
            pass
        for p in self.extra_presets:
            if p.get('id') == pid:
                return dict(p, _cat=False)
        return dict(VOICE_PRESETS[0], _cat=False)

    # ---------- 客户端
    def _mimo_client(self):
        """TTS 客户端（多供应商）：按 ttsProvider 解析端点，全部走 OpenAI 兼容协议。

        供应商注册表见 speech_providers.py；speechProviders[pid] 用户覆盖优先。
        mimo 保持旧行为（mimoApiKey/mimoBaseUrl）；其它供应商读 speechProviders 配置。
        ⚠ 缓存键=(供应商,base,key)：换引擎后必须重建客户端——曾经单例缓存，
        切了供应商仍打旧端点旧模型（"串到别的模型"的真凶）。"""
        import speech_providers as _spv
        pid = (self.cfg.get('ttsProvider') or 'dashscope').strip()
        try:
            ep = _spv.resolve_tts_endpoint(self.cfg, pid)
        except Exception:
            ep = {'provider': pid, 'baseUrl': self.cfg.get('dashscopeBaseUrl'),
                  'apiKey': self.cfg.get('dashscopeApiKey'),
                  'model': (self.cfg.get('ttsEngineModels') or {}).get(pid)}
        _ck = (ep.get('provider'), ep.get('baseUrl'), ep.get('apiKey'))
        if self._client is not None and getattr(self, '_client_key', None) == _ck:
            return self._client
        key = (ep.get('apiKey') or '').strip()
        if not key:
            raise RuntimeError(f'还没填 {pid} 的 API Key（设置 → 语音输出 → 当前引擎 Key）')
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError('缺少 openai 库：pip install openai')
        base = (ep.get('baseUrl') or '').strip()
        if not base:
            raise RuntimeError(f'{pid} 的 Base URL 为空（设置 → 语音输出 → 当前引擎）')
        # 曾经这里把别家供应商的模型名写回 cfg['mimoTtsModel']（"模型联动"）：
        # 切到阿里云后配置里躺着 qwen 的模型名，切回 MiMo 时拿它去打请求，
        # 模型/音色/缓存键全部错位（#C「引擎切换后不同步」的后端根因）。
        # 模型解析现在只在 _eff_model() 里按 provider 做，绝不回写配置。
        self._client = OpenAI(api_key=key, base_url=base, timeout=120)
        self._client_key = (pid, base, key)      # 缓存键：换供应商/base/key 自动重建
        return self._client

    def _provider(self):
        return (self.cfg.get('ttsProvider') or 'dashscope').lower()

    def _model(self):
        # MiMo V2.5 已于 2026-10-21 下线，默认模型不再写死死值：
        # 没显式配置就交给注册表取该供应商最新一代（见 _eff_model）。
        return (self.cfg.get('mimoTtsModel') or '').strip()

    def _eff_model(self, provider):
        """该供应商「下一次合成真正会用的模型」：
        ttsEngineModels[pid] > 旧键（仅 mimo）> 注册表该供应商的最新一代（不是 MiMo 的默认）。
        旧模型名在这里一次性映射到最新等价模型（aliases），保证请求/日志/缓存键三者一致。"""
        provider = (provider or self._provider()).lower()
        mo = ''
        try:
            mo = ((self.cfg.get('ttsEngineModels') or {}).get(provider) or '').strip()
        except Exception:
            mo = ''
        if not mo and provider == 'mimo':
            mo = self._model()
        try:
            import speech_providers as _spv
            m, note = _spv.tts_canonical_model(provider, mo or _spv.tts_default_model(provider))
            if note:
                self._say(note)
            return m
        except Exception:
            return mo or self._model()

    # ---------- 合成主入口
    def synthesize(self, text, preset_id='narrator', provider=None, force=False,
                   voice_override=None, model_override=None, tag='', rate=None):
        """合成一段文本 -> 本地 wav 路径。

        voice_override / model_override 用于试听指定音色、Edge 的 voice 名、
        以及供应商中心里手填的模型名（都会被收敛成「合法且最新」的值）。
        """
        text = (text or '').strip()
        if not text:
            raise RuntimeError('文本为空')
        provider = (provider or self._provider()).lower()
        try:
            import speech_providers as _spv
        except Exception:
            _spv = None

        # ① 模型：显式覆盖 > 该供应商注册表的最新一代。
        #    曾在这里蹲死 self._model()（MiMo 的模型名）——切到阿里云后，缓存键和
        #    报错提示写的都是 MiMo 模型，实际请求又是 qwen：模型/音色/缓存全错位（#C）。
        model = (model_override or '').strip()
        if model and _spv:
            model, _mn = _spv.tts_canonical_model(provider, model)
            if _mn:
                self._say(_mn)
        model = model or (self._eff_model(provider) if _spv else self._model())

        # ①.5 MiMo V2.5 下线守卫：模型为空或在 MIMO_RETIRED_MODELS 里就不要发请求。
        #     直接打过去只会拿到 404/invalid model，前端只能看到一句天书；
        #     这里提前抛人话，指引用户切到阿里云/Edge（或手填 v2.6 模型名）。
        if provider == 'mimo' and _spv:
            low = (model or '').strip().lower()
            retired = {str(x).lower() for x in getattr(_spv, 'MIMO_RETIRED_MODELS', set())}
            if not low or low in retired:
                raise RuntimeError(getattr(_spv, 'MIMO_RETIRED_NOTE',
                                           'MiMo V2.5 已下线，请改用阿里云 Qwen TTS 或 Edge'))

        # ② 预设 + 音色。音色优先级：显式覆盖 > 预设自带 > 全局默认 > 兜底。
        #    （曾把全局 mimoVoice 排在预设前——男声预设全被全局女声顶掉）
        preset = self._preset(preset_id, provider, model)
        voice = (voice_override or preset.get('voice') or '').strip()
        if not voice:
            voice = (self.cfg.get('mimoVoice') or 'Chloe')

        # ③ 阿里云：音色必须属于当前模型系（跨系=Engine411），统一在这里收敛一次。
        #    音色下拉里直接点的真实音色优先；否则 全局 dashscopeVoice > MiMo 预设名映射 > 该系默认。
        instruct = (preset.get('instruct') or '').strip()
        if _spv and provider == 'dashscope':
            if not preset.get('_cat'):
                voice = (self.cfg.get('dashscopeVoice') or '').strip() \
                    or _MIMO_VOICE_MAP.get(voice, voice) or voice
            voice, _vn = _spv.tts_pick_voice(provider, model, voice)
            if _vn:
                self._say(_vn)

        # ③b Edge：预设自带的是 MiMo 风格名（Chloe/Mia/Dean…），Edge 神经音目录里没有，
        #     直接发会 Invalid voice。统一映射到最接近的神经音，再校验一次。
        if _spv and provider == 'edge':
            if not preset.get('_cat'):
                voice = _MIMO_EDGE_MAP.get(voice, voice)
            try:
                valid = {v.get('id') for v in _spv.tts_voices('edge')}
            except Exception:
                valid = set()
            if voice not in valid:
                voice = 'en-US-AriaNeural'

        # ④ 缓存键必须覆盖所有真正影响音频的参数：供应商/模型/音色/语速/风格指令/标签/文本。
        #    曾漏了 dashscopeVoice 与 edgeRate —— 换了音色仍命中上一个音色的音频
        #    （「试听听到的还是上一个音色」/「改了音色没反应」的根因）。
        key_src = '|'.join([provider, model, voice, str(rate or ''),
                            instruct, str(tag or ''), text])
        h = hashlib.sha1(key_src.encode('utf-8')).hexdigest()[:16]
        # 合成缓存统一放 cache/tts/ 子文件夹（persist，重打包不丢，可整夹清理）
        cdir = (self.cache_dir or Path('.')) / 'tts'
        out = cdir / f'tts_{h}.wav'
        if out.exists() and out.stat().st_size > 1000 and not force:
            self._say(f'用缓存：{out.name}')
            return str(out)

        try:
            if provider == 'edge':
                data = self._edge(text, voice, rate=rate)
            elif provider == 'dashscope':
                # 多供应商路由：阿里云 Qwen TTS（模型/音色已在上面收敛到同一系）
                data = self._dashscope(text, preset, voice, model)
            else:
                # mimo / volc / siliconflow / custom：OpenAI 兼容口
                if not model:
                    raise RuntimeError(f'{provider} 还没选模型（语音输出 → 模型名填一个再保存）')
                data = self._mimo(text, preset, voice, model)
        except RuntimeError as e:
            # 报错翻译层：错误码后面跟人话提示（模型不支持/Key/限流/超时），前端红字
            if _spv:
                raise RuntimeError(_spv.err_hint(model, str(e), 'tts')) from None
            raise
        data = _normalize_wav(data)
        cdir.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        self._say(f'合成完成：{out.name}（{len(data) / 1024:.0f} KB）')
        return str(out)

    # ---------- 阿里云/千问平台 Qwen TTS（多供应商路由之一）
    # 三系模型分家路由（官方文档口径，端点不可混用——url error/Engine411 的总根源）：
    #   qwen3-tts*             → /api/v1/services/aigc/multimodal-generation/generation
    #       input{text,voice,language_type}
    #   qwen-audio-*/cosyvoice* → /api/v1/services/audio/tts/SpeechSynthesizer
    #       input{text,voice,format,sample_rate}（cosyvoice 可带 instruction）
    #   MiniMax/*              → multimodal 口但 input 是 voice_setting/audio_setting 结构
    # 系别判定只认 speech_providers.tts_family（单一数据源），本文件不再自带音色表。
    # key/base 走 speech_providers.resolve_tts_endpoint：供应商中心里的覆盖能立即生效（热更新）。
    def _dashscope(self, text, preset, voice, model):
        try:
            import speech_providers as _spv
        except Exception:
            raise RuntimeError('speech_providers.py 载入失败，无法解析阿里云端点')
        ep = _spv.resolve_tts_endpoint(self.cfg, 'dashscope')
        key = (ep.get('apiKey') or '').strip() or (self.cfg.get('dashscopeApiKey') or '').strip()
        if not key:
            raise RuntimeError('还没填阿里云 DashScope API Key（设置 → 语音输出 → 当前引擎 Key）')
        # 模型一律按最新一代解析：显式选的模型（ttsEngineModels / 试听）优先，
        # 没有才用注册表默认；旧名在这里再兜一层 aliases。
        model, _mnote = _spv.tts_canonical_model('dashscope',
                                                 (model or '').strip() or ep.get('model') or '')
        if _mnote:
            self._say(_mnote)
        _bad = [x for x in ('realtime', 'asr', 'filetrans', 'embed', 'rerank') if x in (model or '').lower()]
        if _bad:
            raise RuntimeError(f'「{model}」是 {_bad[0]} 系模型，不能用于语音合成'
                               '（realtime=全双工对话/asr=识别/filetrans=离线转写）——请换 tts 系模型')
        # 高级覆盖（千问AI平台等）> 注册表 baseUrl（去掉 /api/v1 尾巴，端点自己拼）
        host = (self.cfg.get('dashscopeBaseUrl') or '').strip().rstrip('/')
        if not host:
            b = (ep.get('baseUrl') or '').strip().rstrip('/')
            host = (b[:-len('/api/v1')] if b.endswith('/api/v1') else b) or 'https://dashscope.aliyuncs.com'
        m = (model or '').lower()

        # ---- 系别 + 音色校验（音色必须属于该模型系，跨系=Engine411） ----
        fam = _spv.tts_family('dashscope', model)
        v, _vnote = _spv.tts_pick_voice('dashscope', model, voice)
        if _vnote:
            self._say(_vnote)
        instruct = (preset.get('instruct') or '').strip()
        # 风格指令**不能**拼进正文：qwen3-tts 会把整段 text 念出来，
        # 「Neutral, clear narration…」曾被当成句子先朗读一遍（真机 ASR 验证抓到）。
        # 要控制语气就传 input.instructions（qwen3）或 input.instruction（cosyvoice）。
        body = text
        # 官方上限：600 字符（qwen3-tts）；tts-next（Audiogen 系）3000
        limit = 3000 if ('qwen-audio' in m or 'cosyvoice' in m) and 'next' in m else 600
        if len(body) > limit:
            body = body[:limit]
            self._say(f'文本超 {limit} 字符，已截断')

        if fam == 'minimax':
            endpoint = '/api/v1/services/aigc/multimodal-generation/generation'
            payload = {'model': model,
                       'input': {'text': body,
                                 'voice_setting': {'voice_id': v, 'speed': 1, 'vol': 1, 'pitch': 0},
                                 'audio_setting': {'sample_rate': 32000, 'bitrate': 128000,
                                                   'format': 'wav', 'channel': 1}}}
        elif fam == 'qwen-audio':
            # Qwen-Audio/CosyVoice 系（含 tts-next·Audiogen）：WS 实时通道优先，
            # 失败回落 HTTP 口（tts-next 仅 WS，无 HTTP 回落）
            endpoint = '/api/v1/services/audio/tts/SpeechSynthesizer'
            ins = instruct[:100] if 'cosyvoice' in m and instruct else None
            try:
                return self._dashscope_ws(body, v, model, ins)
            except Exception as e:
                if 'next' in m:
                    raise RuntimeError(f'实时合成失败（tts-next 仅 WS 通道，无 HTTP 回落）：{e}') from None
                self._say(f'WS 通道失败（{str(e)[:100]}），回落 HTTP 口重试…')
            inp = {'text': body, 'voice': v, 'format': 'wav', 'sample_rate': 24000}
            if ins:
                inp['instruction'] = ins       # cosyvoice 指令参数名（qwen3 是 instructions）
            payload = {'model': model, 'input': inp}
        else:
            # 默认 qwen3-tts 系（multimodal 口；-realtime 后缀系是 Realtime API 另协议，未接）
            endpoint = '/api/v1/services/aigc/multimodal-generation/generation'
            ascii_ratio = sum(1 for c in body if ord(c) < 128) / max(1, len(body))
            inp = {'text': body, 'voice': v,
                   'language_type': 'English' if ascii_ratio > 0.5 else 'Chinese'}
            if instruct:
                inp['instructions'] = instruct[:200]   # 独立字段：不念出来，只影响语气
            payload = {'model': model, 'input': inp,
                       'parameters': {'text_type': 'PlainText'}}
        url = host + endpoint
        self._say(f'阿里云合成中…（{model} / {v} · {fam}系专用口）')

        import json as _json
        import base64 as _b64
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError

        def _post(pl):
            """SSE 流式优先：逐段收 output.audio.data（base64）边到边攒；
            响应不是 event-stream 时整读 JSON（兼容网关不支持 SSE）。
            统一返回 (res, err)：err 非空=请求/业务失败（含 HTTP 码与错误详情）。"""
            req = Request(url, data=_json.dumps(pl).encode('utf-8'), method='POST',
                          headers={'Authorization': f'Bearer {key}',
                                   'Content-Type': 'application/json',
                                   'X-DashScope-SSE': 'enable'})
            try:
                with urlopen(req, timeout=120) as resp:
                    ctype = (resp.headers.get('Content-Type') or '')
                    if 'event-stream' not in ctype:
                        return _json.loads(resp.read().decode('utf-8')), None
                    parts, last, err_code = [], None, ''
                    for raw in resp:
                        line = raw.decode('utf-8', 'replace').strip()
                        if not line.startswith('data:'):
                            continue
                        try:
                            chunk = _json.loads(line[5:])
                        except Exception:
                            continue
                        last = chunk
                        if chunk.get('code'):
                            err_code = f"{chunk.get('code')} {chunk.get('message', '')}"
                        # 每一块的 data 都是**独立 base64**（各自带填充），
                        # 必须逐块解码再拼字节；拼字符串再解一次会在第一块末尾
                        # 的 '=' 处截断——真机验证时 2.7 秒语音只剩 0.32 秒。
                        _d = ((chunk.get('output') or {}).get('audio') or {}).get('data') or ''
                        if _d:
                            try:
                                parts.append(_b64.b64decode(_d))
                            except Exception:
                                pass
                    if parts:
                        return {'_bytes': b''.join(parts)}, None
                    if err_code:
                        return None, err_code
                    return (last or {}), None
            except HTTPError as e:
                detail = ''
                try:
                    detail = (e.read().decode('utf-8') or '')[:200]
                except Exception:
                    pass
                return None, f'HTTP {e.code}: {detail or e.reason}'
            except Exception as e:
                return None, str(e)[:120]

        res, err = _post(payload)
        if err and 'url error' in err:
            # 口径防御：剥离可选参数再试一次（个别网关对 parameters 挑剔）
            slim = {'model': model,
                    'input': {k: v2 for k, v2 in payload['input'].items()
                              if k in ('text', 'voice', 'voice_setting')}}
            self._say('规范请求体被拒（url error），换精简体重试…')
            res, err = _post(slim)
        if err:
            raise RuntimeError(f'阿里云合成失败（{err}）')
        if res.get('code'):                       # 业务错误（整读 JSON 的 HTTP 200 但 code 非空）
            raise RuntimeError(f"阿里云合成失败：{res.get('code')} {res.get('message', '')}")
        if res.get('_bytes'):                      # SSE 逐块解码后攒的音频
            return res['_bytes']
        audio = ((res.get('output') or {}).get('audio')) or {}
        a_url = audio.get('url') or ''
        a_data = audio.get('data') or ''
        if a_url:
            with urlopen(a_url, timeout=60) as resp:
                blob = resp.read()
            if blob:
                return blob
        if a_data:
            return _b64.b64decode(a_data)
        raise RuntimeError('阿里云合成未返回音频（检查模型名是否 tts 系/音色是否属于该模型；'
                           f"finish={(res.get('output') or {}).get('finish_reason')}）")

    def _dashscope_ws(self, text, voice, model, instruct=None):
        """Qwen-Audio / CosyVoice 系（含 tts-next·Audiogen）实时通道——DashScope
        WS run-task 协议（官方口径：该系同模型名 WS/HTTP 双通道，实时交互走 WS）。
        用 SDK tts_v2 通道（websocket-client 已随 dashscope 打包）：
        run-task(参数) → continue-task(text) → finish-task → 二进制帧攒齐。
        base 映射：cfg.dashscopeBaseUrl 的 https:// → wss:// + /api-ws/v1/inference；
        百炼默认域名则不传 url（走 SDK 默认端点）。"""
        # key/base 同样走注册表解析：供应商中心里改了 Key/BaseUrl 立刻生效（热更新）
        try:
            import speech_providers as _spv
            ep = _spv.resolve_tts_endpoint(self.cfg, 'dashscope')
        except Exception:
            ep = {}
        key = (ep.get('apiKey') or '').strip() or (self.cfg.get('dashscopeApiKey') or '').strip()
        if not key:
            raise RuntimeError('还没填阿里云 DashScope API Key（设置 → 语音输出 → 当前引擎 Key）')
        import dashscope as _ds
        _ds.api_key = key
        from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
        host = (self.cfg.get('dashscopeBaseUrl') or '').strip().rstrip('/')
        if not host:
            b = (ep.get('baseUrl') or '').strip().rstrip('/')
            host = b[:-len('/api/v1')] if b.endswith('/api/v1') else b
        url = None
        if host and 'dashscope.aliyuncs.com' not in host:
            url = host.replace('https://', 'wss://').replace('http://', 'ws://') \
                + '/api-ws/v1/inference'
        self._say(f'实时合成中…（{model} / {voice} · WS 通道）')
        syn = SpeechSynthesizer(
            model=model, voice=voice,
            format=AudioFormat.WAV_24000HZ_MONO_16BIT,
            instruction=(instruct or None),
            url=url)
        data = syn.call(text)
        if not data:
            raise RuntimeError('WS 通道未返回音频（检查模型名/音色是否属于该模型系）')
        return data

    # ---------- MiMo 普通合成
    def _mimo(self, text, preset, voice, model):
        client = self._mimo_client()
        self._say(f'MiMo 合成中…（{preset.get("name")} / {voice}）')
        c = client.chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': preset.get('instruct') or ''},
                      {'role': 'assistant', 'content': text}],
            audio={'format': 'wav', 'voice': voice},
        )
        return self._audio_bytes(c)

    @staticmethod
    def _audio_bytes(completion):
        msg = completion.choices[0].message
        audio = getattr(msg, 'audio', None)
        if audio is None:
            raise RuntimeError('模型没返回音频：' + str(msg)[:200])
        data = audio.data if hasattr(audio, 'data') else (
            audio.get('data') if isinstance(audio, dict) else None)
        if not data:
            raise RuntimeError('返回的音频为空')
        return base64.b64decode(data)

    # ---------- 音色工作台（声音设计 / 声音复刻）：后端已移除（#9）
    #
    # 删除的东西（历史上都在这里）：_voicedesign（文字描述造音色）、_voiceclone（参考音频克隆）、
    # _as_dataurl、design_voice、clone_voice、_design_voice_instruct，以及 synthesize 里
    # 「kind=design/clone」「模型名以 voicedesign/voiceclone 结尾」的四条特殊分支。
    # 连带失效：voices.json 里的 design_/clone_ 条目、kind/refAudio/voiceId 字段、
    # ttsEngineModels 里指向 voicedesign/voiceclone 的值（后者由 aliases 自动映射）。
    #
    # 保留同名方法只为给 app.py 侧同步删除留过渡（否则 AttributeError 裸栈直接漏到前端）。
    def design_voice(self, *a, **kw):
        raise RuntimeError('「声音设计」已下线（音色工作台整块移除）——请在语音输出里直接选模型自带的音色')

    def clone_voice(self, *a, **kw):
        raise RuntimeError('「声音复刻」已下线（音色工作台整块移除）——请在语音输出里直接选模型自带的音色')

    # ---------- Edge TTS
    def _edge(self, text, voice, rate=None):
        try:
            import edge_tts  # noqa
        except ImportError:
            raise RuntimeError('用 Edge TTS 需要先装：pip install edge-tts')
        import asyncio
        import uuid
        import edge_tts
        if not voice or voice.startswith('Chinese (Mandarin)'):
            voice = 'en-US-AriaNeural'
        # 曾经默认 '-10%'：单词被拖慢一成，跟读示范和单词读音两个速度，越听越糊。
        # 单词读音必须跟原速（+0%），rate 只在显式传值时生效。2026-10-16 修复。
        rate = rate or self.cfg.get('edgeRate') or '+0%'
        # 唯一临时文件：曾经的固定名 _edge_tmp.mp3 会被并发调用互相覆盖
        # （VREV 队列自动播 + 用户点词同时合成 → 音频串音/截断）。2026-10-16 修复。
        tmp = (self.cache_dir or Path('.')) / f'_edge_{uuid.uuid4().hex[:8]}.mp3'
        wav = tmp.with_suffix('.wav')
        async def go():
            await edge_tts.Communicate(text, voice, rate=rate).save(str(tmp))
        try:
            asyncio.run(go())
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError('Edge TTS 没生成文件')
            ff = find_ffmpeg()
            if not ff:
                # 没 ffmpeg：明确告诉用户，而不是静默返回 mp3 塞进 .wav 缓存
                # （扩展名/内容不符，localfs 按 .wav 发 audio/wav，WebView2 严格模式下静默失败）
                raise RuntimeError('没找到 ffmpeg，Edge 合成结果无法转成 wav（装 ffmpeg 并加进 PATH 后重试）')
            r = subprocess.run([ff, '-hide_banner', '-loglevel', 'error', '-y',
                                '-i', str(tmp), '-ar', '24000', '-ac', '1', str(wav)],
                               capture_output=True, creationflags=_no_window())
            if not (wav.exists() and wav.stat().st_size > 0):
                # 曾经的漏洞：ffmpeg 失败后继续 wav.read_bytes() → FileNotFoundError
                # 裸栈抛到前端。现在给一句人话（含 ffmpeg 自己的报错尾巴）。2026-10-16 修复。
                tail = (r.stderr or b'').decode('utf-8', 'replace').strip().splitlines()
                raise RuntimeError('ffmpeg 转 wav 失败：' + (tail[-1][:120] if tail else '无输出，检查 ffmpeg 是否可用'))
            return wav.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)

    # ---------- 给界面用
    def list_presets(self, provider=None):
        """当前引擎下「音色下拉」要显示的东西。

        · mimo / volc / siliconflow / custom（OpenAI 兼容口）：风格预设（id + 风格指令）；
        · edge / dashscope：真实音色名（来自注册表该模型系的音色表），首项是
          「默认（留空）」——留空就用该系默认音色，用户不必被一堆音色吓到。
        曾经这里对任何引擎都返回 MiMo 的 23 个风格预设：选了阿里云看到的还是
        Chloe/Mia/Dean，真正发出去的却是被映射后的 Cherry——「音色对不上」的观感来源。
        """
        provider = (provider or self._provider()).lower()
        try:
            import speech_providers as _spv
        except Exception:
            _spv = None
        real_voice = bool(_spv) and bool(_spv.tts_voices(provider, self._eff_model(provider) if _spv else None,
                                                         with_default=False))
        if real_voice:
            model = self._eff_model(provider)
            out = []
            for v in _spv.tts_voices(provider, model):
                out.append({'id': v.get('id') or '', 'name': v.get('label') or v.get('id') or '',
                            'desc': v.get('desc') or '', 'group': v.get('group') or '当前引擎音色',
                            'family': v.get('family') or ''})
            return out
        out = [{'id': p['id'], 'name': p['name'], 'desc': p['desc'],
                'group': p.get('group') or '其它'} for p in VOICE_PRESETS]
        # 练习页的「自定义风格」仍可用；design_/clone_ 已在 __init__ 里过滤
        for p in self.extra_presets:
            out.append({'id': p.get('id'), 'name': p.get('name'),
                        'desc': p.get('desc') or '', 'group': p.get('group') or '我的音色',
                        'custom': True})
        return out

    def test(self):
        try:
            p = self.synthesize('Hello, this is a voice test.', 'narrator', force=True, tag='selftest')
            return {'ok': True, 'file': p}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}


def find_ffmpeg():
    """与 tedlibrarian.find_ffmpeg 同一套查找逻辑（避免两处重复维护）。"""
    try:
        import tedlibrarian
        return tedlibrarian.find_ffmpeg()
    except Exception:
        return None
