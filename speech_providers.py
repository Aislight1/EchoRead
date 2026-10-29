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

# MiMo V2.5 全系于 2026-10-21 10:00 (UTC+8) 下线，旧模型名再调用会直接报错。
# 这些名字保留下来只用于「识别旧配置并给出人话提示」，不再作为默认模型。
MIMO_RETIRED_MODELS = {
    'mimo-v2.5-tts',
    'mimo-v2.5-tts-voiceclone',
    'mimo-v2.5-tts-voicedesign',
    'mimo-v2.5-asr',
}
MIMO_RETIRED_NOTE = ('MiMo V2.5 全系已于 2026-10-21 下线——请在「设置 → 语音输出」'
                     '切换到阿里云 qwen3-tts-flash（HTTP 稳定）或 Edge TTS；'
                     '若 MiMo 已发布 v2.6-tts，可在模型名里手填后保存')


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
            'auth': 'key', 'keyHint': '千问 AI 平台 Key', 'keyName': 'llmApiKey',
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
            'builtin': [{'id': 'mimo-v2.5-asr',
                         'label': 'MiMo-ASR（V2.5 已下线·请改用 paraformer-realtime-v2）'}],
            'auth': 'key', 'keyHint': '小米 MiMo Key', 'keyName': 'mimoApiKey',
            'docs': 'https://mimo.xiaomimimo.com/', 'note': '⛔ V2.5 系已于 2026-10-21 下线；识别请用 paraformer-realtime-v2',
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
            'id': 'edge', 'label': 'Edge 神经音（无需 Key）', 'logo': '🅔',
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
            'docs': 'https://github.com/rany2/edge-tts', 'note': '单词朗读/兜底首选',
            'defaultModel': 'en-US-AriaNeural', 'features': [],
        },
        'mimo': {
            'id': 'mimo', 'label': '小米 MiMo TTS', 'logo': 'M',
            'baseUrl': 'https://api.xiaomimimo.com/v1', 'canPull': True,
            # 2026-10-21 V2.5 全系下线：空 builtin + 空 defaultModel，避免默认再选中死模型。
            # 旧配置里的 v2.5 名字由 MIMO_RETIRED_MODELS 识别并提示；v2.6-tts 发布后
            # 用户可直接在「当前引擎模型」里手填，不需要改代码。
            'defaultModel': '',
            'builtin': [],
            'hidden': [
                {'id': 'mimo-v2.5-tts', 'label': 'V2.5-TTS（已于 2026-10-21 下线）',
                 'use': '仅作旧配置识别；不要再选，请改用阿里云 Qwen3-TTS 或 Edge'},
            ],
            'aliases': {
                'mimo-v2.5-tts-voiceclone': 'mimo-v2.5-tts',
                'mimo-v2.5-tts-voicedesign': 'mimo-v2.5-tts',
            },
            'auth': 'key', 'keyHint': '小米 MiMo Key', 'keyName': 'mimoApiKey',
            'docs': 'https://mimo.xiaomimimo.com/docs/usage-guide/speech-synthesis-v2.5',
            'note': '⛔ V2.5 全系已于 2026-10-21 下线；如已发布 v2.6-tts，请在模型名手填 mimo-v2.6-tts 后保存',
            'features': [],
        },
        'dashscope': {
            'id': 'dashscope', 'label': '阿里云 Qwen TTS', 'logo': '☁️',
            'baseUrl': 'https://dashscope.aliyuncs.com/api/v1', 'canPull': True,
            'pullBaseUrl': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            # #8 模型目录收敛：下拉/芯片只列「每代最新」3 个。
            # 旧代与实测 411 不可用的模型全部折叠进 aliases（保留可解析）：
            # 老配置里的旧模型名在 resolve/synthesize 时自动映射到最新等价模型并打一条日志，
            # 不会被清空、也不会拿旧名去打请求报 411。
            'defaultModel': 'qwen3-tts-flash',
            'builtin': [
                {'id': 'qwen3-tts-flash', 'label': 'Qwen3-TTS-Flash（最新·HTTP 稳定）',
                 'use': '英文跟读 / 单词 / 短文首选：16 个系统音色，非流式 HTTP 口最稳'},
                {'id': 'cosyvoice-v3-flash', 'label': 'CosyVoice-V3-Flash（最新·专用口）',
                 'use': '要用 CosyVoice 音色库（longan 系 12 音色）或要更低延迟时用'},
                {'id': 'MiniMax/speech-2.8-hd', 'label': 'MiniMax-Speech-2.8-HD（最新·高音质）',
                 'use': '影视级音质与情绪表现，单价最高，只有 1 个系统音色'},
            ],
            # 仍可合成、但不再出现在下拉里的（同代快速档）
            'hidden': [
                {'id': 'MiniMax/speech-2.8-turbo', 'label': 'MiniMax-Speech-2.8-Turbo（同代快速档·隐藏）',
                 'use': '同代 2.8 的快速便宜档，功能正常，只是不占下拉位'},
            ],
            # 旧代 / 实测不可用 → 最新等价模型
            'aliases': {
                'qwen-audio-3.0-tts-flash': 'qwen3-tts-flash',
                'qwen-audio-3.0-tts-plus': 'qwen3-tts-flash',
                'qwen-audio-3.1-tts-flash': 'qwen3-tts-flash',
                'qwen-audio-3.1-tts-next': 'qwen3-tts-flash',
                'cosyvoice-v3-plus': 'cosyvoice-v3-flash',
                'cosyvoice-v2': 'cosyvoice-v3-flash',
                'cosyvoice-v3.5-flash': 'cosyvoice-v3-flash',
            },
            # 按模型系的音色清单（音色绑定模型，跨系=Engine411；前端音色下拉/校验用）
            'voices': {
                'qwen3': ['Cherry', 'Serena', 'Ethan', 'Maia', 'Chelsie', 'Momo', 'Kai', 'Nofish',
                          'Bella', 'Jennifer', 'Ryan', 'Moon', 'Mia', 'Vincent', 'Neil', 'Andre'],
                'qwen-audio': ['longanyang', 'longanhuan_v3.6', 'longanlingxin', 'longanlufeng',
                               'xunanchuan_v3.1', 'loongeva', 'loongjohn', 'loongmary',
                               'longjielidou_v3.6', 'longpaopao_v3.6', 'longhuohuo_v3.6', 'longchuanshu_v3.6'],
                'minimax': ['male-qn-qingse'],
            },
            'voiceNotes': {
                'qwen3': 'Qwen3 系 16 个系统音色（中英通用：Cherry/Serena/Ethan/Maia…）',
                'qwen-audio': 'CosyVoice / qwen-audio 系 longan 12 音色（中文为主）',
                'minimax': 'MiniMax 系只有 1 个系统音色（male-qn-qingse）',
            },
            'baseUrlNote': '默认百炼；用千问AI平台时在高级里填 dashscopeBaseUrl=https://maas.qianwenaiapi.com',
            'auth': 'key', 'keyHint': '阿里云百炼/千问AI平台 API Key', 'keyName': 'dashscopeApiKey',
            'docs': 'https://help.aliyun.com/zh/model-studio/',
            'note': '自动路由：qwen3/MiniMax 走 multimodal 口，cosyvoice/qwen-audio 系 WS 实时优先·HTTP 回落；'
                    '目录只列最新一代（3 个），旧模型名自动映射',
            'features': [],
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


# ================= TTS 模型/音色目录（#8：每代只留最新 + 旧名自动映射） =================
# 场景 id 的单一来源。前端「场景级引擎覆盖」下拉的键必须和真正调用
# tts_speak(scene=…) 的地方一致，否则覆盖永远不生效——历史 bug：UI 列的是
# listen/play，唯一传 scene 的调用处用的是 practice（见 app.tts_speak_batch）。
TTS_SCENES = [
    {'id': 'listen', 'name': '听力朗读', 'desc': '听力页「听原声 / 听 AI 朗读」'},
    {'id': 'play', 'name': '跟读播放', 'desc': '跟读页参考音播放'},
    {'id': 'practice', 'name': '练习批处理', 'desc': '逐句练习批量合成（tts_speak_batch）'},
]


def _tts_pdef(pid):
    """TTS 供应商定义（未知 pid 落 custom，避免 KeyError 冒到界面上）。"""
    tts = PROVIDERS.get('tts') or {}
    return tts.get(pid) or tts.get('custom') or {}


# 「⟳ 拉模型」结果里，算得上语音合成模型的型号关键字（其余是 chat/asr/embedding/rerank）
_TTS_MODEL_HINTS = ('tts', 'cosyvoice', 'speech-', 'seed-tts', 'mimo', 'sambert')


def tts_default_model(pid):
    """该供应商「应该默认用」的模型：显式 defaultModel > 内置清单第一个。"""
    p = _tts_pdef(pid)
    m = (p.get('defaultModel') or '').strip()
    if m:
        return m
    return next((b.get('id') for b in (p.get('builtin') or []) if b.get('id')), '')


def tts_family(pid, model):
    """模型属于哪个音色系（= voices 字典的键）。跨系传音色会被拒 Engine411。"""
    m = (model or '').lower()
    if pid == 'edge':
        return 'edge'
    if pid == 'dashscope':
        if m.startswith('minimax') or '/' in m:
            return 'minimax'
        if 'cosyvoice' in m or 'qwen-audio' in m:
            return 'qwen-audio'
        return 'qwen3'
    return pid


def tts_models(pid, include_hidden=False):
    """该供应商的 TTS 模型目录（最新的在前）。每项带一句话用途 use 与 hidden/latest 标记。"""
    p = _tts_pdef(pid)
    out = []
    for b in (p.get('builtin') or []):
        if not b.get('id'):
            continue
        out.append({'id': b['id'], 'label': b.get('label') or b['id'],
                    'use': b.get('use') or '', 'hidden': False,
                    'latest': b['id'] == (p.get('defaultModel') or ''),
                    'family': tts_family(pid, b['id'])})
    if include_hidden:
        for b in (p.get('hidden') or []):
            if not b.get('id'):
                continue
            out.append({'id': b['id'], 'label': b.get('label') or b['id'],
                        'use': b.get('use') or '', 'hidden': True, 'latest': False,
                        'family': tts_family(pid, b['id'])})
    return out


def tts_canonical_model(pid, model):
    """旧模型名 → 最新等价模型。返回 (model, note)。

    note 非空表示发生了映射，调用方应当打一条日志（老配置静默换模型最难排查）。
    """
    m = (model or '').strip()
    if not m:
        return tts_default_model(pid), ''
    # MiMo V2.5：名字仍可解析，但不再映射到另一个死模型；返回空模型让上层给出
    # 「已下线，请切换引擎」的明确错误，而不是拿旧名去打请求收 404/模型不存在。
    if str(pid).lower() == 'mimo' and m.lower() in {x.lower() for x in MIMO_RETIRED_MODELS}:
        return '', MIMO_RETIRED_NOTE
    al = (_tts_pdef(pid).get('aliases') or {})
    if m in al:
        return al[m], f'模型「{m}」是旧代/不可用，已自动映射到最新等价模型「{al[m]}」'
    low = {str(k).lower(): v for k, v in al.items()}
    if m.lower() in low:                       # 大小写/空格兜底
        v = low[m.lower()]
        return v, f'模型「{m}」是旧代/不可用，已自动映射到最新等价模型「{v}」'
    return m, ''


def tts_voices(pid, model=None, with_default=True):
    """该模型系的音色清单。with_default=True 时第一项是「默认/留空」。

    · edge 没有模型概念：直接把内置神经音当音色；
    · dashscope 按模型系返回（跨系会 411，所以只列本系）；
    · mimo/volc/siliconflow/custom 没有固定音色目录 → 返回 []，由 TTS 层用风格预设兜底。
    """
    if pid == 'edge':
        items = [{'id': b['id'], 'label': b.get('label') or b['id'], 'family': 'edge',
                  'group': 'Edge 神经音'} for b in (_tts_pdef('edge').get('builtin') or [])]
        if with_default:
            items.insert(0, {'id': '', 'label': '默认（Aria 美音女）',
                             'family': 'edge', 'group': '默认'})
        return items
    p = _tts_pdef(pid)
    fam = tts_family(pid, model or tts_default_model(pid))
    raw = (p.get('voices') or {}).get(fam) or []
    if not raw:
        return []
    note = (p.get('voiceNotes') or {}).get(fam) or ''
    items = [{'id': v, 'label': v, 'family': fam, 'group': f'{fam} 系', 'desc': note}
             for v in raw]
    if with_default:
        items.insert(0, {'id': '', 'label': f'默认（{raw[0]}）', 'family': fam,
                         'group': '默认', 'desc': note})
    return items


def tts_pick_voice(pid, model, voice):
    """把音色收敛到「属于当前模型系」的合法值。返回 (voice, note)。

    音色绑定模型系：拿 cosyvoice 模型配 Cherry、或拿 qwen3 模型配 longanyang 都会
    Engine411。这里统一回退到该系默认音色，并把原因写进 note 让调用方打日志
    （静默换音色就是「试听听起来还是上一个音色」的观感来源之一）。
    """
    fam = tts_family(pid, model)
    raw = (_tts_pdef(pid).get('voices') or {}).get(fam) or []
    if not raw:                                 # 没有固定音色目录的供应商：原样放行
        return (voice or '').strip(), ''
    v = (voice or '').strip()
    if v in raw:
        return v, ''
    if v:
        return raw[0], (f'音色「{v}」不属于 {model} 的 {fam} 系（跨系会报 Engine411），'
                        f'已回退到该系默认音色「{raw[0]}」')
    return raw[0], f'没选音色，用 {fam} 系默认音色「{raw[0]}」'


def filter_pulled_models(pid, models):
    """「⟳ 拉模型」的结果过滤：语音输出的模型下拉只该看见能合成的东西。

    · 拉回来的列表是 OpenAI 兼容口 /models，会把 chat/asr/embedding/rerank/realtime
      全混进来（前端曾把整份拉取结果直接灌进 datalist，选到 asr 模型必然报错）；
    · 只留 tts 系关键字；注册表里有内置目录的供应商（dashscope/mimo/volc/siliconflow），
      以内置最新一代为准——网关还返回旧代也不列（#8 的约束在拉取路径上同样成立）。
    返回 (models, note)。
    """
    raw = [str(m).strip() for m in (models or []) if str(m).strip()]
    keep = [m for m in raw if any(k in m.lower() for k in _TTS_MODEL_HINTS)]
    p = _tts_pdef(pid)
    curated = [b.get('id') for b in (p.get('builtin') or []) if b.get('id')]
    if curated:
        merged = list(curated)
        shown = set(curated) | {x['id'] for x in tts_models(pid, include_hidden=True)}
        for m in keep:
            if m not in merged and tts_canonical_model(pid, m)[0] not in shown:
                merged.append(m)          # 网关新冒出来的型号才补进来（老型号被 aliases 拦掉）
        note = f'已按内置最新目录收敛为 {len(merged)} 个（排除了 {len(raw) - len(keep)} 个非语音模型）'
        return merged, note
    if keep:
        return keep, f'已过滤掉 {len(raw) - len(keep)} 个非语音模型'
    return raw, '网关没返回可识别的语音模型，保留原列表'


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
    其余：speechProviders/customProviders 覆盖 > 注册表预置。
    model 一定是「最新一代」：旧名经 aliases 自动映射（#8），绝不返回空串或已下线模型。"""
    pid = provider_id or cfg.get('ttsProvider') or 'mimo'
    if pid == 'mimo':
        uc = _user_override(cfg, 'tts', pid)
        model, _n = tts_canonical_model('mimo', uc['model'] or cfg.get('mimoTtsModel') or '')
        return {
            'provider': 'mimo',
            'baseUrl': uc['baseUrl'] or cfg.get('mimoBaseUrl') or 'https://api.xiaomimimo.com/v1',
            'apiKey': uc['apiKey'] or cfg.get('mimoApiKey') or '',
            'model': model,
        }
    pdef = PROVIDERS['tts'].get(pid) or PROVIDERS['tts']['custom']
    uc = _user_override(cfg, 'tts', pid)
    model, _n = tts_canonical_model(pid, uc['model'] or tts_default_model(pid))
    return {
        'provider': pid,
        'baseUrl': uc['baseUrl'] or pdef.get('baseUrl', ''),
        'apiKey': uc['apiKey'] or _key_of(cfg, pdef, pid),
        'model': model,
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
        # 测速也要走「最新一代 + 同系音色」：旧实现硬编码 qwen-audio-3.1-tts-flash
        # （已被 aliases 折叠、且配 Cherry 必 411），测出来的永远是失败。
        _m, _n = tts_canonical_model('dashscope', (model or '').strip() or tts_default_model('dashscope'))
        _v, _note = tts_pick_voice('dashscope', _m, '')
        if tts_family('dashscope', _m) != 'qwen-audio':
            # qwen3 / MiniMax 系只在 multimodal HTTP 口上（WS 的 tts_v2 SDK 不认它们），
            # 用同一份请求体口径探一次真合成。
            import json as _json
            from urllib.request import Request, urlopen
            host = (base_url or 'https://dashscope.aliyuncs.com').rstrip('/')
            host = host[:-len('/api/v1')] if host.endswith('/api/v1') else host
            url = host + '/api/v1/services/aigc/multimodal-generation/generation'
            if tts_family('dashscope', _m) == 'minimax':
                inp = {'text': 'ok', 'voice_setting': {'voice_id': _v}}
            else:
                inp = {'text': 'ok', 'voice': _v, 'language_type': 'English'}
            body = _json.dumps({'model': _m, 'input': inp}).encode('utf-8')
            req = Request(url, data=body, method='POST',
                          headers={'Authorization': f'Bearer {api_key}',
                                   'Content-Type': 'application/json'})
            t0 = time.perf_counter()
            with urlopen(req, timeout=60) as resp:
                raw = resp.read()
            ms = int((time.perf_counter() - t0) * 1000)
            if not raw:
                raise RuntimeError('未返回音频')
            return ms, True
        import dashscope
        dashscope.api_key = api_key
        from dashscope.audio.tts_v2 import SpeechSynthesizer
        t0 = time.perf_counter()
        synth = SpeechSynthesizer(model=_m, voice=_v)
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
    if 'mimo' in low and ('v2.5' in low or 'not found' in low
                          or 'does not exist' in low or 'invalid' in low):
        hints.append(MIMO_RETIRED_NOTE)
    if 'cosyvoice' in low or 'engine error' in low or '[411]' in m:
        hints.append('该模型/音色在合成口被拒（Engine411 = 音色不属于该模型系）——'
                     '换 qwen3-tts-flash（HTTP 稳定）或 cosyvoice-v3-flash（WS 稳定）；'
                     '音色会自动收敛到所选模型的那一系')
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
