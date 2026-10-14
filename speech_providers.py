# -*- coding: utf-8 -*-
"""语音/LLM 多供应商注册表（供应商中心后端数据源）。

结构：PROVIDERS[category][pid]，category='llm'|'asr'|'tts'。
用户在设置页点供应商卡片 → baseUrl 预填可改 → 拉模型(canPull)或用内置清单 → 测速。
用户覆盖存 config.speechProviders[pid]（兼容旧设计）与 config.customProviders[cat][pid]（自定义新供应商）。

计价/口碑出处：_tools/_speech_models_report.md + 2026-10-11 实网抓取（见 _opus_research.md）。
已知时间点：MiMo V2.5 全系 2026-10-21 下线 → v2.6。
"""
import json
import subprocess
import time
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def _no_window():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


PROVIDERS = {
    # ================= LLM =================
    'llm': {
        'dashscope': {
            'id': 'dashscope', 'label': '阿里云百炼（通义千问）', 'logo': '☁️',
            'baseUrl': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'canPull': True,
            'builtin': [
                {'id': 'qwen-plus', 'label': 'Qwen-Plus（均衡）'},
                {'id': 'qwen-turbo', 'label': 'Qwen-Turbo（快）'},
                {'id': 'qwen-max', 'label': 'Qwen-Max（旗舰）'},
                {'id': 'deepseek-v3', 'label': 'DeepSeek-V3（百炼托管）'},
            ],
            'auth': 'key', 'keyHint': '阿里云百炼 API Key（sk-…）', 'keyName': 'llmApiKey',
            'note': '⟳可拉取你账号下全部可用模型。本地直传实测：qwen3.8-omni-flash / qwen3-asr-flash（compatible口base64✓）、paraformer-realtime-v2（流式词级✓）；paraformer-v2 与 asr-flash-message/filetrans 只收公网 URL（需 OSS）。分工：练习识别=paraformer-realtime-v2（词级）；音频处理（生成字幕/转写文本）=qwen3.8-omni-flash；时间轴定稿（诊断修正对齐/重建）=paraformer 词级专用，omni 无词级不能替代。热词：paraformer / qwen-audio-3.x 系',
        },
        'deepseek': {
            'id': 'deepseek', 'label': 'DeepSeek 官方', 'logo': '🔷',
            'baseUrl': 'https://api.deepseek.com', 'canPull': True,
            'builtin': [
                {'id': 'deepseek-chat', 'label': 'DeepSeek-Chat'},
                {'id': 'deepseek-reasoner', 'label': 'DeepSeek-Reasoner（推理）'},
            ],
            'auth': 'key', 'keyHint': 'DeepSeek 平台 API Key', 'keyName': 'llmApiKey',
            'docs': 'https://api-docs.deepseek.com/', 'note': '',
        },
        'mimo': {
            'id': 'mimo', 'label': '小米 MiMo', 'logo': 'M',
            'baseUrl': 'https://api.xiaomimimo.com/v1', 'canPull': True,
            'builtin': [
                {'id': 'mimo-v2.6-pro', 'label': 'MiMo-V2.6-Pro（¥3/¥6 每百万）'},
                {'id': 'mimo-v2.6-flash', 'label': 'MiMo-V2.6-Flash（¥1/¥2，划算）'},
            ],
            'auth': 'key', 'keyHint': 'platform.xiaomimimo.com 控制台 Key', 'keyName': 'mimoApiKey',
            'docs': 'https://mimo.xiaomimimo.com/', 'note': '⚠ V2.5 系 2026-10-21 下线，已列 v2.6',
        },
        'qianwenai': {
            'id': 'qianwenai', 'label': '千问 AI 平台', 'logo': 'Q',
            'baseUrl': 'https://maas.qianwenaiapi.com/compatible-mode/v1', 'canPull': True,
            'builtin': [
                {'id': 'qwen3.8-max', 'label': 'Qwen3.8-Max'},
                {'id': 'qwen3.7-plus', 'label': 'Qwen3.7-Plus'},
                {'id': 'qwen3.8-flash', 'label': 'Qwen3.8-Flash'},
            ],
            'auth': 'key', 'keyHint': '千问 AI 平台 Key（有免费额度）', 'keyName': 'llmApiKey',
            'docs': 'https://platform.qianwenai.com/docs/developer-guides/getting-started/introduction',
            'note': 'OpenAI 兼容，可当百炼备胎',
        },
        'openai': {
            'id': 'openai', 'label': 'OpenAI', 'logo': 'O',
            'baseUrl': 'https://api.openai.com/v1', 'canPull': True,
            'builtin': [],                       # 模型多，全走拉取
            'auth': 'key', 'keyHint': 'OpenAI API Key', 'keyName': 'llmApiKey',
            'docs': 'https://platform.openai.com/docs/', 'note': '',
        },
        'custom': {
            'id': 'custom', 'label': '自定义（OpenAI 兼容）', 'logo': '⚙️',
            'baseUrl': '', 'canPull': True, 'builtin': [],
            'auth': 'key', 'keyHint': '任意 OpenAI 兼容端点的 Key', 'keyName': 'llmApiKey',
            'docs': '', 'note': '',
        },
    },
    # ================= ASR =================
    'asr': {
        'dashscope': {
            'id': 'dashscope', 'label': '阿里云百炼识别', 'logo': '☁️',
            'baseUrl': '', 'canPull': True,
            # 拉模型走 compatible-mode 的 /models（原生 api/v1 没有这个端点）
            'pullBaseUrl': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'builtin': [
                {'id': 'qwen3.8-omni-flash', 'label': 'Qwen3.8-Omni-Flash（✓长音频文件转写·本地直传·推荐给音频处理）'},
                {'id': 'paraformer-realtime-v2', 'label': 'Paraformer-Realtime-V2（✓词级时间戳·练习识别/对齐/重建专用）'},
                {'id': 'qwen3-asr-flash', 'label': 'Qwen3-ASR-Flash（✓本地直传·无词级·轻量转写）'},
                {'id': 'paraformer-v2', 'label': 'Paraformer-V2（官方非实时推荐·但只收公网URL·OSS用户用）'},
                {'id': 'qwen-audio-3.1-asr-flash-message', 'label': 'ASR-Flash-Message（超长音频+热词·只收公网URL·OSS用户用）'},
                {'id': 'qwen-audio-3.1-asr-flash-filetrans', 'label': 'ASR-Flash-Filetrans（异步文件转写·只收公网URL）'},
                {'id': 'qwen3.5-livetranslate-flash-realtime', 'label': 'LiveTranslate-Realtime（实时同传·本应用不适用）'},
            ],
            'auth': 'key', 'keyHint': '阿里云百炼 API Key', 'keyName': 'dashscopeApiKey',
            'docs': 'https://help.aliyun.com/zh/model-studio/', 'note': '⟳可拉取你账号下全部可用模型；热词走预编译词表（本卡下方热词区块，paraformer / qwen-audio-3.x 系支持）；选型：paraformer-realtime=实时流式（练习录音/对齐），qwen3.8-omni-flash=长音频文件转写（配「分片并行」模式，时间轴由诊断修正对齐定稿）',
        },
        'mimo': {
            'id': 'mimo', 'label': '小米 MiMo 识别', 'logo': 'M',
            'baseUrl': 'https://api.xiaomimimo.com/v1', 'canPull': False,
            'builtin': [{'id': 'mimo-v2.5-asr', 'label': 'MiMo-ASR（¥0.5/时·无词级时间戳）'}],
            'auth': 'key', 'keyHint': '小米 MiMo Key', 'keyName': 'mimoApiKey',
            'docs': 'https://mimo.xiaomimimo.com/', 'note': '⚠ V2.5 系 2026-10-21 下线',
        },
        'doubao': {
            'id': 'doubao', 'label': '火山方舟豆包识别', 'logo': '🔥',
            'baseUrl': 'https://openspeech.bytedance.com', 'canPull': False,
            'builtin': [{'id': 'seed-ASR-2.0', 'label': 'Seed-ASR-2.0（✓词级时间戳）'}],
            'auth': 'dual',
            'keyHint': '双参鉴权：AppID + Access Token（火山控制台→豆包语音→API 服务中心）',
            'keyName': 'volcApiKey',
            'docs': 'https://docs.volcengine.com/docs/DoubaoVoice/LargemodelrecordingfilerecognitionstandardversionAPI',
            'note': '接口已预留，引擎未接——需先申请 appid+token；Ark 兼容口 ark.cn-beijing.volces.com/api/v3 亦可试',
        },
        'siliconflow': {
            'id': 'siliconflow', 'label': 'SiliconFlow（聚合）', 'logo': 'S',
            'baseUrl': 'https://api.siliconflow.cn/v1', 'canPull': True,
            'builtin': [{'id': 'FunAudioLLM/SenseVoiceSmall', 'label': 'SenseVoice-Small（快·无词级）'}],
            'auth': 'key', 'keyHint': 'SiliconFlow Key（用户已有）', 'keyName': 'siliconflowApiKey',
            'docs': 'https://siliconflow.cn/', 'note': '实验：OpenAI 兼容音频口，未实测',
        },
        'custom': {
            'id': 'custom', 'label': '自定义识别端点', 'logo': '⚙️',
            'baseUrl': '', 'canPull': True, 'builtin': [],
            'auth': 'key', 'keyHint': '', 'keyName': 'customSpeechApiKey',
            'docs': '', 'note': '',
        },
    },
    # ================= TTS =================
    'tts': {
        'edge': {
            'id': 'edge', 'label': 'Edge 神经音（本地免费）', 'logo': '🅔',
            'baseUrl': '', 'canPull': False,
            'builtin': [
                {'id': 'en-US-AriaNeural', 'label': 'Aria（美音·女）'},
                {'id': 'en-US-GuyNeural', 'label': 'Guy（美音·男）'},
                {'id': 'en-GB-SoniaNeural', 'label': 'Sonia（英音·女）'},
                {'id': 'en-GB-RyanNeural', 'label': 'Ryan（英音·男）'},
                {'id': 'zh-CN-XiaoxiaoNeural', 'label': '晓晓（中文·女）'},
                {'id': 'zh-CN-YunxiNeural', 'label': '云希（中文·男）'},
            ],
            'auth': 'none', 'keyHint': '', 'keyName': '',
            'docs': 'https://github.com/rany2/edge-tts', 'note': '单词朗读/兜底首选', 'features': [],
        },
        'mimo': {
            'id': 'mimo', 'label': '小米 MiMo TTS', 'logo': 'M',
            'baseUrl': 'https://api.xiaomimimo.com/v1', 'canPull': True,
            'builtin': [
                {'id': 'mimo-v2.5-tts', 'label': 'V2.5-TTS（风格指令·限时免费）'},
                {'id': 'mimo-v2.5-tts-voiceclone', 'label': 'V2.5-VoiceClone（克隆）'},
                {'id': 'mimo-v2.5-tts-voicedesign', 'label': 'V2.5-VoiceDesign（设计）'},
            ],
            'auth': 'key', 'keyHint': '小米 MiMo Key', 'keyName': 'mimoApiKey',
            'docs': 'https://mimo.xiaomimimo.com/docs/usage-guide/speech-synthesis-v2.5',
            'note': '⚠ V2.5 系 2026-10-21 下线（v2.6 待发布即切）', 'features': ['clone', 'design'],
        },
        'dashscope': {
            'id': 'dashscope', 'label': '阿里云 Qwen TTS', 'logo': '☁️',
            'baseUrl': 'https://dashscope.aliyuncs.com/api/v1', 'canPull': True,
            'pullBaseUrl': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'builtin': [
                {'id': 'qwen-audio-3.0-tts-flash', 'label': 'Qwen-Audio-3.0-TTS-Flash（❌实测411不可用·换qwen3/cosyvoice）'},
                {'id': 'cosyvoice-v3-flash', 'label': 'CosyVoice-V3-Flash（✓专用口·快·90音色）'},
                {'id': 'qwen3-tts-flash', 'label': 'Qwen3-TTS-Flash（multimodal口·48音色·0.8元/万字符）'},
                {'id': 'qwen-audio-3.1-tts-flash', 'label': 'Qwen-Audio-3.1-TTS-Flash（❌实测411不可用·换qwen3/cosyvoice）'},
                {'id': 'qwen-audio-3.0-tts-plus', 'label': 'Qwen-Audio-3.0-TTS-Plus（❌实测411不可用·换qwen3/cosyvoice）'},
                {'id': 'cosyvoice-v3.5-flash', 'label': 'CosyVoice-V3.5-Flash（专用口·仅复刻/设计音色）'},
                {'id': 'cosyvoice-v3-plus', 'label': 'CosyVoice-V3-Plus（专用口）'},
                {'id': 'cosyvoice-v2', 'label': 'CosyVoice-V2（专用口·老稳定）'},
                {'id': 'MiniMax/speech-2.8-hd', 'label': 'MiniMax-Speech-2.8-HD（multimodal口·高音质）'},
                {'id': 'MiniMax/speech-2.8-turbo', 'label': 'MiniMax-Speech-2.8-Turbo（multimodal口·快）'},
                {'id': 'qwen-audio-3.1-tts-next', 'label': 'Qwen-Audio-3.1-TTS-Next（❌实测411不可用·换qwen3/cosyvoice）'},
            ],
            # 按模型系的音色清单（音色绑定模型，跨系=Engine411；前端音色下拉/校验用）
            'voices': {
                'qwen3': ['Cherry', 'Serena', 'Ethan', 'Maia', 'Chelsie', 'Momo', 'Kai', 'Nofish',
                          'Bella', 'Jennifer', 'Ryan', 'Moon', 'Mia', 'Vincent', 'Neil', 'Andre'],
                'qwen-audio': ['longanyang', 'longanhuan_v3.6', 'longanlingxin', 'longanlufeng',
                               'xunanchuan_v3.1', 'loongeva', 'loongjohn', 'loongmary',
                               'longjielidou_v3.6', 'longpaopao_v3.6', 'longhuohuo_v3.6', 'longchuanshu_v3.6'],
                'minimax': ['male-qn-qingse'],
            },
            'baseUrlNote': '默认百炼；用千问AI平台时在高级里填 dashscopeBaseUrl=https://maas.qianwenaiapi.com',
            'auth': 'key', 'keyHint': '阿里云百炼/千问AI平台 API Key', 'keyName': 'dashscopeApiKey',
            'docs': 'https://help.aliyun.com/zh/model-studio/',
            'note': '自动路由：qwen-audio/cosyvoice 系（含 tts-next）WS 实时优先·HTTP 回落；qwen3/MiniMax 走 multimodal 口；⟳拉取只列已开通模型，内置表是全景', 'features': [],
        },
        'volc': {
            'id': 'volc', 'label': '火山方舟豆包 TTS', 'logo': '🔥',
            'baseUrl': 'https://ark.cn-beijing.volces.com/api/v3', 'canPull': True,
            'builtin': [{'id': 'seed-TTS-2.0', 'label': 'Seed-TTS-2.0（对话式·副语言）'}],
            'auth': 'key', 'keyHint': '火山方舟 Ark API Key', 'keyName': 'volcApiKey',
            'docs': 'https://docs.volcengine.com/docs/DoubaoVoice/',
            'note': 'OpenAI 兼容口走通用合成引擎；Seed 系列或需 openspeech 双鉴权（接口预留）', 'features': [],
        },
        'siliconflow': {
            'id': 'siliconflow', 'label': 'SiliconFlow（聚合）', 'logo': 'S',
            'baseUrl': 'https://api.siliconflow.cn/v1', 'canPull': True,
            'builtin': [{'id': 'FunAudioLLM/CosyVoice2-0.5B', 'label': 'CosyVoice2-0.5B（低价）'}],
            'auth': 'key', 'keyHint': 'SiliconFlow Key', 'keyName': 'siliconflowApiKey',
            'docs': 'https://siliconflow.cn/', 'note': '实验：OpenAI 兼容音频口，未实测', 'features': [],
        },
        'custom': {
            'id': 'custom', 'label': '自定义合成端点', 'logo': '⚙️',
            'baseUrl': '', 'canPull': True, 'builtin': [],
            'auth': 'key', 'keyHint': '', 'keyName': 'customSpeechApiKey',
            'docs': '', 'note': '', 'features': [],
        },
    },
}


