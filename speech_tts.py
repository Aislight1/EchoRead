#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音合成（TTS）

主用小米 MiMo-V2.5-TTS（OpenAI 兼容协议）；另留 Edge TTS 作免费后备。

MiMo 三个模型（实测 2026-10）：
  mimo-v2.5-tts             普通合成，audio.voice 传内置音色名
  mimo-v2.5-tts-voicedesign 用**文字描述**设计音色，**不能传 voice**；返回一个 id
  mimo-v2.5-tts-voiceclone  音色克隆，audio.voice 传**参考音频的 DataURL**；返回一个 id
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
        self.extra_presets = extra_presets or []      # 自定义/设计/克隆出来的音色

    def _say(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    # ---------- 音色查找（先内置，再自定义）
    def _preset(self, pid):
        for p in VOICE_PRESETS:
            if p['id'] == pid:
                return p
        for p in self.extra_presets:
            if p.get('id') == pid:
                return p
        return VOICE_PRESETS[0]

    # ---------- 客户端
    def _mimo_client(self):
        """TTS 客户端（多供应商）：按 ttsProvider 解析端点，全部走 OpenAI 兼容协议。

        供应商注册表见 speech_providers.py；speechProviders[pid] 用户覆盖优先。
        mimo 保持旧行为（mimoApiKey/mimoBaseUrl）；其它供应商读 speechProviders 配置。
        ⚠ 缓存键=(供应商,base,key)：换引擎后必须重建客户端——曾经单例缓存，
        切了供应商仍打旧端点旧模型（"串到别的模型"的真凶）。"""
        import speech_providers as _spv
        try:
            _ep = _spv.resolve_tts_endpoint(self.cfg, (self.cfg.get('ttsProvider') or 'mimo').strip())
        except Exception:
            _ep = {}
        _ck = (_ep.get('provider'), _ep.get('baseUrl'), _ep.get('apiKey'))
        if self._client is not None and getattr(self, '_client_key', None) == _ck:
            return self._client
        pid = (self.cfg.get('ttsProvider') or 'mimo').strip()
        try:
            import speech_providers as spv
            ep = spv.resolve_tts_endpoint(self.cfg, pid)
        except Exception:
            ep = {'provider': 'mimo', 'baseUrl': self.cfg.get('mimoBaseUrl'),
                  'apiKey': self.cfg.get('mimoApiKey'),
                  'model': self.cfg.get('mimoTtsModel')}
        key = (ep.get('apiKey') or '').strip()
        if not key:
            raise RuntimeError(f'还没填 {pid} 的 API Key（设置 → 语音合成）')
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError('缺少 openai 库：pip install openai')
        base = (ep.get('baseUrl') or '').strip()
        if not base:
            raise RuntimeError(f'{pid} 的 Base URL 为空（设置 → 语音合成）')
        # 模型联动：注册表默认模型 or 用户覆盖
        if pid != 'mimo':
            m = (ep.get('model') or '').strip()
            if m and not self.cfg.get('mimoTtsModel'):
                self.cfg['mimoTtsModel'] = m
        self._client = OpenAI(api_key=key, base_url=base, timeout=120)
        self._client_key = (pid, base, key)      # 缓存键：换供应商/base/key 自动重建
        return self._client

    def _provider(self):
        return (self.cfg.get('ttsProvider') or 'mimo').lower()

    def _model(self):
        return (self.cfg.get('mimoTtsModel') or 'mimo-v2.5-tts').strip()

    # ---------- 合成主入口
    def synthesize(self, text, preset_id='narrator', provider=None, force=False,
                   voice_override=None, model_override=None, tag='', rate=None):
        """合成一段文本 -> 本地 wav 路径。

        voice_override / model_override 用于：
          · 设计出来的音色（voicedesign 返回的 id）
          · 克隆出来的音色（voiceclone 返回的 id）
          · Edge TTS 的 voice 名
        """
        text = (text or '').strip()
        if not text:
            raise RuntimeError('文本为空')
        provider = (provider or self._provider()).lower()
        preset = self._preset(preset_id)
        model = (model_override or self._model())
        # 音色优先级：显式覆盖 > 预设自带 > 全局默认 > 兜底。
        # 曾经全局 mimoVoice 排在预设前——男声预设全被全局女声顶掉（用户实测男声全女声的真凶）
        voice = voice_override or preset.get('voice') or self.cfg.get('mimoVoice') or 'Chloe'

        key_src = f'{provider}|{model}|{voice}|{preset.get("id")}|{tag}|{text}'
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
                # 多供应商路由（P-供应商中心）：阿里云 Qwen TTS，模型优先 preset.model
                # 默认 qwen3-tts-flash——非实时 HTTP 口已验证的稳定款（3.1 系是实时流式系）
                data = self._dashscope(text, preset, voice,
                                       model_override or preset.get('model') or 'qwen3-tts-flash')
            elif preset.get('kind') == 'design':
                # 设计出来的音色：**不能**把它的 id 当 voice 传给普通合成
                # （实测报 "Unknown voice: <id>"），必须重新过 voicedesign 模型
                data, vid = self._voicedesign(text, preset.get('instruct') or 'A clear voice.')
                self._say(f'用设计音色「{preset.get("name")}」合成（id={str(vid)[:8]}…）')
            elif preset.get('kind') == 'clone':
                # 克隆音色同理：id 不能复用，必须拿参考音频重新克隆
                ref = preset.get('refAudio') or ''
                if not ref or not Path(ref).exists():
                    raise RuntimeError(f'克隆音色「{preset.get("name")}」的参考音频找不到了，请重新克隆一次')
                data, vid = self._voiceclone(text, preset.get('instruct') or 'A natural clear voice.',
                                             self._as_dataurl(ref))
                self._say(f'用克隆音色「{preset.get("name")}」合成（id={str(vid)[:8]}…）')
            elif model.endswith('voicedesign'):
                data, vid = self._voicedesign(text, preset.get('instruct') or 'A clear voice.')
                self._say(f'音色设计 id={vid}')
            elif model.endswith('voiceclone'):
                data, vid = self._voiceclone(text, preset.get('instruct') or 'A clear voice.', voice)
                self._say(f'音色克隆 id={vid}')
            else:
                data = self._mimo(text, preset, voice, model)
        except RuntimeError as e:
            # 报错翻译层：错误码后面跟人话提示（模型不支持/Key/限流/超时），前端红字
            import speech_providers as _spv
            raise RuntimeError(_spv.err_hint(model, str(e), 'tts')) from None
        cdir.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        self._say(f'合成完成：{out.name}（{len(data) / 1024:.0f} KB）')
        return str(out)

    # ---------- 阿里云/千问平台 Qwen TTS（多供应商路由之一）
    # 三系模型分家路由（官方文档口径，端点不可混用——url error/Engine411 的总根源）：
    #   qwen-audio-*/cosyvoice* → /api/v1/services/audio/tts/SpeechSynthesizer
    #       input{text,voice,format,sample_rate}（cosyvoice 可带 instruction）
    #   qwen3-tts*             → /api/v1/services/aigc/multimodal-generation/generation
    #       input{text,voice,language_type}
    #   MiniMax/*              → multimodal 口但 input 是 voice_setting/audio_setting 结构
    # base 可配 cfg.dashscopeBaseUrl（百炼默认；千问平台填 https://maas.qianwenaiapi.com）
    QW_TTS_VOICE_SETS = {
        'qwen3': ['Cherry', 'Serena', 'Ethan', 'Maia', 'Chelsie', 'Momo', 'Kai', 'Nofish',
                  'Bella', 'Jennifer', 'Ryan', 'Moon', 'Mia', 'Vincent', 'Neil', 'Andre'],
        'qa': ['longanyang', 'longanhuan_v3.6', 'longanlingxin', 'longanlufeng', 'xunanchuan_v3.1',
               'loongeva', 'loongjohn', 'loongmary', 'longjielidou_v3.6', 'longpaopao_v3.6',
               'longhuohuo_v3.6', 'longchuanshu_v3.6'],
        'minimax': ['male-qn-qingse'],
    }

    def _dashscope(self, text, preset, voice, model):
        key = (self.cfg.get('dashscopeApiKey') or '').strip()
        if not key:
            raise RuntimeError('还没填阿里云 DashScope API Key（设置 → 语音输出）')
        _bad = [x for x in ('realtime', 'asr', 'filetrans', 'embed', 'rerank') if x in (model or '').lower()]
        if _bad:
            raise RuntimeError(f'「{model}」是 {_bad[0]} 系模型，不能用于语音合成'
                               '（realtime=全双工对话/asr=识别/filetrans=离线转写）——请换 tts 系模型')
        host = (self.cfg.get('dashscopeBaseUrl') or 'https://dashscope.aliyuncs.com').rstrip('/')
        m = (model or '').lower()

        # ---- 系别判定 + 端点 + 请求体 + 音色校验（音色必须属于该模型系，跨系=Engine411） ----
        mimo_map = {'Chloe': 'Cherry', 'Mia': 'Serena', 'Dean': 'Ethan', 'Milo': 'Ethan'}  # MiMo 预设名→qwen3 系
        cfg_voice = (self.cfg.get('dashscopeVoice') or '').strip()
        v = cfg_voice or mimo_map.get(voice, voice) or ''
        instruct = (preset.get('instruct') or '').strip()
        body = (f'{instruct}\n{text}') if instruct else text
        # 官方上限：600 字符（qwen3-tts）；tts-next（Audiogen 系）3000
        limit = 3000 if ('qwen-audio' in m or 'cosyvoice' in m) and 'next' in m else 600
        if len(body) > limit:
            body = body[:limit]
            self._say(f'文本超 {limit} 字符，已截断')

        if m.startswith('minimax') or '/' in (model or ''):
            fam, endpoint = 'minimax', '/api/v1/services/aigc/multimodal-generation/generation'
            if v not in self.QW_TTS_VOICE_SETS['minimax']:
                v = 'male-qn-qingse'
            payload = {'model': model,
                       'input': {'text': body,
                                 'voice_setting': {'voice_id': v, 'speed': 1, 'vol': 1, 'pitch': 0},
                                 'audio_setting': {'sample_rate': 32000, 'bitrate': 128000,
                                                   'format': 'wav', 'channel': 1}}}
        elif 'qwen-audio' in m or 'cosyvoice' in m:
            # Qwen-Audio/CosyVoice 系（含 tts-next·Audiogen）：WS 实时通道优先，
            # 失败回落 HTTP 口（tts-next 仅 WS，无 HTTP 回落）
            fam, endpoint = 'qa', '/api/v1/services/audio/tts/SpeechSynthesizer'
            if v not in self.QW_TTS_VOICE_SETS['qa']:
                v = 'longanyang'
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
            fam, endpoint = 'qwen3', '/api/v1/services/aigc/multimodal-generation/generation'
            if v not in self.QW_TTS_VOICE_SETS['qwen3']:
                v = 'Cherry'
            ascii_ratio = sum(1 for c in body if ord(c) < 128) / max(1, len(body))
            payload = {'model': model,
                       'input': {'text': body, 'voice': v,
                                 'language_type': 'English' if ascii_ratio > 0.5 else 'Chinese'},
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
                    b64, last, err_code = '', None, ''
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
                        b64 += ((chunk.get('output') or {}).get('audio') or {}).get('data') or ''
                    if b64:
                        return {'_b64': b64}, None
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
        if res.get('_b64'):                        # SSE 攒的音频
            return _b64.b64decode(res['_b64'])
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
        key = (self.cfg.get('dashscopeApiKey') or '').strip()
        if not key:
            raise RuntimeError('还没填阿里云 DashScope API Key（设置 → 语音输出）')
        import dashscope as _ds
        _ds.api_key = key
        from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
        host = (self.cfg.get('dashscopeBaseUrl') or '').strip().rstrip('/')
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

    @staticmethod
    def _audio_meta(completion):
        msg = completion.choices[0].message
        a = getattr(msg, 'audio', None)
        return a.model_dump() if hasattr(a, 'model_dump') else (a if isinstance(a, dict) else {})

    # ---------- 音色设计（文字描述 -> 音色）
    def _voicedesign(self, text, instruct):
        client = self._mimo_client()
        self._say('音色设计中…（用文字描述生成）')
        c = client.chat.completions.create(
            model='mimo-v2.5-tts-voicedesign',
            messages=[{'role': 'user', 'content': instruct},
                      {'role': 'assistant', 'content': text}],
            audio={'format': 'wav'},          # 注意：voicedesign 不能传 voice
        )
        return self._audio_bytes(c), (self._audio_meta(c).get('id') or '')

    # ---------- 音色克隆（参考音频 -> 音色）
    @staticmethod
    def _as_dataurl(path):
        return 'data:audio/wav;base64,' + base64.b64encode(Path(path).read_bytes()).decode()

    def _voiceclone(self, text, instruct, voice):
        client = self._mimo_client()
        v = voice or ''
        if not v.startswith('data:'):
            raise RuntimeError('音色克隆需要 audio.voice 传参考音频的 DataURL')
        self._say('音色克隆中…')
        c = client.chat.completions.create(
            model='mimo-v2.5-tts-voiceclone',
            messages=[{'role': 'user', 'content': instruct},
                      {'role': 'assistant', 'content': text}],
            audio={'format': 'wav', 'voice': v},
        )
        return self._audio_bytes(c), (self._audio_meta(c).get('id') or '')

    # ---------- 高级：设计 / 克隆一个音色并保存
    #
    # 重要（实测 2026-10）：voicedesign / voiceclone 返回的 id **不能**当 voice
    # 传给普通 mimo-v2.5-tts（会报 "Unknown voice: <id>"）。
    # 所以自定义音色必须记住「怎么重新造出来」：
    #   design 音色  -> 存 instruct（风格描述）
    #   clone  音色  -> 把参考音频**持久化保存**，每次合成重新克隆
    # 这也是 _preset() 里给它们打 kind 标记的原因。
    def design_voice(self, name, instruct, sample_text=None):
        text = sample_text or TEST_SAMPLES['en']
        data, vid = self._voicedesign(text, instruct)
        p = (self.cache_dir or Path('.')) / f'design_{hashlib.sha1(name.encode()).hexdigest()[:8]}.wav'
        p.write_bytes(data)
        return {'ok': True, 'voiceId': vid, 'file': str(p), 'name': name,
                'instruct': instruct, 'kind': 'design'}

    def clone_voice(self, name, ref_audio_path, instruct=None, sample_text=None):
        src = Path(ref_audio_path)
        if not src.exists():
            raise RuntimeError('参考音频不存在：' + str(src))
        # 持久化一份参考音频（放 voices/ 下，不会被缓存清理）
        keep_dir = (self.cache_dir or Path('.')) / 'voices'
        keep_dir.mkdir(parents=True, exist_ok=True)
        kept = keep_dir / f'ref_{hashlib.sha1(name.encode()).hexdigest()[:8]}{src.suffix or ".wav"}'
        if str(src.resolve()) != str(kept.resolve()):
            kept.write_bytes(src.read_bytes())

        durl = self._as_dataurl(kept)
        text = sample_text or TEST_SAMPLES['en']
        ins = instruct or 'A natural, clear voice.'
        data, vid = self._voiceclone(text, ins, durl)
        p = (self.cache_dir or Path('.')) / f'clone_{hashlib.sha1(name.encode()).hexdigest()[:8]}.wav'
        p.write_bytes(data)
        return {'ok': True, 'voiceId': vid, 'file': str(p), 'name': name,
                'instruct': ins, 'kind': 'clone', 'refAudio': str(kept)}

    def _design_voice_instruct(self, preset):
        return preset.get('instruct') or 'A clear voice.'

    # ---------- Edge TTS
    def _edge(self, text, voice, rate=None):
        try:
            import edge_tts  # noqa
        except ImportError:
            raise RuntimeError('用 Edge TTS 需要先装：pip install edge-tts')
        import asyncio
        import edge_tts
        if not voice or voice.startswith('Chinese (Mandarin)'):
            voice = 'en-US-AriaNeural'
        rate = rate or self.cfg.get('edgeRate') or '-10%'
        tmp = (self.cache_dir or Path('.')) / '_edge_tmp.mp3'
        async def go():
            await edge_tts.Communicate(text, voice, rate=rate).save(str(tmp))
        asyncio.run(go())
        if not tmp.exists():
            raise RuntimeError('Edge TTS 没生成文件')
        wav = tmp.with_suffix('.wav')
        ff = find_ffmpeg()
        if ff:
            subprocess.run([ff, '-hide_banner', '-loglevel', 'error', '-y',
                            '-i', str(tmp), str(wav)],
                           capture_output=True, creationflags=_no_window())
            if wav.exists():
                tmp.unlink(missing_ok=True)
                return wav.read_bytes()
        return tmp.read_bytes()

    # ---------- 给界面用
    def list_presets(self):
        out = [{'id': p['id'], 'name': p['name'], 'desc': p['desc'],
                'group': p.get('group') or '其它'} for p in VOICE_PRESETS]
        for p in self.extra_presets:
            out.append({'id': p.get('id'), 'name': p.get('name'),
                        'desc': p.get('desc') or '', 'group': p.get('group') or '我的音色',
                        'custom': True, 'kind': p.get('kind')})
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