# ================= 用户覆盖解析（兼容两代存法） =================
def _user_override(cfg, category, pid):
    """用户覆盖：新=customProviders[cat][pid]，旧=speechProviders[pid]（baseUrl/apiKey/model）。"""
    uc = {}
    try:
        uc = (cfg.get('customProviders') or {}).get(category, {}).get(pid) or {}
    except Exception:
        uc = {}
    legacy = (cfg.get('speechProviders') or {}).get(pid) or {}
    return {
        'baseUrl': uc.get('baseUrl') or legacy.get('baseUrl') or '',
        'apiKey': uc.get('apiKey') or legacy.get('apiKey') or '',
        'model': uc.get('model') or legacy.get('model') or '',
    }


def _key_of(cfg, pdef, pid):
    """按注册表 keyName 取存量 key（mimo/dashscope 有专用键；llm 统一 llmApiKey）。"""
    kn = pdef.get('keyName') or ''
    if kn and cfg.get(kn):
        return cfg[kn]
    # 兜底映射
    if pid == 'mimo':
        return cfg.get('mimoApiKey') or ''
    if pid == 'dashscope':
        return cfg.get('dashscopeApiKey') or cfg.get('llmApiKey') or ''
    return cfg.get('llmApiKey') or ''


def resolve_llm_endpoint(cfg, pid):
    pdef = PROVIDERS['llm'].get(pid) or PROVIDERS['llm']['custom']
    uc = _user_override(cfg, 'llm', pid)
    return {
        'provider': pid,
        'baseUrl': uc['baseUrl'] or pdef.get('baseUrl', ''),
        'apiKey': uc['apiKey'] or _key_of(cfg, pdef, pid),
        'model': uc['model'] or (cfg.get('llmModel') if pid == 'custom' else '') or '',
    }


def resolve_tts_endpoint(cfg, provider_id=None):
    """解析 TTS 端点。mimo 保持旧行为（mimoApiKey/mimoBaseUrl/mimoTtsModel）；
    其余：speechProviders/customProviders 覆盖 > 注册表预置。"""
    pid = provider_id or cfg.get('ttsProvider') or 'mimo'
    if pid == 'mimo':
        uc = _user_override(cfg, 'tts', pid)
        return {
            'provider': 'mimo',
            'baseUrl': uc['baseUrl'] or cfg.get('mimoBaseUrl') or 'https://api.xiaomimimo.com/v1',
            'apiKey': uc['apiKey'] or cfg.get('mimoApiKey') or '',
            'model': uc['model'] or cfg.get('mimoTtsModel') or 'mimo-v2.5-tts',
        }
    pdef = PROVIDERS['tts'].get(pid) or PROVIDERS['tts']['custom']
    uc = _user_override(cfg, 'tts', pid)
    return {
        'provider': pid,
        'baseUrl': uc['baseUrl'] or pdef.get('baseUrl', ''),
        'apiKey': uc['apiKey'] or _key_of(cfg, pdef, pid),
        'model': uc['model'] or (pdef.get('builtin') or [{}])[0].get('id', ''),
    }


def resolve_asr_endpoint(cfg, provider_id=None):
    """解析 ASR 端点。dashscope 走 SDK（baseUrl 空）；mimo 旧键。"""
    pid = provider_id or cfg.get('asrProvider') or 'dashscope'
    if pid == 'dashscope':
        uc = _user_override(cfg, 'asr', pid)
        return {
            'provider': 'dashscope', 'baseUrl': '',
            'apiKey': uc['apiKey'] or cfg.get('dashscopeApiKey') or '',
            'model': uc['model'] or cfg.get('asrModel') or 'paraformer-realtime-v2',
        }
    if pid == 'mimo':
        uc = _user_override(cfg, 'asr', pid)
        return {
            'provider': 'mimo',
            'baseUrl': uc['baseUrl'] or cfg.get('mimoBaseUrl') or 'https://api.xiaomimimo.com/v1',
            'apiKey': uc['apiKey'] or cfg.get('mimoApiKey') or '',
            'model': uc['model'] or cfg.get('mimoAsrModel') or 'mimo-v2.5-asr',
        }
    pdef = PROVIDERS['asr'].get(pid) or PROVIDERS['asr']['custom']
    uc = _user_override(cfg, 'asr', pid)
    return {
        'provider': pid,
        'baseUrl': uc['baseUrl'] or pdef.get('baseUrl', ''),
        'apiKey': uc['apiKey'] or _key_of(cfg, pdef, pid),
        'model': uc['model'] or (pdef.get('builtin') or [{}])[0].get('id', ''),
    }


# ================= 拉模型（OpenAI 兼容 GET /models） =================
def pull_models(base_url, api_key, timeout=15):
    """GET {base}/models；失败返回 None（前端回落内置清单）。"""
    if not (base_url or '').strip():
        return None
    url = urljoin(base_url.rstrip('/') + '/', 'models')
    req = Request(url, headers={'Authorization': f'Bearer {api_key or ""}'})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            models = sorted({str(m.get('id')) for m in (data.get('data') or []) if m.get('id')})
            return models or None
    except Exception:
        return None


# ================= 测速（最小真实调用） =================
def speed_test_llm(base_url, api_key, model):
    """LLM：POST chat.completions 一次小请求测总耗时 ms。"""
    if not (base_url and api_key and model):
        raise RuntimeError('参数不完整（baseUrl / key / 模型都要填）')
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30)
    t0 = time.perf_counter()
    client.chat.completions.create(model=model,
                                   messages=[{'role': 'user', 'content': '回复一个词：ok'}],
                                   max_tokens=5)
    return int((time.perf_counter() - t0) * 1000)


def _gen_test_wav(ffmpeg, seconds=1.0):
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
        wav = Path(tmp.name)
    c = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
                        '-ar', '16000', '-ac', '1', str(wav)],
                       capture_output=True, creationflags=_no_window())
    if c.returncode != 0 or not wav.exists():
        raise RuntimeError('ffmpeg 生成测试音频失败')
    return wav


def speed_test_asr(pid, base_url, api_key, model, ffmpeg=None):
    """ASR 测速：dashscope 真识别；其余（OpenAI 兼容口）测 /models 连通延迟。"""
    if pid == 'dashscope':
        if not api_key:
            raise RuntimeError('缺少 DashScope API Key')
        if not ffmpeg:
            raise RuntimeError('未找到 ffmpeg，无法生成测试音频')
        wav = _gen_test_wav(ffmpeg)
        try:
            import dashscope
            dashscope.api_key = api_key
            from dashscope.audio.asr import Recognition
            t0 = time.perf_counter()
            rec = Recognition(model=model or 'paraformer-realtime-v2', format='wav', sample_rate=16000)
            res = rec.call(file=str(wav))
            ms = int((time.perf_counter() - t0) * 1000)
            if getattr(res, 'status_code', 0) != 200:
                raise RuntimeError(f"识别失败：{str(getattr(res, 'message', ''))[:80]}")
            return ms, True
        finally:
            wav.unlink(missing_ok=True)
    # 其它：连通延迟
    if not base_url:
        raise RuntimeError('缺少 baseUrl')
    t0 = time.perf_counter()
    ok = pull_models(base_url, api_key, timeout=10) is not None
    ms = int((time.perf_counter() - t0) * 1000)
    if not ok:
        raise RuntimeError('无法连通 /models 端点')
    return ms, False


def speed_test_tts(pid, base_url, api_key, model, ffmpeg=None):
    """TTS 测速：edge 本地合成；mimo 真合成；dashscope 真合成；其余测连通。返回 (ms, is_real)。"""
    if pid == 'edge':
        import asyncio
        import tempfile
        from pathlib import Path
        import edge_tts
        with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
            out = Path(tmp.name)
        async def go():
            await edge_tts.Communicate('ok', 'en-US-AriaNeural').save(str(out))
        try:
            t0 = time.perf_counter()
            asyncio.run(go())
            return int((time.perf_counter() - t0) * 1000), True
        finally:
            out.unlink(missing_ok=True)
    if pid == 'mimo':
        if not (base_url and api_key):
            raise RuntimeError('缺少 MiMo Key / baseUrl')
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        t0 = time.perf_counter()
        c = client.chat.completions.create(
            model=model or 'mimo-v2.5-tts',
            messages=[{'role': 'user', 'content': 'A clear voice.'},
                      {'role': 'assistant', 'content': 'ok'}],
            audio={'format': 'wav', 'voice': 'Chloe'})
        ms = int((time.perf_counter() - t0) * 1000)
        if not getattr(c.choices[0].message, 'audio', None):
            raise RuntimeError('未返回音频')
        return ms, True
    if pid == 'dashscope':
        if not api_key:
            raise RuntimeError('缺少 DashScope API Key')
        import dashscope
        dashscope.api_key = api_key
        from dashscope.audio.tts_v2 import SpeechSynthesizer
        t0 = time.perf_counter()
        synth = SpeechSynthesizer(model=model or 'qwen-audio-3.1-tts-flash', voice='Cherry')
        res = synth.call(text='ok')
        ms = int((time.perf_counter() - t0) * 1000)
        audio = res.get_audio_data() if hasattr(res, 'get_audio_data') else None
        if not audio and not getattr(res, 'status_code', 200) == 200:
            raise RuntimeError('合成失败（实验接口，口径见错误日志）')
        return ms, True
    # doubao / custom / siliconflow：连通延迟
    if not base_url:
        raise RuntimeError('缺少 baseUrl')
    t0 = time.perf_counter()
    ok = pull_models(base_url, api_key, timeout=10) is not None
    ms = int((time.perf_counter() - t0) * 1000)
    if not ok:
        raise RuntimeError('无法连通 /models 端点')
    return ms, False


# ================= 报错翻译层：错误码后追加人话提示 =================
def err_hint(model, msg, kind='tts'):
    """把服务端报错翻成人话，追加在原文后（前端红字渲染）。
    kind='tts'|'asr'，只换措辞不换逻辑。"""
    m = str(msg or '')
    low = (str(model or '') + ' ' + m).lower()
    hints = []
    if 'cosyvoice' in low or 'engine error' in low or '[411]' in m:
        hints.append('该模型/音色在合成口被拒（qwen-audio 系 TTS 实测本账号 411 全不可用）——换 qwen3-tts-flash（HTTP 稳定）或 cosyvoice-v3-flash（WS 稳定）')
    if 'url error' in low or 'please check url' in low:
        hints.append('该模型可能不在此接口上（qwen-audio-3.1 系已走 WS 实时通道并自动回落 HTTP；仍报错多为账号未开通该模型）'
                     '——下拉默认的 qwen3-tts-flash 是已验证稳定款')
    if 'websocket' in low or 'ws 通道' in m or 'handshake' in low:
        hints.append('WS 实时通道连接失败——检查网络/代理是否放行 wss，或稍后重试（非 next 模型已自动回落 HTTP 口）')
    if 'compatible-mode' in low or 'chat/completions' in low:
        hints.append('OpenAI 兼容口报错——确认 Key 有百炼权限、模型名用 qwen3-asr-flash（不带 -realtime/-filetrans 后缀）')
    if 'invalidparameter' in low.replace(' ', '') or 'invalid model' in low \
       or 'model not exist' in low or 'arithmetic' in low and 'model' in low or ('not found' in low and 'model' in low):
        if kind == 'tts':
            hints.append('模型名不对或你的账号未开通该模型——检查拼写或换一个（下拉里的都是可用系）')
        else:
            hints.append('模型名不对或账号未开通——换 paraformer-realtime-v2 最稳')
    if 'unauthorized' in low or '401' in m or 'api key' in low or 'authentication' in low or 'access' in low and 'denied' in low:
        hints.append('API Key 无效或没填——设置里重新保存一次')
    if '429' in m or 'rate limit' in low or 'throttl' in low:
        hints.append('触发限流——稍几秒重试，或把并行路数调小')
    if 'timeout' in low or 'timed out' in low:
        hints.append('网络超时——重试一次；走代理就查节点')
    if kind == 'asr' and ('empty' in low or '为空' in m):
        hints.append('识别结果为空——音频太短/太安静属正常')
    return (m + '｜' + '；'.join(hints)) if hints else m
