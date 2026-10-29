#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
听与读 · 桌面版

pywebview + WebView2 原生窗口。
语料管理、下载、跟读播放器全部本地完成；
发音练习用到三个外部能力（可在设置里各自配置，也都能留空不用）：
    · 语音合成 阿里云 Qwen3-TTS（默认）/ Edge TTS（无需 Key）/ MiMo（V2.5 已下线）
    · 语音识别 阿里云 DashScope paraformer（返回逐词时间戳）
    · 对话纠错 任意 OpenAI 兼容 LLM
"""
import hashlib
import json
import math
import re
import sys
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import tedlibrarian as tl  # noqa: E402
import videosrc            # noqa: E402
import videokind           # noqa: E402  ⑤ 来源类型（演讲/对话/读书/播客）
import plan as planmod     # noqa: E402
import localfs             # noqa: E402
import speech_tts          # noqa: E402
import speech_asr          # noqa: E402
import analysis            # noqa: E402
import listening           # noqa: E402
import speech_engine       # noqa: E402
import store               # noqa: E402
import prompts             # noqa: E402
import difficulty as diffmod  # noqa: E402

try:
    import webview
except ImportError:
    print('缺少 pywebview，请先：pip install pywebview')
    sys.exit(1) 


def _ui_dir():
    import sys as _s
    base = getattr(_s, '_MEIPASS', None)
    if base:
        p = Path(base) / 'ui'
        if p.exists():
            return p
    return APP_DIR / 'ui'


def _audio_sib(audio_path, suffix):
    """音频的同名兄弟文件（X.mp3 → X + suffix，如 .en.vtt / .zh.vtt）。

    必须字符串拼接：标题里含点时（B站合集常见 "01.[20190101]…"），
    Path.with_suffix 会把最后一个点之后的整段标题当"后缀"替换掉，
    拼出 …p01 01.en.vtt 这种不存在的路径——跟读/诊断误报"没有字幕"的根因。
    """
    p = Path(audio_path)
    return p.parent / (p.stem + suffix)


def _llm_json_cached(cache_dir, pv, model_name, sys_p, user, stat=None, llm=None):
    """LLM 批次结果的磁盘缓存（P3/H-04）。

    键 = 提示词版本 + 模型名 + system + user（整批句子都在 user 里），
    同一批文本没变就秒回，不再烧一次模型调用。失败/非 dict 不写缓存。
    stat 传 dict 时累加 {'hit','miss'}，供调用方报告命中批数。
    llm 必须由调用方注入（客户端是 doctor run() 里的局部变量，不是模块级名字）。
    """
    key = hashlib.sha1(
        f'{pv}|{model_name or ""}|{sys_p}|{user}'.encode('utf-8')
    ).hexdigest()[:24]
    d = Path(cache_dir) / 'llm'
    p = d / f'c_{key}.json'
    try:
        if p.exists():
            obj = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(obj, dict) and obj.get('v') == pv:
                if stat is not None:
                    stat['hit'] = stat.get('hit', 0) + 1
                return obj.get('data')
    except Exception:
        pass
    if llm is None:
        raise RuntimeError('LLM 客户端未就绪')
    data = llm.chat_json(sys_p, user, max_tokens=4000, tag='字幕诊断')
    try:
        if isinstance(data, dict):
            d.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps({'v': pv, 'data': data}, ensure_ascii=False),
                           encoding='utf-8')
            tmp.replace(p)
            fs = sorted(d.glob('c_*.json'), key=lambda x: x.stat().st_mtime)
            if len(fs) > 800:                  # 上限：超 800 条清最旧 300 条
                for f in fs[:300]:
                    f.unlink(missing_ok=True)
    except Exception:
        pass
    if stat is not None:
        stat['miss'] = stat.get('miss', 0) + 1
    return data


def _find_browser(name):
    """找浏览器可执行文件：注册表 App Paths → 常见安装位置 → PATH。"""
    import os
    try:
        import winreg
        key_name = {'edge': 'msedge.exe', 'chrome': 'chrome.exe',
                    'waterfox': 'waterfox.exe'}.get(name)
        if key_name:
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k = winreg.OpenKey(
                        root, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths'
                              + '\\' + key_name)
                    v = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    if v and os.path.exists(v):
                        return v
                except OSError:
                    continue
    except Exception:
        pass
    exe = {'edge': 'msedge.exe', 'chrome': 'chrome.exe',
           'waterfox': 'waterfox.exe'}.get(name, '')
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    pf86 = os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')
    lap = os.environ.get('LOCALAPPDATA', '')
    cands = {
        'edge': [rf'{pf86}\Microsoft\Edge\Application\msedge.exe',
                 rf'{pf}\Microsoft\Edge\Application\msedge.exe'],
        'chrome': [rf'{pf}\Google\Chrome\Application\chrome.exe',
                   rf'{pf86}\Google\Chrome\Application\chrome.exe',
                   rf'{lap}\Google\Chrome\Application\chrome.exe'],
        'waterfox': [rf'{pf}\Waterfox\waterfox.exe',
                     rf'{pf86}\Waterfox\waterfox.exe'],
    }.get(name, [])
    for c in cands:
        if os.path.exists(c):
            return c
    import shutil
    return shutil.which(exe or name)


def _open_with_browser(u, b):
    """用指定浏览器打开 URL。返回 (成功?, 实际方式)。"""
    if b in ('edge', 'chrome', 'waterfox'):
        exe = _find_browser(b)
        if exe:
            subprocess.Popen([exe, u],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return True, b
        return False, ''
    import webbrowser
    webbrowser.open(u)
    return True, 'default'


# 每日提示（本地金句库，零 token；按日期哈希固定，可手动换一条）
DAILY_TIPS = [
    'Slow is smooth, smooth is fast. —— 慢就是稳，稳就是快。读准比读快重要。',
    'You don\'t learn to swim by reading about swimming. —— 开口说，才是学语言的方式。',
    'Mistakes are proof that you are trying. —— 错误证明你在尝试。',
    'Practice makes permanent. —— 练习不会造就完美，只会造就永久——所以要用对的方法练。',
    'Listen first, speak second. —— 先听够，再说准。耳朵领先嘴巴。',
    'One sentence a day keeps forgetting away. —— 每天吃透一句，胜过泛读十篇。',
    'Shadowing builds rhythm. —— 跟读练的是节奏，不是单词。',
    'If you can say it, you own it. —— 能说出口的，才真正是你的。',
    'Little by little, a little becomes a lot. —— 每天一点点，累积就是很多。',
    'Don\'t practice until you get it right; practice until you can\'t get it wrong. —— 练到不会错，而不只是这一次对。',
    'A different language is a different vision of life. —— 另一门语言，是看世界的另一双眼睛。',
    'Record yourself. Your ears are your best teacher. —— 录下自己的声音，耳朵是最好的老师。',
    'Speak it out loud. Silent reading builds readers, not speakers. —— 默读造就这样读者，朗读才造就说话的人。',
    'Repetition is the mother of skill. —— 重复是技能之母。',
    'Focus on the music, not just the notes. —— 先抓语调的旋律，再抠单词的音符。',
    'Every expert was once a beginner. —— 每个专家都曾是新手。',
    'The limit of my language means the limit of my world. —— 语言的边界，就是世界的边界。',
    'Learning is a treasure that will follow its owner everywhere. —— 学到的东西，走到哪跟到哪。',
    'Clarity beats speed every time. —— 清晰永远胜过语速。',
    'Today\'s practice is tomorrow\'s fluency. —— 今天练的每句，都是明天的流利。',
    'Imitate first, innovate later. —— 先模仿到位，再谈自己的风格。',
    'A little progress each day adds up to big results. —— 每天一小步，结果一大步。',
    'The expert in anything was once a disaster. —— 任何领域的专家，都曾搞砸过。',
    'Consistency beats intensity. —— 稳定的坚持胜过猛冲猛打。',
    'Train your ear with movies you love. —— 用你爱的电影磨耳朵，兴趣就是续航。',
    'Read it. Say it. Use it. Own it. —— 读过、说过、用过，才是学会。',
    'Fluency is accuracy over time. —— 流利不是快，是长时间都准。',
    'Don\'t fear the pause; fear the rush. —— 不怕停顿，怕抢拍。',
    'Small daily improvements are the key to staggering long-term results. —— 每天小进步，长期大飞跃。',
    'Your accent is your story. Your clarity is your skill. —— 口音是你的故事，清晰是你的本事。',
    'Review beats novelty. —— 复习旧词胜过贪学新词。',
    'Speak before you are ready. —— 在「准备好」之前就开口。',
    'The alphabet of pain will spell your growth. —— 刻意练习的酸痛，拼出的是成长。',
    'Listen to one sentence until you can hear every word. —— 一句话听到每个词都清晰为止。',
    'Make it fun, make it stick. —— 有趣的才能记住。',
    'Practice the way you will perform. —— 像实战一样练习。',
    'Words are free. It\'s how you use them that may cost you. —— 单词不花钱，用对才值钱。',
    'Better one done than ten begun. —— 吃透一句，胜过开头十句。',
    'Energy and persistence conquer all things. —— 精力和坚持能征服一切。',
    'You are what you repeatedly do. —— 你反复做的事，就是你成为的人。',
]


class Api:
    def __init__(self, server=None):
        self.cfg = tl.load_config()
        self.server = server
        self._dl = {'running': False, 'log': [], 'done': False, 'error': '', 'result': None}
        self._lock = threading.Lock()
        self._migrate_gsv_config()
        self._migrate_retired_tts()
        # 主线程预热本地对齐引擎：torch 的 OpenMP 初始化必须发生在主线程，
        # doctor 后台线程里首次 import 会段错误（没装 torch 静默跳过，<10ms）
        try:
            import local_align as _la
            _la.prewarm()
        except Exception:
            pass

        # ---- 练习相关 ----
        self.cache_dir = tl.APP_DIR / 'cache'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = tl.APP_DIR / 'practice.json'
        self.vocab_file = tl.APP_DIR / 'vocab.json'
        self.voice_file = tl.APP_DIR / 'voices.json'
        self.vocab = analysis.Vocab(self.vocab_file)
        self._rec = speech_asr.Recorder(log=self._plog)
        self._plogs = []
        self._clean_legacy_voices()
        self._recording = False
        # ---- 统一语音引擎 + 单词词典缓存 ----
        self.app_dir = tl.APP_DIR
        self.plan = planmod.Plan(self.app_dir / 'plan.json', self.log_file)
        self._fav_file = self.app_dir / 'sentence_favorites.json'
        self._phon_file = self.app_dir / 'phoneme_progress.json'
        self._engine_obj = None
        self._defcache = None
        self._llm = None

    # ---------------- 配置读写（设置页用）
    def settings(self):
        cfg = tl.load_config()
        bi = self.app_build()
        return {
            'ok': True,
            'buildStamp': bi.get('stamp') or '',
            'buildAt': bi.get('builtAt') or '',
            'libraryDir': cfg.get('libraryDir', ''),
            'extraDirs': cfg.get('extraDirs', []),
            'openAfter': bool(cfg.get('openFolderAfterDownload')),
            'ttsProvider': cfg.get('ttsProvider') or 'dashscope',
            'mimoApiKey': cfg.get('mimoApiKey') or '',
            'mimoBaseUrl': cfg.get('mimoBaseUrl') or 'https://api.xiaomimimo.com/v1',
            # MiMo V2.5 已于 2026-10-21 下线：不再默认返回死模型名，空就是空（前端提示手填 v2.6）
            'mimoTtsModel': cfg.get('mimoTtsModel') or '',
            'mimoVoice': cfg.get('mimoVoice') or 'Chloe',
            'dashscopeApiKey': cfg.get('dashscopeApiKey') or '',
            'asrModel': cfg.get('asrModel') or 'paraformer-realtime-v2',
            # ⑥：白名单里能存 asrMode、但读回来时漏了这一项 → 前端每次进设置页
            # 拿到 undefined 就回落成 traditional 并原样写回，用户切「分片并行」
            # 永远存不住（这就是"改了没生效"的一半原因）。
            'asrMode': cfg.get('asrMode') or 'traditional',
            'asrProvider': cfg.get('asrProvider') or 'dashscope',
            'audioModel': cfg.get('audioModel') or '',
            'micDevice': cfg.get('micDevice'),          # None/未设＝跟系统默认；数字＝锁定的输入设备（#7）
            'mimoAsrModel': cfg.get('mimoAsrModel') or '',
            'llmBaseUrl': cfg.get('llmBaseUrl') or '',
            'llmApiKey': cfg.get('llmApiKey') or '',
            'llmModel': cfg.get('llmModel') or '',
            'ttsPreset': cfg.get('ttsPreset') or 'narrator',
            'downloadProxy': cfg.get('downloadProxy') or '',
            'smartDownload': cfg.get('smartDownload', True),
            'lastUrl': cfg.get('lastUrl') or '',
            'uiTheme': cfg.get('uiTheme') or 'forest',
            'uiMotion': cfg.get('uiMotion') or 'standard',
            'pvMode': cfg.get('pvMode') or 'a',
            'llmProfiles': cfg.get('llmProfiles') or {'lookup': {'enabled': False}},
            'pricingOverrides': cfg.get('pricingOverrides') or [],
            'shortcuts': cfg.get('shortcuts') or {
                'tabsPrefix': 'alt', 'reviewFlip': '空格',
                'reviewGood': '1', 'reviewHard': '2', 'reviewAgain': '3',
            },
        }

    def save_settings(self, patch=None):
        try:
            patch = patch or {}
            cfg = tl.load_config()
            allowed = {
                'ttsProvider', 'mimoApiKey', 'mimoBaseUrl', 'mimoTtsModel', 'mimoVoice',
                'dashscopeApiKey', 'asrModel', 'llmBaseUrl', 'llmApiKey', 'llmModel',
                'ttsPreset', 'asrProvider', 'mimoAsrModel', 'asrMode', 'audioModel', 'asrAlign',
                'micDevice',                                # 录音输入设备（#7：换句子不再掉回默认设备）
                'llmBatchWorkers', 'asrAlignWorkers',
                'customProviders', 'speechProviders', 'volcApiKey', 'siliconflowApiKey',
                'dashscopeBaseUrl', 'dashscopeVoice',
                'asrHotwords', 'asrHotwordOn', 'asrVocabularyId', 'asrVocabModel',
                'customSpeechApiKey', 'ttsEngineModels',
                'ttsEngine', 'ttsEngineOverride', 'testSentences',
                'defineCompact', 'downloadProxy', 'speechProviders', 'volcApiKey', 'openaiApiKey', 'siliconflowApiKey', 'customSpeechApiKey',
                'enRefDir',
                'uiTheme', 'uiMotion', 'pvMode', 'llmProfiles', 'pricingOverrides', 'shortcuts',
                'usageBudget', 'parsePos',
            }
            for k, v in patch.items():
                if k in allowed:
                    cfg[k] = v
            tl.save_config(cfg)
            self.cfg = cfg
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def app_build(self):
        """构建指纹：让「我现在跑的是哪次构建」一眼可见。
        用户这次报的很多现象其实是跑了旧 dist（exe 10/15、源码 10/17 已修），
        有这行字就不必再靠猜。#6/#1 排查的第一步永远是先看这里。"""
        info = {'builtAt': '', 'stamp': '', 'source': '',
                'frozen': bool(getattr(sys, 'frozen', False))}
        for p in (self.app_dir / 'build_info.json', self.app_dir / '_internal' / 'build_info.json'):
            try:
                if p.exists():
                    d = json.loads(p.read_text(encoding='utf-8')) or {}
                    for k in ('builtAt', 'stamp', 'source'):
                        if d.get(k):
                            info[k] = str(d.get(k))
                    break
            except Exception:
                continue
        if not info['stamp']:
            info['stamp'] = '源码运行（未打包）'
            try:  # 源码运行时用 app.py 的修改时间当指纹：任何“改了没生效”的误会一眼可查
                info['source'] = datetime.fromtimestamp(
                    (APP_DIR / 'app.py').stat().st_mtime).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
        return {'ok': True, **info}

    # ================= 练习：基础 =================
    def _migrate_gsv_config(self):
        """GSV 移除（终稿 P0）：启动时清洗配置残留——ttsEngine/ttsEngineOverride
        里的 'local' 改当前可用引擎（阿里云优先，其次 Edge），死键删除。"""
        try:
            cfg = self.cfg
            dirty = False
            fb = speech_engine.preferred_engine(cfg)
            if str(cfg.get('ttsEngine') or '') == 'local':
                cfg['ttsEngine'] = fb
                dirty = True
            ov = cfg.get('ttsEngineOverride')
            if isinstance(ov, dict):
                for k, v in list(ov.items()):
                    if str(v) == 'local':
                        ov[k] = fb
                        dirty = True
            for k in ('gsvDir', 'gsvConfig', 'gsvExtraDirs', 'localRefEnabled'):
                if k in cfg:
                    cfg.pop(k, None)
                    dirty = True
            if dirty:
                tl.save_config(cfg)
                self.cfg = cfg
                self._plog(f'已清理 GPT-SoVITS 旧配置（该功能已移除，引擎自动切到 {fb}）')
        except Exception:
            pass

    def _migrate_retired_tts(self):
        """MiMo V2.5 下线（2026-10-21）：把指向死模型的配置迁到可用引擎。

        · ttsEngine/ttsProvider 是 mimo，且模型为空或落在 MIMO_RETIRED_MODELS
          → 切到 preferred_engine（有百炼 Key 用 dashscope，否则 Edge）；
        · 切到 dashscope 时补 ttsEngineModels.dashscope = qwen3-tts-flash；
        · ttsEngineOverride 里指向死 mimo 的场景一并改；
        · 旧的 mimoTtsModel 字段原样保留（只作历史参考，不再参与合成）——
          用户若已手填 v2.6 模型，mimo 视为可用，不迁移。
        """
        try:
            import speech_providers as _spv
            cfg = self.cfg
            retired = {str(x).lower() for x in getattr(_spv, 'MIMO_RETIRED_MODELS', set())}

            def _dead_mimo(v):
                if str(v or '').lower() != 'mimo':
                    return False
                mo = (((cfg.get('ttsEngineModels') or {}).get('mimo')
                       or cfg.get('mimoTtsModel') or '')).strip().lower()
                return (not mo) or mo in retired

            fb = speech_engine.preferred_engine(cfg)
            dirty = False
            if _dead_mimo(cfg.get('ttsEngine')) or _dead_mimo(cfg.get('ttsProvider')):
                cfg['ttsEngine'] = fb
                cfg['ttsProvider'] = fb
                dirty = True
            ov = cfg.get('ttsEngineOverride')
            if isinstance(ov, dict):
                for k, v in list(ov.items()):
                    if _dead_mimo(v):
                        ov[k] = fb
                        dirty = True
            if fb == 'dashscope':
                models = dict(cfg.get('ttsEngineModels') or {})
                if not (models.get('dashscope') or '').strip():
                    models['dashscope'] = 'qwen3-tts-flash'
                    cfg['ttsEngineModels'] = models
                    dirty = True
            if dirty:
                tl.save_config(cfg)
                self.cfg = cfg
                self._plog(f'MiMo V2.5 已下线：语音输出已切到 {fb}（原 MiMo 配置保留作参考）')
        except Exception:
            pass

    def _custom_voices(self):
        """读练习页自定义音色。

        音色工作台下线后，voices.json 里可能还躺着 design_/clone_ 两类旧条目：
        它们对应的接口已经不存在，读的时候就丢掉，别让它们再出现在下拉里。
        """
        try:
            if self.voice_file.exists():
                d = json.loads(self.voice_file.read_text(encoding='utf-8'))
                if not isinstance(d, list):
                    return []
                return [x for x in d if isinstance(x, dict)
                        and str(x.get('kind') or '').lower() not in ('design', 'clone')
                        and not str(x.get('id') or '').startswith(('design_', 'clone_'))]
        except Exception:
            pass
        return []

    def _clean_legacy_voices(self):
        """清掉 voices.json 里已下线能力（design_/clone_）的残留条目。

        音色工作台整卡删除后，这两类条目没有任何入口能删，也不会被合成使用，
        留着只会让旧配置看起来"还有音色"。只删这两类，练习页自定义风格（custom_）保留。
        改动前先原样备份成 voices.json.bak-<时间戳>；任何异常都不影响启动。
        """
        try:
            if not self.voice_file.exists():
                return
            items = json.loads(self.voice_file.read_text(encoding='utf-8'))
            if not isinstance(items, list):
                return
            dead = [x for x in items if isinstance(x, dict) and (
                str(x.get('kind') or '').lower() in ('design', 'clone')
                or str(x.get('id') or '').startswith(('design_', 'clone_')))]
            if not dead:
                return
            bak = self.voice_file.with_name(
                'voices.json.bak-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
            bak.write_bytes(self.voice_file.read_bytes())
            dead_ids = {id(x) for x in dead}
            keep = [x for x in items if id(x) not in dead_ids]
            store.write_json(self.voice_file, keep)
            self._plog(f'清理 voices.json：移除 {len(dead)} 个已下线的设计/克隆音色（备份 {bak.name}）')
        except Exception:
            pass

    def _services(self):
        """按当前配置构造服务对象。

        LLM 复用同一个实例：它上面挂着 token 用量计数，
        每次重建会把计数清零，用量就永远看不到。
        """
        cfg = tl.load_config()
        cfg['_appDir'] = str(self.app_dir)      # 给 DefineCache 定位用
        self.cfg = cfg
        tts = speech_tts.TTS(cfg, self.cache_dir, log=self._plog,
                             extra_presets=self._custom_voices())
        asr = speech_asr.ASR(cfg, log=self._plog)
        if self._llm is None:
            self._llm = analysis.LLM(cfg, log=self._plog)
            self._llm.set_usage_log(self.app_dir / 'usage_log.jsonl')
        else:
            self._llm.cfg = cfg                 # 配置可能改了
            self._llm._client = None            # 让下次重建客户端
        return tts, asr, self._llm

    def _coach(self, llm=None, lookup=False):
        """统一构造 Coach，保证词典缓存是同一个实例（跨调用累积命中）。

        lookup=True 时注入查词释义专用 API（若启用）；失败不回落主模型，
        由 define 内部抛错提示手动重试。
        """
        if llm is None:
            _t, _a, llm = self._services()
        if self._defcache is None:
            self._defcache = analysis.DefineCache(self.app_dir / 'define_cache.json')
        return analysis.Coach(llm, log=self._plog, defcache=self._defcache,
                              lookup_llm=(self._get_lookup_llm() if lookup else None))

    def _lookup_profile_state(self):
        cfg = tl.load_config()
        prof = (cfg.get('llmProfiles') or {}).get('lookup') or {}
        return (bool(prof.get('enabled')),
                (prof.get('baseUrl') or '').strip(),
                (prof.get('apiKey') or '').strip(),
                (prof.get('model') or '').strip())

    def _get_lookup_llm(self):
        """查词释义专用 LLM 实例（带缓存）；未启用或配置不全返回 None。"""
        enabled, base, key, model = self._lookup_profile_state()
        if not (enabled and base and key and model):
            return None
        if getattr(self, '_lookup_llm_obj', None) is None:
            lc = dict(tl.load_config())
            lc['llmBaseUrl'] = base
            lc['llmApiKey'] = key
            lc['llmModel'] = model
            self._lookup_llm_obj = analysis.LLM(lc, log=self._plog)
            self._lookup_llm_obj.set_usage_log(self.app_dir / 'usage_log.jsonl')
        return self._lookup_llm_obj

    def get_llm_profiles(self):
        enabled, base, key, model = self._lookup_profile_state()
        return {'ok': True,
                'lookup': {'enabled': enabled, 'baseUrl': base, 'model': model,
                           'hasKey': bool(key), 'keyTail': key[-4:] if key else ''}}

    def save_llm_profile(self, name, profile):
        try:
            if str(name or '').strip() != 'lookup':
                return {'ok': False, 'msg': '未知 profile：' + str(name)}
            cfg = tl.load_config()
            profiles = cfg.get('llmProfiles') or {}
            old = profiles.get(name) or {}
            new = {'enabled': bool(profile.get('enabled')),
                   'baseUrl': (profile.get('baseUrl') or '').strip(),
                   'model': (profile.get('model') or '').strip(),
                   'apiKey': (profile.get('apiKey') or '').strip() or old.get('apiKey', '')}
            profiles[name] = new
            cfg['llmProfiles'] = profiles
            tl.save_config(cfg)
            self.cfg = cfg
            self._lookup_llm_obj = None      # 强制下次重建
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def test_llm_profile(self, name):
        try:
            enabled, base, key, model = self._lookup_profile_state()
            if not (base and key and model):
                return {'ok': False, 'msg': '专用 API 配置不全（地址 / Key / 模型）'}
            import time as _t
            lc = dict(tl.load_config())
            lc['llmBaseUrl'] = base
            lc['llmApiKey'] = key
            lc['llmModel'] = model
            llm = analysis.LLM(lc, log=self._plog)
            t0 = _t.time()
            txt = llm.chat('回复一个词：ok', 'ok?', max_tokens=5, tag='专用API测试')
            return {'ok': True, 'latencyMs': int((_t.time() - t0) * 1000), 'reply': txt[:30]}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def _engine(self, tts=None):
        """统一语音引擎（阿里云 Qwen TTS / Edge / 其它供应商）。"""
        if tts is None:
            tts, _a, _l = self._services()
        if self._engine_obj is None:
            self._engine_obj = speech_engine.SpeechEngine(
                self.cfg, self.cache_dir, log=self._plog, tts=tts,
                extra_presets=self._custom_voices())
        else:
            # 配置可能改了，把新配置和 tts 换上
            self._engine_obj.cfg = self.cfg
            self._engine_obj._tts = tts
        return self._engine_obj

    # ================= 语音输出管理（总开关） =================
    def speech_overview(self):
        """语音输出管理面板：引擎列表 + 当前引擎 + 音色 + 试听句。
        每个引擎附 keyName/hasKey/model——「当前引擎的 Key/模型」区按此绑定。"""
        try:
            eng = self._engine()
            ov = eng.overview()
            models = self.cfg.get('ttsEngineModels') or {}
            for e in ov.get('engines') or []:
                eid = e.get('id')
                kn = (speech_engine.ENGINES.get(eid) or {}).get('needsKey')
                e['keyName'] = kn or ''
                try:
                    import speech_providers as _spv
                    preg = (_spv.PROVIDERS.get('tts') or {}).get(eid) or {}
                    e['baseUrl'] = preg.get('baseUrl', '')
                    e['canPull'] = bool(preg.get('canPull'))
                    e['builtin'] = [b.get('id') for b in (preg.get('builtin') or [])]
                    e['voices'] = preg.get('voices') or {}
                    e['baseUrlNote'] = preg.get('baseUrlNote', '')
                except Exception:
                    e['baseUrl'], e['canPull'], e['builtin'] = '', False, []
                    e['voices'], e['baseUrlNote'] = {}, ''
                if eid == 'mimo':
                    e['model'] = self.cfg.get('mimoTtsModel') or ''
                else:
                    e['model'] = models.get(eid) or ''
                e['modelSaved'] = bool(e['model'])   # 是否用户显式存过（没存过只是默认值，别当覆盖写回）
                # 空值不该让前端「当前模型」显示空白：补该供应商的默认最新模型
                if not e['model']:
                    try:
                        e['model'] = _spv.tts_default_model(eid)
                    except Exception:
                        e['model'] = ''
                # 音色目录由供应商注册表单源给出（跨系音色会被 Engine411 拒），
                # 前端据此只列当前模型系的音色
                try:
                    e['voiceList'] = _spv.tts_voices(eid, e['model'] or None)
                    e['families'] = list((e.get('voices') or {}).keys())
                    e['defaultModel'] = _spv.tts_default_model(eid)
                except Exception:
                    e['voiceList'], e['families'], e['defaultModel'] = [], [], ''
                has = bool(kn and (self.cfg.get(kn) or '').strip())
                if not has:
                    uc = (self.cfg.get('speechProviders') or {}).get(eid) or {}
                    has = bool((uc.get('apiKey') or '').strip())
                e['hasKey'] = has
                # MiMo V2.5 下线标记：引擎卡直接显示「已下线」，不让用户再误选死模型
                try:
                    import speech_providers as _spv2
                    _ret = {str(x).lower() for x in getattr(_spv2, 'MIMO_RETIRED_MODELS', set())}
                    _res = (e['model'] or '').lower()
                    e['retired'] = bool(eid == 'mimo' and ((not _res) or _res in _ret))
                    e['retiredNote'] = getattr(_spv2, 'MIMO_RETIRED_NOTE', '') if e['retired'] else ''
                except Exception:
                    e['retired'], e['retiredNote'] = False, ''
            ov['ok'] = True
            # 语音输出的持久字段：前端别再从 window._HUB_* 猜，统一从后端取
            ov['dashscopeVoice'] = self.cfg.get('dashscopeVoice') or ''
            ov['dashscopeBaseUrl'] = self.cfg.get('dashscopeBaseUrl') or ''
            ov['ttsEngineModels'] = dict(models)
            try:
                import speech_providers as _spv2
                ov['scenes'] = list(getattr(_spv2, 'TTS_SCENES', []) or [])
            except Exception:
                ov['scenes'] = []
            return ov
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def asr_self_test(self, seconds=5, lang='en'):
        """🎤 录音自测：录 N 秒 → 用当前配置的识别模型实测 → 返回文本+耗时。
        验证「模型/Key 是否真的生效」，设置页语音识别卡的试金石。"""
        try:
            import speech_asr
            import tempfile
            _t, asr, _l = self._services()
            rec = speech_asr.Recorder(log=lambda m: None)
            wav = Path(tempfile.mktemp(suffix='.wav'))
            rec.start()
            time.sleep(max(2, min(int(seconds or 5), 15)))
            _p, _dur, _peak = rec.stop(str(wav))
            if not wav.exists() or wav.stat().st_size < 2000:
                return {'ok': False, 'msg': '没录到声音（检查麦克风权限/输入设备）'}
            try:
                t0 = time.time()
                res = asr.recognize(str(wav), lang if lang in ('en', 'zh') else 'en')
                ms = int((time.time() - t0) * 1000)
                txt = (res.get('text') or '').strip()
                return {'ok': True, 'text': txt, 'ms': ms,
                        'wordTs': bool(res.get('words')),
                        'msg': '' if txt else '识别成功但内容为空（再试一次，说响一点）'}
            finally:
                wav.unlink(missing_ok=True)
        except Exception as e:
            try:
                import speech_providers as _spv2
                e = RuntimeError(_spv2.err_hint(self.cfg.get('asrModel') or '', str(e), 'asr'))
            except Exception:
                pass
            return {'ok': False, 'msg': str(e)[:200]}

    # ================= 语音识别：热词（预编译词表） =================
    def asr_vocab_panel(self):
        """热词面板数据：本地词表 + 云端表状态（vocabulary_id/绑定模型/是否失配）。"""
        try:
            import speech_asr
            return speech_asr.vocab_panel(tl.load_config())
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def asr_vocab_save(self, words=None, on=None):
        """保存本地词表+开关（纯本地，不发网络；同步云端另点「同步」按钮）。"""
        try:
            cfg = tl.load_config()
            clean = []
            for w in (words or []):
                t = str((w if isinstance(w, dict) else {}).get('word') or '').strip()
                if not t:
                    continue
                try:
                    wt = int((w if isinstance(w, dict) else {}).get('weight') or 4)
                except (TypeError, ValueError):
                    wt = 4
                clean.append({'word': t[:60], 'weight': wt})
            cfg['asrHotwords'] = clean[:2000]      # 官方上限：主版本 2000 词
            if on is not None:
                cfg['asrHotwordOn'] = bool(on)
            tl.save_config(cfg)
            self.cfg = cfg
            return {'ok': True, 'count': len(clean)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def asr_vocab_sync(self):
        """本地词表 → 云端热词表（建/更新）。换过识别模型自动重建表
        （target_model 必须与识别模型一致，否则热词静默不生效）。"""
        try:
            import speech_asr
            cfg = tl.load_config()
            r = speech_asr.vocab_sync(cfg, log=self._plog)
            if r.get('ok'):
                cfg['asrVocabularyId'] = r.get('vocabulary_id') or ''
                cfg['asrVocabModel'] = r.get('target_model') or ''
                tl.save_config(cfg)
                self.cfg = cfg
            return r
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def asr_vocab_delete(self):
        """删云端热词表（本地词表保留，可再同步重建）。"""
        try:
            import speech_asr
            cfg = tl.load_config()
            r = speech_asr.vocab_delete(cfg, log=self._plog)
            cfg['asrVocabularyId'] = ''
            cfg['asrVocabModel'] = ''
            tl.save_config(cfg)
            self.cfg = cfg
            return r
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def speech_set_engine(self, engine, scene=''):
        """设引擎。scene 为空 = 全局默认；非空 = 只覆写那个场景。

        engine 传空串 = **清除**该场景的覆盖（回落到全局引擎）——之前清不掉，
        场景被覆写后只能改不能删，属于「音节设置怎么改都不对」的一部分。"""
        try:
            cfg = tl.load_config()
            if scene:
                ov = dict(cfg.get('ttsEngineOverride') or {})
                if not engine:
                    ov.pop(scene, None)
                    cfg['ttsEngineOverride'] = ov
                    tl.save_config(cfg)
                    self._engine_obj = None
                    self._plog(f'语音引擎：{scene} 场景覆盖已清除（回落全局）')
                    return {'ok': True, 'engine': '', 'scene': scene, 'cleared': True}
                if engine not in speech_engine.ENGINES:
                    return {'ok': False, 'msg': f'未知引擎：{engine}'}
                ov[scene] = engine
                cfg['ttsEngineOverride'] = ov
            else:
                if engine not in speech_engine.ENGINES:
                    return {'ok': False, 'msg': f'未知引擎：{engine}'}
                cfg['ttsEngine'] = engine
                cfg['ttsProvider'] = engine        # 引擎=供应商：直连 TTS 层的调用（tts_speak 等）跟着走
            tl.save_config(cfg)
            self._engine_obj = None            # 让下次重建
            self._plog(f"语音引擎 → {speech_engine.ENGINES[engine]['name']}"
                       + (f"（仅 {scene}）" if scene else "（全局）"))
            warn = ''
            if engine == 'mimo' and not speech_engine._mimo_usable(cfg):
                try:
                    import speech_providers as _spv
                    warn = getattr(_spv, 'MIMO_RETIRED_NOTE', '')
                except Exception:
                    warn = 'MiMo V2.5 已下线，请填新模型名（如 mimo-v2.6-tts）'
            return {'ok': True, 'engine': engine, 'scene': scene, 'warn': warn}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def speech_set_test_sentence(self, lang, text):
        """改固定试听句。"""
        try:
            cfg = tl.load_config()
            t = dict(cfg.get('testSentences') or {})
            t[lang] = (text or '').strip()
            cfg['testSentences'] = t
            tl.save_config(cfg)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def speech_preview(self, voice_id='', lang='en', scene='', speed=1.0, allow_risky=False):
        """固定试听句合成（一次合成，之后走缓存，不再调 API）。"""
        try:
            eng = self._engine()
            r = eng.preview(voice_id=voice_id, lang=lang, scene=scene or None,
                            speed=speed, allow_risky=bool(allow_risky))
            self._refresh_roots()
            r['url'] = self._url(r['file'])
            return r
        except Exception as e:
            msg = str(e)
            return {'ok': False, 'msg': msg[:300],
                    'risky': '本地引擎不适合这段文本' in msg}

    def speech_voices(self, scene=''):
        try:
            return {'ok': True, 'voices': self._engine().voices(scene or None),
                    'engine': self._engine().engine(scene or None)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def speech_risk(self, text, scene=''):
        """这段文本用当前引擎合成有没有风险（主要是「本地 + 短英文」）。"""
        try:
            eng = self._engine()
            return {'ok': True, 'risk': eng.current_risk(text, scene or None),
                    'engine': eng.engine(scene or None)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

            return {'ok': False, 'msg': str(e)[:200]}

    # ================= LLM 模型管理 =================
    def llm_models(self):
        """拉取当前端点提供的模型列表。"""
        try:
            _t, _a, llm = self._services()
            return llm.list_models()
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def llm_speed_test(self, models=None):
        """给一批模型测速（首字延迟 + 总耗时）。"""
        try:
            _t, _a, llm = self._services()
            models = models or []
            if not models:
                m = (self.cfg.get('llmModel') or '').strip()
                models = [m] if m else []
            if not models:
                return {'ok': False, 'msg': '没有可测的模型'}
            if len(models) > 12:
                models = models[:12]
            return llm.speed_test(models)
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def llm_set_model(self, model):
        try:
            cfg = tl.load_config()
            cfg['llmModel'] = (model or '').strip()
            tl.save_config(cfg)
            self._plog(f"LLM 模型 → {cfg['llmModel']}")
            return {'ok': True, 'model': cfg['llmModel']}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def llm_usage(self):
        """本次运行的 token 用量 + 词典缓存命中情况。"""
        try:
            _t, _a, llm = self._services()
            u = llm.usage_info()
            u['ok'] = True
            if self._defcache is None:
                self._defcache = analysis.DefineCache(self.app_dir / 'define_cache.json')
            u['defineCache'] = self._defcache.stats()
            u['defineCompact'] = (self.cfg.get('defineCompact') is not False)
            return u
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def define_cache_clear(self):
        try:
            if self._defcache is None:
                self._defcache = analysis.DefineCache(self.app_dir / 'define_cache.json')
            n = self._defcache.size()
            self._defcache.clear()
            return {'ok': True, 'cleared': n}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}

    def set_define_compact(self, on):
        try:
            cfg = tl.load_config()
            cfg['defineCompact'] = bool(on)
            tl.save_config(cfg)
            return {'ok': True, 'compact': bool(on)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:200]}


    def _plog(self, m):
        self._plogs.append({'t': datetime.now().strftime('%H:%M:%S'), 'm': str(m)})
        if len(self._plogs) > 300:
            self._plogs = self._plogs[-200:]

    def practice_info(self):
        """练习页初始化：能力状态、音色预设、设备、统计。"""
        cfg = tl.load_config()
        tts, asr, llm = self._services()
        rec_ok, rec_msg = speech_asr.Recorder.available()
        # list_presets() 已经带了「练习页自定义风格」（extra_presets，构造 TTS 时传入），
        # 这里绝不能再拼一次 voices.json——否则每个自定义音色都会在下拉里出现两遍。
        presets = tts.list_presets()
        # 麦克风默认设备在服务端给一次，前端不必每次自己回落（#7 换句跳回默认设备的根因之一）
        try:
            mic_dev = int(cfg.get('micDevice')) if str(cfg.get('micDevice') or '').strip() else None
        except (TypeError, ValueError):
            mic_dev = None
        # 当前引擎必须是「真的会用」的那个：MiMo V2.5 下线后不能继续报 mimo，
        # Key/模型也要按该供应商查（曾经恒查 mimoApiKey + mimo 模型 → 状态假显示）。
        try:
            eng = speech_engine.current_engine(cfg)
        except Exception:
            eng = 'dashscope'
        try:
            import speech_providers as _spv
            _model = ((cfg.get('ttsEngineModels') or {}).get(eng)
                      or (cfg.get('mimoTtsModel') if eng == 'mimo' else '')
                      or _spv.tts_default_model(eng))
        except Exception:
            _model = ''
        _kn = (speech_engine.ENGINES.get(eng) or {}).get('needsKey')
        _has = (not _kn) or bool((cfg.get(_kn) or '').strip()) or bool(
            ((cfg.get('speechProviders') or {}).get(eng) or {}).get('apiKey'))
        return {
            'ok': True,
            'tts': {
                'provider': cfg.get('ttsProvider') or eng,
                'hasKey': _has,
                'model': _model,
                'voice': ((cfg.get('dashscopeVoice') if eng == 'dashscope'
                           else cfg.get('mimoVoice')) or ''),
                'engine': eng,
                'engineOverride': dict(cfg.get('ttsEngineOverride') or {}),
            },
            'asr': {
                'hasKey': bool((cfg.get('dashscopeApiKey') or '').strip()),
                # 别再谎报：字段为空时返回空串，前端 svcBar 显示「未设置」而不是假装在跑
                # paraformer-realtime-v2（用户切模型后这里不刷新就是 ⑥ 的根因）
                'model': cfg.get('asrModel') or '',
                'provider': cfg.get('asrProvider') or 'dashscope',
                'audioModel': cfg.get('audioModel') or '',
                'mode': cfg.get('asrMode') or 'traditional',
            },
            'micDevice': mic_dev,
            'llm': {
                'ready': llm.ready,
                'baseUrl': cfg.get('llmBaseUrl') or '',
                'model': cfg.get('llmModel') or '',
            },
            'recorder': {'ok': rec_ok, 'msg': rec_msg},
            'presets': presets,
            'presetId': cfg.get('ttsPreset') or 'narrator',
            'vocabStats': self.vocab.stats(),
            'practiceStats': self._practice_stats(),
        }

    # ---------- 音色
    def add_custom_voice(self, name, instruct, voice='Chloe'):
        """自定义音色：本质就是自定义一段风格指令。"""
        name = (name or '').strip()
        instruct = (instruct or '').strip()
        if not name or not instruct:
            return {'ok': False, 'msg': '名称和风格描述都要填'}
        try:
            items = json.loads(self.voice_file.read_text(encoding='utf-8')) if self.voice_file.exists() else []
        except Exception:
            items = []
        vid = 'custom_' + str(int(time.time()))
        items.append({'id': vid, 'name': name, 'desc': instruct[:60],
                      'instruct': instruct, 'voice': voice, 'custom': True})
        store.write_json(self.voice_file, items)
        return {'ok': True, 'id': vid}

    # ---------- 音色设计 / 克隆：UI 与后端入口均已删除（#9 音色工作台整卡删除）
    # MiMo V2.5 起 voicedesign / voiceclone 下线，语音合成只用内置音色；
    # 旧 voices.json 的 design_/clone_ 条目由 _custom_voices() 过滤并在启动时清理。

    def remove_custom_voice(self, vid):
        """删自定义风格音色（练习页「自定义风格」仍在用，保留）。"""
        try:
            items = json.loads(self.voice_file.read_text(encoding='utf-8')) if self.voice_file.exists() else []
            items = [x for x in items if x.get('id') != vid]
            store.write_json(self.voice_file, items)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------- 音色预设分组（给界面下拉用）
    def tts_presets(self):
        tts, _a, _l = self._services()
        presets = tts.list_presets()
        # （本地 GPT-SoVITS 音色已按终稿 P0 移除）
        return {'ok': True, 'presets': presets,
                'samples': speech_tts.TEST_SAMPLES,
                'ttsProvider': self.cfg.get('ttsProvider') or 'dashscope'}

    # ---------- 逐句练习用：拿一句的文本 + 参考音频
    def sentence_list(self, item_id, q='', limit=0, offset=0):
        """句子清单（#4 跳转弹窗的数据源）：一次给全，前端本地过滤/搜索。

        一集通常 100~500 句，整包 JSON 只有几十 KB，比分页来回快得多；
        limit/offset 留给超长材料（有声书整本）兜底。"""
        try:
            it = next((x for x in tl.scan_library(self.all_dirs())
                       if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            en_vtt = _audio_sib(it['audio'], '.en.vtt')
            if not en_vtt.exists():
                return {'ok': False, 'msg': '这个语料没有英文字幕，无法跳句——请先生成字幕'}
            cues = tl.parse_vtt_cues(en_vtt.read_text(encoding='utf-8', errors='replace'))
            rows = []
            qq = (q or '').strip().lower()
            for i, c in enumerate(cues):
                t = c.get('text') or ''
                if qq and qq not in t.lower():
                    continue
                rows.append({'index': i, 'en': t,
                             'start': round(float(c.get('start') or 0), 2),
                             'end': round(float(c.get('end') or 0), 2)})
            total = len(rows)
            if limit:
                rows = rows[int(offset or 0):int(offset or 0) + int(limit)]
            return {'ok': True, 'total': total, 'cueTotal': len(cues),
                    'title': it['title'], 'itemId': item_id, 'rows': rows}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def practice_sentence(self, item_id, index):
        """取某条语料的第 index 句（含英文与中文）。

        Bug #5 修复：添加前后句信息，供精讲功能使用。
        """
        try:
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            en_vtt = _audio_sib(it['audio'], '.en.vtt')
            zh_vtt = _audio_sib(it['audio'], '.zh.vtt')
            if not en_vtt.exists():
                return {'ok': False, 'msg': '这个语料没有英文字幕，无法逐句练习——请先在语料卡片上点「⚡生成字幕」'}
            # 复用播放器的解析逻辑：直接读 vtt 文本交给前端解析太重，这里自己解析
            cues = tl.parse_vtt_cues(en_vtt.read_text(encoding='utf-8', errors='replace'))
            if not cues:
                return {'ok': False, 'msg': '字幕解析为空'}
            if index < 0 or index >= len(cues):
                return {'ok': False, 'msg': '句子序号越界'}
            zh = []
            if zh_vtt.exists():
                zh = tl.parse_vtt_cues(zh_vtt.read_text(encoding='utf-8', errors='replace'))
            c = cues[index]
            zt = ''
            for z in zh:
                if z['start'] < c['end'] and z['end'] > c['start']:
                    zt = (zt + ' ' + z['text']).strip()

            # Bug #5 修复：获取前后句用于精讲上下文
            prev_en = cues[index - 1]['text'] if index > 0 else ''
            next_en = cues[index + 1]['text'] if index < len(cues) - 1 else ''

            self._refresh_roots()
            return {
                'ok': True,
                'index': index, 'total': len(cues),
                'en': c['text'], 'zh': zt,
                'start': c['start'], 'end': c['end'],
                'audioUrl': self._url(Path(it['audio'])),
                'title': it['title'],
                'itemId': item_id,
                'prevEn': prev_en,  # Bug #5 修复
                'nextEn': next_en,  # Bug #5 修复
            }
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def segment_audio(self, item_id, start, end):
        """服务端原声切段（播放错位根治）：ffmpeg 按字幕时间戳精确切出这一句的
        小 mp3，前端从 0 直接播——不再依赖浏览器对大音频的 currentTime 定位
        （WebView2 在大 mp3 上 seek 受 readyState 竞态/帧估算影响，会出现
        "每句播的都不是这句"的整体错位，而后端按时间戳切段是精确的）。
        切段进 cache/seg/（同参数秒回）；超量自动清最旧的。"""
        try:
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到语料'}
            start = max(0.0, float(start or 0))
            end = float(end or 0)
            if end <= start:
                end = start + 0.5
            dur = min(end - start + 0.15, 3600)
            import hashlib as _h
            key = _h.sha1(f'{item_id}|{start:.2f}|{end:.2f}'.encode('utf-8')).hexdigest()[:16]
            segdir = self.cache_dir / 'seg'
            segdir.mkdir(parents=True, exist_ok=True)
            out = segdir / f'seg_{key}.mp3'
            if not (out.exists() and out.stat().st_size > 1000):
                import subprocess as _sp
                nw = getattr(_sp, 'CREATE_NO_WINDOW', 0)
                r = _sp.run([tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                             '-ss', f'{start:.3f}', '-i', str(it['audio']),
                             '-t', f'{dur:.3f}', '-c', 'copy', str(out)],
                            capture_output=True, creationflags=nw)
                if r.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
                    # copy 切帧失败（个别流）→ 重编码兜底
                    r = _sp.run([tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                                 '-ss', f'{start:.3f}', '-i', str(it['audio']),
                                 '-t', f'{dur:.3f}', '-b:a', '128k', str(out)],
                                capture_output=True, creationflags=nw)
                if not (out.exists() and out.stat().st_size > 1000):
                    return {'ok': False, 'msg': '原声切段失败'}
                try:                        # 缓存上限：超 1200 个清最旧 300 个
                    segs = sorted(segdir.glob('seg_*.mp3'), key=lambda p: p.stat().st_mtime)
                    if len(segs) > 1200:
                        for p in segs[:300]:
                            p.unlink(missing_ok=True)
                except Exception:
                    pass
            self._refresh_roots()
            return {'ok': True, 'url': self._url(out), 'dur': round(dur, 2)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    # ---------- 合成（统一走引擎层）
    def tts_speak(self, text, preset_id=None, scene='', allow_risky=False):
        try:
            cfg = tl.load_config()
            eng = self._engine()
            eid = eng.engine(scene or None)
            vid = preset_id or (cfg.get('ttsPreset') or 'narrator')
            path = eng.synthesize(text, vid, scene=scene or None, allow_risky=bool(allow_risky))
            self._refresh_roots()
            return {'ok': True, 'url': self._url(path), 'file': path, 'via': eid,
                    'engineName': eng.engine_info(eid)['name']}
        except Exception as e:
            # 把"风险"单独标出来，前端可以据此给出一键换引擎的按钮
            msg = str(e)
            risky = '本地引擎不适合这段文本' in msg
            return {'ok': False, 'msg': msg, 'risky': risky,
                    'risk': self._engine().current_risk(text, scene or None) if risky else None}

    # ---------- 录音
    def _mic_device(self):
        """用户锁定的输入设备：设置页存进 micDevice；没有就返回 None（跟系统默认）。
        #7：前端不再每次进练习页都把设备下拉重置成 default，后端也不再用 None 直落默认。"""
        try:
            v = self.cfg.get('micDevice') if isinstance(self.cfg, dict) else None
            if v in (None, '', 'default'):
                return None
            return int(v)
        except Exception:
            return None

    def rec_start(self, device=None):
        try:
            # 前端显式传了有效设备就用它（并记住）；没传就沿用上次锁定的设备
            if device in (None, '', 'default'):
                device = self._mic_device()
            else:
                try:
                    device = int(device)
                except Exception:
                    device = self._mic_device()
                if device is not None and device != self.cfg.get('micDevice'):
                    cfg = tl.load_config()
                    cfg['micDevice'] = device
                    tl.save_config(cfg)
                    self.cfg = cfg
            self._rec.start(device)
            self._recording = True
            return {'ok': True, 'device': device}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def rec_level(self):
        return {'ok': True, 'level': self._rec.level() if self._recording else 0.0}

    def rec_stop(self, item_id='', index=0):
        try:
            self._recording = False
            out = self.cache_dir / f'rec_{int(time.time())}.wav'
            path, dur, peak = self._rec.stop(out)
            self._refresh_roots()
            return {'ok': True, 'file': path, 'url': self._url(path),
                    'duration': dur, 'peak': peak,
                    'tooQuiet': peak < 0.02}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def rec_devices(self):
        d = speech_asr.Recorder.list_devices()
        # 额外告诉前端“当前锁定的是哪个设备”，前端据此保持下拉选中（#7）
        try:
            if isinstance(d, dict):
                d = dict(d)
                d['current'] = self._mic_device()
        except Exception:
            pass
        return d

    # ---------- 识别 + 比对 + 纠音（一步到位）
    def practice_check(self, item_id, index, ref_text, rec_file='', rec_url='',
                       with_llm=True):
        """核心流程：识别录音 → 逐词比对 → （可选）LLM 纠音。"""
        try:
            _tts, asr, llm = self._services()
            wav = rec_file
            if not wav and rec_url:
                # 从本地服务 URL 还原成文件路径
                wav = self._path_from_url(rec_url)
            if not wav or not Path(wav).exists():
                return {'ok': False, 'msg': '找不到录音文件'}

            try:
                r = asr.recognize(wav, 'en')
            except Exception as e:
                self._append_practice({
                    'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'kind': 'invalid', 'reason': 'asr_failed',
                    'itemId': item_id, 'index': index, 'en': ref_text})
                return {'ok': False, 'invalid': True,
                        'msg': '识别失败（录音太短或全是静音）：' + str(e)[:80]}
            if not (r.get('text') or '').strip():
                # 没读/只读一点：记为无效尝试（数据中心单独计数），不烧 LLM
                self._append_practice({
                    'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'kind': 'invalid', 'reason': 'no_speech',
                    'itemId': item_id, 'index': index, 'en': ref_text})
                return {'ok': False, 'invalid': True,
                        'msg': '没识别到有效语音——对着麦克风把整句读完再点停止'}
            cmp = speech_asr.compare(ref_text, r['text'])

            diag = None
            if with_llm:
                try:
                    coach = analysis.Coach(llm)
                    diag = coach.diagnose(ref_text, r['text'], cmp)
                except Exception as e:
                    diag = {'error': str(e)}

            # 记录练习。score 优先取 LLM 纠音分；没跑纠音（with_llm=False 或 diag 无分）
            # 时回落本地口径分（listening.score_of：准确率-多读×3，钳 0-100），
            # 否则数据中心「平均发音得分」与练习记录在手动纠音后长期为空（2026-10-16 修复）。
            _score = None
            if isinstance(diag, dict) and isinstance(diag.get('score'), (int, float)):
                _score = diag['score']
            else:
                try:
                    _score = listening.score_of(cmp)
                except Exception:
                    _score = None
            rec = {
                'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'itemId': item_id, 'index': index,
                'en': ref_text, 'heard': r['text'],
                'accuracy': cmp['accuracy'],
                'score': _score,
                'wrong': cmp['wrong'], 'missed': cmp['missed'], 'extra': cmp['extra'],
            }
            self._append_practice(rec)
            self._bump_item_stats(item_id, index, cmp.get('accuracy'))

            return {'ok': True, 'heard': r['text'], 'words': r['words'],
                    'sentences': r['sentences'], 'compare': cmp, 'diag': diag,
                    'recUrl': self._url(wav)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def practice_diag(self, ref_text, heard, accuracy=0, wrong=0, missed=0, extra=0):
        """只做 LLM 纠音分析（识别/比对已在 practice_check 完成，前端异步补跑这一步）。"""
        try:
            _tts, _asr, llm = self._services()
            if not llm.ready:
                return {'ok': False, 'msg': '还没配 LLM'}
            cmp = {'accuracy': accuracy, 'wrong': wrong, 'missed': missed,
                   'extra': extra, 'items': [], 'refCount': 0}
            coach = analysis.Coach(llm)
            diag = coach.diagnose(ref_text, heard or '', cmp)
            return {'ok': True, 'diag': diag}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def quick_drill_summary(self, attempts):
        """快速训练结束后的整体总结（LLM 一次调用）。"""
        try:
            _tts, _asr, llm = self._services()
            if not llm.ready:
                return {'ok': False, 'msg': '还没配 LLM，跳过 AI 总结'}
            sys_p, _u = prompts.get('drill_summary')
            user = prompts.fmt('drill_summary',
                               attempts=json.dumps(attempts or [], ensure_ascii=False, indent=1))
            data = llm.chat_json(sys_p, user, max_tokens=1200, tag='快速训练总结')
            return {'ok': True, 'summary': data}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def _path_from_url(self, url):
        """把 http://127.0.0.1:port/xxx 还原成本地路径。"""
        try:
            from urllib.parse import unquote, urlparse
            p = unquote(urlparse(url).path).lstrip('/')
            for root in localfs.Handler.allowed_roots:
                cand = Path(root) / p
                if cand.exists():
                    return str(cand)
        except Exception:
            pass
        return ''

    # ---------- 生词本
    def vocab_add(self, text, context='', with_detail=True, source='', vtype='', meaning='',
                  dup_bump=True):
        """收藏一个词/短语。

        token 说明：detail 走 Coach.define，它有两道省 token 的闸 ——
          ① 词典缓存：同一个词第二次不花钱（返回值里 _fromCache=True）
          ② 精简模板：比完整版少约 56% 输入
        vtype: word=单词 / phrase=短语·搭配；meaning=已知中文含义（免 LLM）
        dup_bump: 已存在时是否 reviewCount+1——查词抽屉打开=真实遇到（True）；
        手动点收藏按钮的重复点击=False（幂等不刷"重复遇到"次数）。
        """
        try:
            detail = None
            if with_detail:
                _t, _a, llm = self._services()
                if llm.ready:
                    try:
                        detail = self._coach(llm, lookup=True).define(text, context)
                    except Exception as e:
                        detail = {'error': str(e)}
                else:
                    detail = {'error': '没配 LLM，无法自动生成详解（可以只收藏，稍后补）'}
            r = self.vocab.add(text, context, detail, source,
                               vtype=vtype or 'word', meaning=meaning or '',
                               count_review=dup_bump)
            if isinstance(r, dict):
                r['fromCache'] = bool(isinstance(detail, dict) and detail.get('_fromCache'))
            return r
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def vocab_list(self, q='', only_unmastered=False):
        items = self.vocab.all()
        if q:
            ql = q.lower()
            items = [x for x in items
                     if ql in (x.get('text') or '').lower()
                     or ql in json.dumps(x.get('detail') or {}, ensure_ascii=False).lower()]
        if only_unmastered:
            items = [x for x in items if not x.get('mastered')]
        items = sorted(items, key=lambda x: x.get('addedAt') or '', reverse=True)
        return {'ok': True, 'items': items, 'stats': self.vocab.stats()}

    def vocab_remove(self, vid):
        return self.vocab.remove(vid)

    def vocab_update(self, vid, **fields):
        return self.vocab.update(vid, **fields)

    def vocab_detail(self, vid):
        for x in self.vocab.all():
            if x.get('id') == vid:
                return {'ok': True, 'item': x}
        return {'ok': False, 'msg': '找不到'}

    # ---------- 练习记录与总结
    def _load_practice(self):
        if not self.log_file.exists():
            return []
        try:
            d = json.loads(self.log_file.read_text(encoding='utf-8'))
            return d if isinstance(d, list) else d.get('items', [])
        except Exception:
            return []

    def _append_practice(self, rec):
        items = self._load_practice()
        items.append(rec)
        if len(items) > 5000:
            items = items[-4000:]
        store.write_json(self.log_file, items)

    def _practice_stats(self):
        items = self._load_practice()
        if not items:
            return {'sessions': 0, 'avgAccuracy': 0, 'avgScore': 0, 'recent': []}
        accs = [x.get('accuracy') or 0 for x in items]
        # 发音得分：没有显式 score 的历史条目按同口径本地分回填（准确率-多读×3，钳 0-100），
        # 让数据中心不再恒显 0（2026-10-16 修复，写入时已带 score，这里兜底旧数据）。
        scores = []
        for x in items:
            s = x.get('score')
            if not isinstance(s, (int, float)):
                # 只有发音/听写类记录才有 accuracy+extra 可比分；invalid 之类跳过
                if x.get('accuracy') is None:
                    continue
                try:
                    s = listening.score_of({'accuracy': x.get('accuracy') or 0,
                                            'extra': x.get('extra') or 0})
                except Exception:
                    s = None
            if isinstance(s, (int, float)):
                scores.append(s)
        today = datetime.now().strftime('%Y-%m-%d')
        return {
            'sessions': len(items),
            'avgAccuracy': round(sum(accs) / len(accs), 1) if accs else 0,
            'avgScore': round(sum(scores) / len(scores), 1) if scores else 0,
            'today': sum(1 for x in items if (x.get('at') or '').startswith(today)),
            'last10': [
                {'at': x.get('at'), 'accuracy': x.get('accuracy'), 'score': x.get('score'),
                 'extra': x.get('extra') or 0,
                 'en': (x.get('en') or '')[:40]}
                for x in items[-10:]
            ],
        }

    def practice_records(self, limit=200):
        items = self._load_practice()
        return {'ok': True, 'items': items[-limit:], 'stats': self._practice_stats()}

    def practice_summary(self, days=7):
        """LLM 生成练习总结。"""
        try:
            _t, _a, llm = self._services()
            if not llm.ready:
                return {'ok': False, 'msg': '还没配 LLM，无法生成总结'}
            items = self._load_practice()
            if not items:
                return {'ok': False, 'msg': '还没有练习记录'}
            words = self.vocab.all()
            recs = [{'时间': x.get('at'), '准确率': x.get('accuracy'), '得分': x.get('score'),
                     '句子': (x.get('en') or '')[:70], '听到': (x.get('heard') or '')[:70],
                     '读错': x.get('wrong'), '漏读': x.get('missed'), '多读': x.get('extra')}
                    for x in items[-60:]]
            # 材料难度参考（离线评级，零 token）：最近记录涉及的语料逐个算
            diff_lines = []
            seen_items = []
            for x in items[-60:]:
                iid = x.get('itemId')
                if iid and iid not in seen_items:
                    seen_items.append(iid)
            for iid in seen_items[:8]:
                try:
                    _it, cues = self._cues_of(iid)
                    d = diffmod.compute(cues)
                    if not d:
                        continue
                    accs = [x.get('accuracy') for x in items[-60:]
                            if x.get('itemId') == iid and isinstance(x.get('accuracy'), (int, float))]
                    avg = round(sum(accs) / len(accs)) if accs else None
                    diff_lines.append('《' + str(_it.get('title') or '')[:40] + '》难度 '
                                      + d['level'] + d['name'] + '（语速 ' + str(d['rate']) + ' 词/秒）'
                                      + ('近平均准确率 ' + str(avg) + '%' if avg is not None else ''))
                except Exception:
                    continue
            data = analysis.Coach(llm).summarize(recs, words, '；'.join(diff_lines))
            data['_meta'] = {'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M'),
                             'basedOn': len(recs)}
            return {'ok': True, 'summary': data}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def practice_logs(self, since=0):
        return {'ok': True, 'logs': self._plogs[since:], 'total': len(self._plogs)}

    # ================= 听力练习 =================
    def _cues_of(self, item_id):
        items = tl.scan_library(self.all_dirs())
        it = next((x for x in items if x['id'] == item_id), None)
        if not it:
            raise RuntimeError('找不到该语料')
        en = _audio_sib(it['audio'], '.en.vtt')
        if not en.exists():
            raise RuntimeError('这个语料没有英文字幕（可先在卡片上点「⚡生成字幕」）')
        cues = tl.parse_vtt_cues(en.read_text(encoding='utf-8', errors='replace'))
        # 逐句 AI 难度（诊断修正的 sidecar；句数不符则弃用，回退本地规则）
        # B1 修复：写入方是 {slug}.lvl.json（doctor 落盘），读取路径必须同构——
        # 曾用 en.with_suffix('.lvl.json') 产出 X.en.lvl.json（永不存在），
        # 导致 AI 分级自上线以来从未生效
        try:
            lv_path = en.parent / (Path(it['audio']).stem + '.lvl.json')
            if lv_path.exists():
                d = json.loads(lv_path.read_text(encoding='utf-8'))
                if isinstance(d, dict) and d.get('cueCount') == len(cues)                         and d.get('promptVersion') == prompts.version_of('subtitle_doctor'):
                    for c, lv in zip(cues, d.get('levels') or []):
                        if lv in ('simple', 'normal', 'complex'):
                            c['level'] = lv
        except Exception:
            pass
        # 句子组装层（A6/A2 治标）：碎片合并+句末拆分——不动字幕文件，
        # 逐句精读/听力/精讲/doctor/预检（全走本入口）立即按真句子工作；
        # 索引=组装后的句序，各消费方天然一致。level 随合并/拆分传播
        try:
            import videosrc as _vs
            cues = _vs.assemble_sentences(cues)
        except Exception:
            pass
        return it, cues

    # ================= 听力难度评级（离线） + 脱稿大意 =================
    def _difficulty_cache(self):
        try:
            d = json.loads((self.app_dir / 'difficulty.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def item_difficulty(self, item_id):
        """单语料难度（三维打分 L1-L5，difficulty.json 缓存，零 token）。"""
        try:
            cache = self._difficulty_cache()
            d = cache.get(str(item_id))
            if not d:
                _it, cues = self._cues_of(item_id)
                d = diffmod.compute(cues)
                if d:
                    cache[str(item_id)] = d
                    store.write_json(self.app_dir / 'difficulty.json', cache)
            return {'ok': True, 'difficulty': d}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def difficulty_map(self):
        """全部语料难度 map（首次会现算未缓存的语料）。"""
        try:
            cache = self._difficulty_cache()
            items = tl.scan_library(self.all_dirs())
            dirty = False
            for it in items:
                if it['id'] in cache:
                    continue
                try:
                    en = _audio_sib(it['audio'], '.en.vtt')
                    if not en.exists():
                        continue
                    cues = tl.parse_vtt_cues(en.read_text(encoding='utf-8', errors='replace'))
                    d = diffmod.compute(cues)
                    if d:
                        cache[it['id']] = d
                        dirty = True
                except Exception:
                    continue
            if dirty:
                store.write_json(self.app_dir / 'difficulty.json', cache)
            return {'ok': True, 'map': cache}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def gist_pick(self, item_id):
        """脱稿大意：随机挑一个连续句窗（跳开场白、信息密度加权、避开最近 7 天练过的窗口）。"""
        try:
            import random as _rd
            from datetime import timedelta as _td
            _it, cues = self._cues_of(item_id)
            rows = []
            for i, c in enumerate(cues):
                t = (c.get('text') or '').strip()
                if t:
                    rows.append({'i': i, 'start': float(c.get('start') or 0),
                                 'end': float(c.get('end') or 0), 'text': t})
            if len(rows) < 8:
                return {'ok': False, 'msg': '这个语料句子太少（至少 8 句）才能练大意'}
            skip = max(2, int(len(rows) * 0.1))      # 开场寒暄信息量低，往后跳
            pool = rows[skip:] or rows
            avg_n = sum(len(r['text'].split()) for r in rows) / len(rows)

            def _w(r):
                n = len(r['text'].split())
                if n < avg_n * 0.6:
                    return 0.6
                if n > avg_n * 1.2:
                    return 1.2
                return 1.0

            recent = []
            cut = (datetime.now() - _td(days=7)).strftime('%Y-%m-%d')
            for x in self._load_practice():
                if (x.get('subkind') == 'gist' and str(x.get('itemId')) == str(item_id)
                        and (x.get('at') or '')[:10] >= cut):
                    recent.append((x.get('gistStart'), x.get('gistEnd')))
            win = None
            for _ in range(60):
                s_row = _rd.choices(pool, weights=[_w(r) for r in pool], k=1)[0]
                idx = rows.index(s_row)
                n = _rd.randint(4, 8)
                w = rows[idx:idx + n]
                if len(w) < 4:
                    w = rows[max(0, idx - (4 - len(w))):idx + n]
                if len(w) < 3:
                    continue
                dur = w[-1]['end'] - w[0]['start']
                if dur > 75 and len(w) > 4:
                    w = w[:max(4, int(len(w) * 60 / max(1.0, dur)))]
                s_i, e_i = w[0]['i'], w[-1]['i']
                if any(gs is not None and not (e_i < gs - 2 or s_i > ge + 2)
                       for gs, ge in recent):
                    continue
                win = w
                break
            if not win:
                mid = len(rows) // 2
                win = rows[mid:mid + 6]
            return {'ok': True, 'start': round(win[0]['start'], 2),
                    'end': round(win[-1]['end'] + 0.2, 2),
                    'startIdx': win[0]['i'], 'endIdx': win[-1]['i'],
                    'count': len(win),
                    'duration': round(win[-1]['end'] - win[0]['start'], 1)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def gist_grade(self, item_id, start_idx, end_idx, answer, attempts=1):
        """脱稿大意判卷：LLM 只依据该窗口文稿评 accurate/partial/missed，记录进练习。"""
        try:
            ans = str(answer or '').strip()
            if not ans:
                return {'ok': False, 'msg': '先写一下你听到的大意'}
            _it, cues = self._cues_of(item_id)
            s_i, e_i = int(start_idx), min(int(end_idx), len(cues) - 1)
            win = [cues[i] for i in range(s_i, e_i + 1)]
            en_text = ' '.join((c.get('text') or '').strip() for c in win
                               if (c.get('text') or '').strip())
            zh_text = ''
            try:
                zhv = _audio_sib(_it['audio'], '.zh.vtt')
                if zhv.exists():
                    zc = tl.parse_vtt_cues(zhv.read_text(encoding='utf-8', errors='replace'))
                    s0 = float(win[0].get('start') or 0)
                    e0 = float(win[-1].get('end') or 0)
                    zh_text = ' '.join((c.get('text') or '').strip() for c in zc
                                       if float(c.get('start') or 0) < e0
                                       and float(c.get('end') or 0) > s0)
            except Exception:
                zh_text = ''

            data = {}
            if len(ans) < 15 and any(k in ans for k in
                                     ('不知道', '没听清', '没听懂', '听不出', '没听到')):
                verdict, score = 'missed', 20
                fb = ('没关系——这段确实不好抓。先看下面的文稿读懂它，'
                      '再点「再听一遍」对照着感受一次，你会突然就听清了。')
            else:
                _t, _a, llm = self._services()
                if not llm.ready:
                    return {'ok': False, 'msg': '还没配 LLM'}
                sys_p, _u = prompts.get('gist_grade')
                user = prompts.fmt('gist_grade',
                                   en_text=en_text[:1500],
                                   zh_line=('【文稿参考中文】' + zh_text[:800]) if zh_text else '',
                                   answer=ans[:600])
                data = llm.chat_json(sys_p, user, max_tokens=900, tag='脱稿判卷')
                verdict = str(data.get('verdict') or 'partial').lower()
                if verdict not in ('accurate', 'partial', 'missed'):
                    verdict = 'partial'
                try:
                    score = max(0, min(100, int(data.get('score'))))
                except Exception:
                    score = {'accurate': 90, 'partial': 60, 'missed': 20}[verdict]
                fb = data.get('feedback') or ''
            self._append_practice({
                'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'kind': 'listening', 'subkind': 'gist',
                'itemId': item_id, 'index': s_i,
                'gistStart': s_i, 'gistEnd': e_i,
                'en': en_text[:160], 'heard': ans[:200],
                'accuracy': score, 'verdict': verdict, 'attempts': attempts,
                'wrong': 0, 'missed': 0, 'extra': 0})
            self._bump_item_stats(item_id, s_i, score)
            return {'ok': True, 'verdict': verdict, 'score': score, 'feedback': fb,
                    'keyPoints': data.get('key_points') or [],
                    'covered': data.get('covered') or [],
                    'missed': data.get('missed') or [],
                    'transcript': [{'en': (c.get('text') or '').strip(),
                                    'start': c.get('start')} for c in win],
                    'transcriptZh': zh_text}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def listen_info(self, item_id):
        """听力页初始化：难度分布 + 模式说明。"""
        try:
            _it, cues = self._cues_of(item_id)
            cfg = tl.load_config()
            return {'ok': True, 'stats': listening.stats(cues),
                    'levels': [{'id': k, 'name': n, 'desc': d} for k, n, d in listening.LEVELS],
                    'modes': [
                        {'id': 'dictation', 'name': '全文听写', 'desc': '把整句打出来'},
                        {'id': 'cloze', 'name': '填空模式', 'desc': '只打被挖空的词'},
                        {'id': 'first', 'name': '首字母提示', 'desc': '每个词只给首字母'},
                    ],
                    'asrProvider': cfg.get('asrProvider') or 'dashscope',
                    'ttsProvider': cfg.get('ttsProvider') or 'dashscope',
                    'usesRefAudio': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def listen_sentences(self, item_id, level='', limit=60, offset=0):
        try:
            _it, cues = self._cues_of(item_id)
            rows = listening.pick_sentences(cues, level or None, limit=limit, offset=offset)
            return {'ok': True, 'items': rows, 'stats': listening.stats(cues)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def listen_make(self, item_id, index, mode='dictation', level='', seed=None):
        """按句号 + 模式生成一道题。"""
        try:
            _it, cues = self._cues_of(item_id)
            if index < 0 or index >= len(cues):
                return {'ok': False, 'msg': '句子序号越界'}
            text = (cues[index].get('text') or '').strip()
            ex = listening.build_exercise(text, mode, level or None, seed)
            ex['index'] = index
            ex['start'] = cues[index].get('start')
            ex['end'] = cues[index].get('end')
            return ex
        except Exception as e:
            return {'ok': False, 'msg': str(e)}


    def listen_check(self, item_id, index, user_text, mode='dictation', level='',
                     seed=None, with_llm=True):
        """判分 + （可选）LLM 批改。"""
        try:
            _t, _a, llm = self._services()
            _it, cues = self._cues_of(item_id)
            if index < 0 or index >= len(cues):
                return {'ok': False, 'msg': '句子序号越界'}
            answer = (cues[index].get('text') or '').strip()
            ex = listening.build_exercise(answer, mode, level or None, seed)
            cmp = listening.grade(answer, user_text, mode, ex.get('blanks'))
            score = listening.score_of(cmp)

            diag = None
            if with_llm and llm.ready:
                table = '\n'.join(
                    '  应为「%s」/ 实为「%s」 -> %s' % (x.get('ref') or '-', x.get('got') or '-', x['type'])
                    for x in cmp['items'] if x['type'] != 'ok') or '  （逐词都对上了）'
                try:
                    sys_p, _u = prompts.get('listen_check')
                    user = prompts.fmt('listen_check',
                                       mode=mode, level=ex['levelName'], answer=answer,
                                       user_text=user_text or '（空）', table=table)
                    diag = llm.chat_json(sys_p, user, max_tokens=1800, tag='听写批改')
                except Exception as e:
                    diag = {'error': str(e)}

            rec = {'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                   'kind': 'listening', 'itemId': item_id, 'index': index,
                   'mode': mode, 'level': ex['level'],
                   'en': answer, 'heard': user_text, 'accuracy': cmp['accuracy'],
                   # LLM 判分缺 score 键时回落本地分，别把 None 写进统计（平均分被 None 污染的根因）
                   'score': (diag or {}).get('score') if isinstance(diag, dict) and isinstance((diag or {}).get('score'), (int, float)) else score,
                   'wrong': cmp['wrong'], 'missed': cmp['missed'], 'extra': cmp['extra']}
            self._append_practice(rec)
            self._bump_item_stats(item_id, index, cmp.get('accuracy'))

            return {'ok': True, 'answer': answer, 'compare': cmp, 'score': score,
                    'exercise': ex, 'diag': diag}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ================= 听力 AI 挑战题（三档：普通=挖空选择 / 困难=词块拼句 / 地狱=无提示盲打） =================
    def _quiz_cache(self):
        try:
            d = json.loads((self.app_dir / 'listen_quiz_cache.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _quiz_save(self, d):
        try:
            store.write_json(self.app_dir / 'listen_quiz_cache.json', d)
        except Exception:
            pass

    def listen_quiz(self, item_id, index, level='normal'):
        """给当前句出一道 AI 挑战题。
        normal：LLM 挖空+干扰项（缓存 per 句，答案不下发前端）；
        hard：本地把句子词序打乱成词块（seed 固定，可复现）；
        hell：不返回任何提示（前端盲打后走 listen_check 本地判分）。"""
        try:
            _it, cues = self._cues_of(item_id)
            if not (0 <= index < len(cues)):
                return {'ok': False, 'msg': '句子序号越界'}
            text = (cues[index].get('text') or '').strip()
            if not text:
                return {'ok': False, 'msg': '这句没有文本'}
            key = self._parse_key(text) + '|' + level
            if level == 'normal':
                cache = self._quiz_cache()
                q = cache.get(key)
                if not q:
                    _t, _a, llm = self._services()
                    if not llm.ready:
                        return {'ok': False, 'msg': '普通难度出题需要 LLM（设置 → 对话与纠错）'}
                    sys_p, _u = prompts.get('listen_quiz')
                    user = prompts.fmt('listen_quiz', sentence=text)
                    data = llm.chat_json(sys_p, user, max_tokens=900, tag='听力出题')
                    blanks = data.get('blanks') or []
                    disp = str(data.get('display') or '')
                    if not blanks or '____' not in disp:
                        return {'ok': False, 'msg': '出题失败（LLM 输出不完整），再试一次'}
                    import random as _r
                    for b in blanks:
                        opts = [str(x) for x in (b.get('options') or []) if str(x).strip()]
                        ans = str(b.get('answer') or '').strip()
                        if ans and ans not in opts:
                            opts.append(ans)
                        _r.shuffle(opts)
                        b['options'] = opts[:4]
                    q = {'display': disp, 'blanks': [{'answer': str(b.get('answer') or ''),
                                                       'options': b['options']} for b in blanks]}
                    cache[key] = q
                    self._quiz_save(cache)
                # 答案不下发：判分走 listen_quiz_check（缓存里有答案）
                return {'ok': True, 'level': 'normal', 'index': index, 'text': text,
                        'display': q['display'],
                        'options': [b['options'] for b in q['blanks']],
                        'blankCount': len(q['blanks'])}
            if level == 'hard':
                import random as _r
                words = text.split()
                _r.seed(hash(key) & 0xffffffff)
                pool = words[:]
                _r.shuffle(pool)
                return {'ok': True, 'level': 'hard', 'index': index, 'text': text,
                        'pool': pool, 'count': len(words)}
            return {'ok': True, 'level': 'hell', 'index': index,
                    'count': len(text.split())}   # 不带 text——盲打不能提前见句
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def listen_quiz_check(self, item_id, index, level='normal', answers=None, picked=None):
        """判分（全本地零 token）：
        normal：answers=[每空选的选项] 与缓存里的答案比对（逐空对错）；
        hard：picked=[按序点选的词] 与原句词序比对；
        hell：picked=用户输入的整句 → 逐词比对（口径与听写判分一致）。"""
        try:
            _it, cues = self._cues_of(item_id)
            if not (0 <= index < len(cues)):
                return {'ok': False, 'msg': '句子序号越界'}
            text = (cues[index].get('text') or '').strip()
            if level == 'normal':
                cache = self._quiz_cache()
                q = cache.get(self._parse_key(text) + '|normal')
                if not q:
                    return {'ok': False, 'msg': '题目缓存丢了，重新出题再答'}
                ans_list = [str(x or '') for x in (answers or [])]
                results = []
                for i, b in enumerate(q['blanks']):
                    got = ans_list[i] if i < len(ans_list) else ''
                    results.append({'ok': got.strip() == str(b['answer']).strip(),
                                    'answer': str(b['answer'])})
                score = sum(1 for r in results if r['ok'])
                return {'ok': True, 'results': results,
                        'score': score, 'total': len(results)}
            if level == 'hard':
                import speech_asr as _sa
                cmp = _sa.compare(text, ' '.join(str(x or '') for x in (picked or [])))
                return {'ok': True, 'accuracy': cmp['accuracy'], 'compare': cmp}
            # hell：盲打
            import speech_asr as _sa
            cmp = _sa.compare(text, str(picked or ''))
            return {'ok': True, 'accuracy': cmp['accuracy'], 'compare': cmp}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def listen_read_text(self, item_id, index, preset_id='', provider=''):
        """朗读当前句。走统一引擎（听力页场景可单独指定引擎）。"""
        try:
            eng = self._engine()
            _it, cues = self._cues_of(item_id)
            text = (cues[index].get('text') or '').strip()
            p = eng.synthesize(text, preset_id or None, scene='listen')
            self._refresh_roots()
            return {'ok': True, 'url': self._url(p), 'engine': eng.engine('listen')}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:250]}

    def practice_test(self, which='all'):
        """连通性自检。"""
        tts, asr, llm = self._services()
        out = {}
        if which in ('all', 'tts'):
            out['tts'] = tts.test()
        if which in ('all', 'asr'):
            out['asr'] = asr.test()
        if which in ('all', 'llm'):
            out['llm'] = llm.test()
        if which in ('all', 'rec'):
            ok, msg = speech_asr.Recorder.available()
            out['rec'] = {'ok': ok, 'msg': msg or '录音可用'}
        return {'ok': True, 'results': out}

    # ---------------- 本地服务
    def _refresh_roots(self):
        """把「界面目录 + 所有库目录 + 缓存目录」加入本地服务白名单。"""
        if not self.server:
            return
        roots = [str(_ui_dir()), str(self.cache_dir)] + self.all_dirs()
        self.server.set_roots(roots)

    def _url(self, p):
        if not self.server:
            return ''
        return self.server.url_for(p)

    # ---------------- 基础信息
    def app_info(self):
        # 冻结（打包）后路径最容易跑偏，所以把关键路径一并报出来，方便排查
        import sys as _s
        return {
            'ok': True,
            'ffmpeg': bool(tl.FFMPEG),
            'ffmpegPath': tl.FFMPEG or '',
            'libraryDir': self.cfg.get('libraryDir', ''),
            'extraDirs': self.cfg.get('extraDirs', []),
            'openAfter': bool(self.cfg.get('openFolderAfterDownload')),
            'lastUrl': self.cfg.get('lastUrl', ''),
            'appDir': str(APP_DIR),
            'cfgPath': str(tl.CONFIG),
            'bootTab': getattr(self, 'boot_tab', ''),
            'bootPlay': getattr(self, 'boot_play', ''),
            'diag': {
                'frozen': bool(getattr(_s, 'frozen', False)),
                'meipass': str(getattr(_s, '_MEIPASS', '')),
                'executable': _s.executable,
                'libAppDir': str(tl.APP_DIR),
                'uiDir': str(_ui_dir()),
            },
        }

    def all_dirs(self):
        ds = [self.cfg.get('libraryDir', '')] + list(self.cfg.get('extraDirs', []))
        return [d for d in ds if d]

    # ---------------- ⑤ 来源类型（演讲 / 对话 / 读书 / 播客）
    def _stamp_source_kind(self, folder, slug, log=None):
        """下载完成后把来源类型写进 <slug>.meta.json 的 sourceKind。

        已有值不覆盖（用户手动改过的类型是权威）。分类失败一律静默——
        它是"锦上添花"，不能因为猜类型把下载结果挡住。"""
        try:
            if not folder or not slug:
                return None
            mp = Path(folder) / (slug + '.meta.json')
            data = {}
            if mp.exists():
                try:
                    data = json.loads(mp.read_text(encoding='utf-8')) or {}
                except Exception:
                    data = {}
            old = data.get('sourceKind')
            if old in videokind.RULES:
                if log:
                    log(f'来源类型：{videokind.describe(old)}（沿用已记录）')
                return old
            kind = videokind.classify(data.get('title') or slug, data.get('url') or '', data)
            data['sourceKind'] = kind
            store.write_json(mp, data)
            if log:
                log(f'来源类型：{videokind.describe(kind)} → 记入 meta（{slug}.meta.json）')
            return kind
        except Exception:
            return None

    def video_kinds(self):
        """素材类型清单（前端选择器用）。顺序即 RULES 的声明顺序，标签/说明都在后端一处维护。"""
        try:
            return {'ok': True, 'kinds': [
                {'id': k, 'label': videokind.label(k), 'desc': videokind.describe(k)}
                for k in videokind.RULES]}
        except Exception as e:
            return {'ok': False, 'msg': str(e), 'kinds': []}

    def set_item_kind(self, item_id, kind=''):
        """手动纠正来源类型（分类猜错时的急救口）。写 meta，下次生成字幕/后处理按新类型走。"""
        try:
            kind = (kind or '').strip()
            if kind and kind not in videokind.RULES:
                return {'ok': False, 'msg': '未知类型：' + kind}
            it = None
            for x in tl.scan_library(self.all_dirs()):
                if x.get('id') == item_id:
                    it = x
                    break
            if not it:
                return {'ok': False, 'msg': '找不到该语料：' + str(item_id)}
            mp = Path(it['dir']) / (item_id + '.meta.json')
            data = {}
            if mp.exists():
                try:
                    data = json.loads(mp.read_text(encoding='utf-8')) or {}
                except Exception:
                    data = {}
            if kind:
                data['sourceKind'] = kind
            else:
                data.pop('sourceKind', None)
            store.write_json(mp, data)
            k = data.get('sourceKind') or videokind.classify(
                it.get('title') or item_id, it.get('url') or '', data)
            return {'ok': True, 'kind': k, 'label': videokind.label(k),
                    'desc': videokind.describe(k)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------------- 库
    def scan(self):
        try:
            self._refresh_roots()
            items = tl.scan_library(self.all_dirs())
            return {'ok': True, 'items': items, 'dirs': self.all_dirs()}
        except Exception as e:
            return {'ok': False, 'msg': str(e), 'items': []}

    def get_assets(self, item_id):
        """给播放器用的资源清单（走本地 HTTP，避免 file:// 在 WebView2 里的各种坑）。"""
        items = tl.scan_library(self.all_dirs())
        it = next((x for x in items if x['id'] == item_id), None)
        if not it:
            return {'ok': False, 'msg': '找不到该语料'}
        self._refresh_roots()
        audio = Path(it['audio'])
        en_vtt = _audio_sib(it['audio'], '.en.vtt')
        zh_vtt = _audio_sib(it['audio'], '.zh.vtt')
        bi = audio.parent / (audio.stem + '.对照.md')
        tx = audio.parent / (audio.stem + '.en.txt')
        return {
            'ok': True,
            'item': it,
            'audioUrl': self._url(audio),
            'enVttUrl': self._url(en_vtt) if en_vtt.exists() else '',
            'zhVttUrl': self._url(zh_vtt) if zh_vtt.exists() else '',
            'bilingual': str(bi) if bi.exists() else '',
            'text': str(tx) if tx.exists() else '',
            'dir': str(audio.parent),
            'baseName': audio.stem,
            # 词级时间戳 sidecar（跟读页逐词高亮用）。空串 = 前端不启用词级高亮。
            'wordsUrl': (self._url(_audio_sib(it['audio'], '.words.json'))
                         if _audio_sib(it['audio'], '.words.json').exists() else ''),
        }

    def read_words(self, item_id):
        """读取词级时间戳 sidecar（跟读页卡拉OK/逐词高亮用）。

        .words.json 由 doctor 阶段 3.5 的 local_align 强制对齐产出，结构：
            {'v','src','cueCount','words':[{'w','s','e','i','sc'}]}
        s/e = 相对**原始音频起点**的秒（未叠加校准偏移）；i = 所属句号；sc = CTC 置信度。

        返回 {'ok','words','cueCount','stale'}：
          stale=True 表示 cueCount 与当前英文字幕句数不一致（字幕被改过），
          前端必须忽略这批词级数据，否则词会串到别的句子上。
        """
        try:
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            wp = _audio_sib(it['audio'], '.words.json')
            if not wp.exists():
                return {'ok': True, 'words': [], 'cueCount': 0, 'stale': False}
            try:
                d = json.loads(wp.read_text(encoding='utf-8', errors='replace'))
            except Exception:
                return {'ok': False, 'msg': '.words.json 解析失败（可能写坏）'}
            words = d.get('words') or []
            cc = int(d.get('cueCount') or 0)
            n_cue = 0
            en_vtt = _audio_sib(it['audio'], '.en.vtt')
            if en_vtt.exists():
                try:
                    n_cue = len(tl.parse_vtt_cues(
                        en_vtt.read_text(encoding='utf-8', errors='replace')))
                except Exception:
                    n_cue = 0
            stale = bool(cc and n_cue and cc != n_cue)
            return {'ok': True, 'words': words, 'cueCount': cc, 'stale': stale}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def read_url(self, url, limit=3000000):
        """按本地服务 URL 读文本（字幕/文稿）。

        只允许读本地服务能提供的地址，杜绝被当成任意 URL 抓取器。
        """
        try:
            if not self.server or not url.startswith(self.server.base()):
                return {'ok': False, 'msg': '拒绝：只允许读取本地库内的文件'}
            import urllib.request
            with urllib.request.urlopen(url, timeout=20) as r:
                txt = r.read(limit).decode('utf-8', 'replace')
            return {'ok': True, 'text': txt, 'truncated': len(txt) >= limit}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def read_text(self, path, limit=300000):
        try:
            p = Path(path)
            if not p.exists():
                return {'ok': False, 'msg': '文件不存在'}
            txt = p.read_text(encoding='utf-8', errors='replace')
            return {'ok': True, 'text': txt[:limit], 'truncated': len(txt) > limit}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def save_meta(self, item_id, tags=None, note=None, title=None):
        try:
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            p = tl.set_meta(it, tags=tags, note=note, title=title)
            return {'ok': True, 'path': p}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------------- 文件操作
    def reveal(self, path):
        try:
            tl.reveal_in_explorer(path)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def open_file(self, path):
        try:
            tl.open_path(path)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def open_url(self, url, browser=''):
        """用指定浏览器打开外部网页。browser: ''=系统默认 / edge / chrome / waterfox。"""
        try:
            u = (url or '').strip()
            if not (u.startswith('http://') or u.startswith('https://')):
                return {'ok': False, 'msg': '只支持 http/https 链接'}
            b = (browser or '').strip().lower()
            if b in ('edge', 'chrome', 'waterfox'):
                ok, via = _open_with_browser(u, b)
                if ok:
                    return {'ok': True, 'via': via}
                r = self.open_url(u)                       # 没找到指定浏览器，回落默认
                return {'ok': r.get('ok', False), 'via': 'default',
                        'msg': f'没找到 {b}，已用系统默认打开'}
            ok, via = _open_with_browser(u, '')
            return {'ok': ok, 'via': via}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def browsers_available(self):
        return {'ok': True,
                'edge': bool(_find_browser('edge')),
                'chrome': bool(_find_browser('chrome')),
                'waterfox': bool(_find_browser('waterfox'))}

    def delete_item(self, item_id, remove_audio=True):
        """删除一条语料。

        安全约束：只删「库目录内、且是本语料同名的一组文件」，
        路径必须先通过 is_relative_to 校验，绝不接受任意路径。
        """
        try:
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}

            audio = Path(it['audio']).resolve()
            allowed = [Path(d).resolve() for d in self.all_dirs() if Path(d).exists()]
            if not any(audio.is_relative_to(d) for d in allowed):
                return {'ok': False, 'msg': '拒绝：该文件不在库目录内'}

            base = audio.with_suffix('')
            victims = []
            for f in audio.parent.iterdir():
                if not f.is_file():
                    continue
                stem = f.stem
                if stem == base.name or stem.startswith(base.name + '.'):
                    if not remove_audio and f.suffix.lower() in tl.AUDIO_EXT:
                        continue
                    victims.append(f)
            for f in victims:
                try:
                    f.unlink()
                except Exception:
                    pass
            return {'ok': True, 'removed': [str(v) for v in victims], 'count': len(victims)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------------- 设置
    def set_dirs(self, libraryDir=None, extraDirs=None):
        try:
            if libraryDir is not None:
                p = Path(libraryDir)
                p.mkdir(parents=True, exist_ok=True)
                self.cfg['libraryDir'] = str(p)
            if extraDirs is not None:
                keep = []
                for d in extraDirs:
                    if d and Path(d).exists():
                        keep.append(str(Path(d)))
                self.cfg['extraDirs'] = keep
            tl.save_config(self.cfg)
            return {'ok': True, 'dirs': self.all_dirs()}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def pick_dir(self, title='选择文件夹'):
        try:
            res = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                d = res[0] if isinstance(res, (list, tuple)) else res
                return {'ok': True, 'dir': str(d)}
            return {'ok': False, 'msg': '未选择'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def set_options(self, openAfter=None):
        if openAfter is not None:
            self.cfg['openFolderAfterDownload'] = bool(openAfter)
        tl.save_config(self.cfg)
        return {'ok': True}

    # ---------------- 下载（流式日志）
    def bilibili_probe(self, url):
        """B站合集/多P探测（秒级、零下载）：是合集就返回分集清单给前端勾选。
        非 B站链接或探测失败 → is_collection=False（不挡正常下载）。
        注意：不能叫 download_probe——设置页「测试连通」已有同名方法，会被覆盖。"""
        try:
            url = (url or '').strip()
            if not url.startswith('http'):
                return {'ok': False, 'msg': '请填网址'}
            plat = videosrc.detect_platform(url)
            if plat != 'bilibili':
                return {'ok': True, 'is_collection': False, 'count': 0, 'entries': []}
            r = videosrc.probe_collection(url, log=lambda m: self._plog(m))
            return {'ok': True, **r}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def start_download(self, url, to_library=True, custom_dir='', proxy=None, episodes=None):
        with self._lock:
            if self._dl['running']:
                return {'ok': False, 'msg': '已有任务在下载中'}
            url = (url or '').strip()
            if not url.startswith('http'):
                return {'ok': False, 'msg': '请填一个 TED / B站 / YouTube 视频网址'}
            outdir = self.cfg.get('libraryDir') if to_library else (custom_dir or self.cfg.get('libraryDir'))
            self._dl = {'running': True, 'log': [], 'done': False, 'error': '', 'result': None,
                        'dir': outdir, 'paused': False, 'progress': {}}
            self.cfg['lastUrl'] = url
            if proxy is not None:
                self.cfg['downloadProxy'] = (proxy or '').strip()
            tl.save_config(self.cfg)

        ctrl = {'pause': threading.Event(), 'cancel': False}
        self._dl_ctrl = ctrl
        mode = (self.cfg.get('downloadProxy') or 'auto').strip() or 'auto'

        def log(m):
            self._dl['log'].append(str(m))
            if len(self._dl['log']) > 400:
                self._dl['log'] = self._dl['log'][-300:]

        def resolve_proxy():
            """auto = 自动探测；直连/direct/off = 强制直连；其他 = 显式代理地址。"""
            low = mode.lower()
            if low in ('direct', 'off', 'none', '直连'):
                log('代理：已设为直连')
                return ''
            if low != 'auto':
                log(f'代理：使用指定 {mode}')
                return mode
            import proxydetect
            r = proxydetect.detect_proxy(log=log)
            if r['proxy']:
                tag = '已验证可用' if r['verified'] else '未验证（端口开着）'
                log(f"代理：自动检测（{r['via']}）→ {r['proxy']}（{tag}）")
                return r['proxy']
            log('代理：未检测到本机代理，直连')
            return ''

        def work():
            try:
                plat = videosrc.detect_platform(url)
                if plat:
                    # B站 / YouTube：B站是国内 CDN 永远直连，YouTube 走代理
                    res = videosrc.download_video(
                        url, outdir, log=log, control=ctrl,
                        proxy=('' if plat == 'bilibili' else resolve_proxy()),
                        progress=self._set_progress,
                        asr=self._services()[1], llm=self._services()[2],
                        episodes=(episodes if isinstance(episodes, list) and episodes else None))
                else:
                    # TED 的 HLS 服务器在境外，有代理就走代理（实测 40× 提速）
                    res = tl.download_talk(url, outdir, log=log, control=ctrl,
                                           proxy=resolve_proxy(),
                                           progress=self._set_progress)
                    # TED 无官方中文 → LLM 自动补译（三级兜底第③级，agent 化：状态检查→翻译→复查）
                    try:
                        _audio_p = (res.get('files') or [None])[0] or ((res.get('dir') and None))
                        import videosrc as _vs
                        _zh_missing = False
                        if res.get('dir') and res.get('name'):
                            from pathlib import Path as _P
                            _ap = _P(res['dir']) / (res['name'] + '.mp3')
                            _zv = _ap.parent / (_ap.stem + '.zh.vtt')
                            _ev = _ap.parent / (_ap.stem + '.en.vtt')
                            _need = (_ev.exists() and (not _zv.exists() or _zv.stat().st_size < 64))
                            if _need:
                                log('中文官方字幕缺失 → 启用 LLM 自动补译（AI 补全）…')
                                _en_cues = tl.parse_vtt_cues(_ev.read_text(encoding='utf-8', errors='replace'))
                                _llm = self._services()[2]
                                if _llm and getattr(_llm, 'ready', False):
                                    _zh_cues = _vs._translate_cues(_en_cues, _llm, log, ctrl or {}, None)
                                    if _zh_cues:
                                        tl.write_srt(_ap.parent / (_ap.stem + '.zh.srt'), _zh_cues)
                                        tl.srt_to_vtt(_ap.parent / (_ap.stem + '.zh.srt'), _zv)
                                        log(f'✓ 中文补译完成：{len(_zh_cues)} 句（LLM 翻译）')
                                        try:
                                            _vs.write_bilingual_md(_ap) if hasattr(_vs, 'write_bilingual_md') else None
                                        except Exception:
                                            pass
                                    else:
                                        log('⚠ LLM 补译未返回结果（可稍后用「🩺 诊断修正」补中文）')
                                else:
                                    log('⚠ 未配置 LLM——中文跳过（配置后用「🩺 诊断修正」可补）')
                    except Exception as _ze:
                        log('中文自动补译出错（不影响英文）：' + str(_ze)[:120])
                # 智能收尾（TED/B站统一）：omni 转写质检 + 自动 AI 摘要（下载即成品）
                try:
                    _sid = (res.get('name') or '') or ((res.get('item') or {}).get('id') or '')
                    if _sid:
                        self._smart_finalize(_sid, log=log)
                except Exception:
                    pass
                # ⑤ 来源类型分类（演讲/对话/读书/播客 → 后处理参数不同），记入 meta
                try:
                    _slug = res.get('name') or (res.get('item') or {}).get('id') or ''
                    self._stamp_source_kind(res.get('dir'), _slug, log=log)
                except Exception:
                    pass
                self._dl['result'] = res
                if self.cfg.get('openFolderAfterDownload'):
                    try:
                        tl.open_path(outdir)
                    except Exception:
                        pass
            except Exception as e:
                msg = str(e)
                if '已取消' in msg:
                    self._dl['error'] = '已取消'
                    log('已取消下载')
                else:
                    self._dl['error'] = msg
                    log('失败：' + msg)
            finally:
                self._dl['done'] = True
                self._dl['running'] = False
                self._dl['paused'] = False

        threading.Thread(target=work, daemon=True).start()
        return {'ok': True}

    def _set_progress(self, info):
        self._dl['progress'] = info

    def download_pause(self, on=None):
        """暂停 / 继续当前下载。on 缺省 = 切换。"""
        try:
            if not self._dl['running']:
                return {'ok': False, 'msg': '当前没有进行中的下载'}
            ctrl = getattr(self, '_dl_ctrl', None)
            if not ctrl:
                return {'ok': False, 'msg': '这个任务不支持暂停'}
            new = (not ctrl['pause'].is_set()) if on is None else bool(on)
            if new:
                ctrl['pause'].set()
            else:
                ctrl['pause'].clear()
            self._dl['paused'] = new
            return {'ok': True, 'paused': new}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def download_cancel(self):
        try:
            if not self._dl['running']:
                return {'ok': False, 'msg': '当前没有进行中的下载'}
            ctrl = getattr(self, '_dl_ctrl', None)
            if ctrl:
                ctrl['pause'].clear()   # 若正暂停着，先唤醒让它走到取消分支
                ctrl['cancel'] = True
            return {'ok': True, 'msg': '正在取消…'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def download_status(self, since=0):
        log = self._dl['log']
        return {
            'running': self._dl['running'],
            'done': self._dl['done'],
            'error': self._dl['error'],
            'result': self._dl['result'],
            'paused': self._dl.get('paused', False),
            'progress': self._dl.get('progress') or {},
            'log': log[since:],
            'total': len(log),
        }

    # ================= 本地导入（音频 / 纯文本）+ 语料改名 =================
    _IMPORT_AUDIO = ('.mp3', '.m4a', '.wav', '.flac', '.ogg', '.aac', '.wma',
                     '.opus', '.mp4', '.webm', '.mkv')

    def import_pick(self):
        """选一个本地音频或 .txt 文本（导入用）。"""
        try:
            res = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False,
                file_types=('导入文件 (*.mp3;*.m4a;*.wav;*.flac;*.ogg;*.aac;*.wma;*.opus;*.mp4;*.webm;*.mkv;*.txt)',
                            '所有文件 (*.*)'))
            if res:
                return {'ok': True, 'path': res[0] if isinstance(res, (list, tuple)) else res}
            return {'ok': False, 'msg': '未选择'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def import_media(self, path):
        """导入本地文件入库。

        音频 → 拷贝进语料库目录（无字幕，前端自动触发「⚡生成字幕」管线）；
        纯文本 → LLM 断句+翻译 → 逐句 TTS 合成 → 拼接成整篇 mp3（时间轴=逐句
        合成时长累加，天生精确）。后台线程跑，import_status 轮询进度。
        """
        try:
            src = Path(path)
            if not src.exists():
                return {'ok': False, 'msg': '文件不存在'}
            ext = src.suffix.lower()
            if ext == '.txt':
                kind = 'text'
                runner = self._import_text_run
            elif ext in self._IMPORT_AUDIO:
                kind = 'audio'
                runner = self._import_audio_run
            else:
                return {'ok': False, 'msg': f'不支持的格式 {ext}——支持音频和 .txt 文本'}
            self._imp = {'running': True, 'log': [], 'done': False, 'error': '',
                         'kind': kind, 'item': '', 'progress': {}}
            threading.Thread(target=runner, args=(src,), daemon=True).start()
            return {'ok': True, 'kind': kind}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def import_status(self):
        imp = getattr(self, '_imp', None)
        if not imp:
            return {'ok': True, 'running': False}
        return {'ok': True, 'running': bool(imp.get('running')), 'done': bool(imp.get('done')),
                'error': imp.get('error') or '', 'kind': imp.get('kind') or '',
                'item': imp.get('item') or '', 'log': (imp.get('log') or [])[-40:],
                'progress': imp.get('progress') or {}}

    def _import_audio_run(self, src):
        imp = self._imp

        def say(m):
            imp['log'].append(str(m))
            imp['log'] = imp['log'][-80:]

        try:
            import shutil
            lib = Path(self.cfg.get('libraryDir') or '')
            if not str(lib).strip():
                raise RuntimeError('未设置语料库目录（设置里选一下）')
            lib.mkdir(parents=True, exist_ok=True)
            slug = tl.slugify(src.stem)
            base, dst = slug, lib / (slug + src.suffix.lower())
            i = 2
            while dst.exists():
                slug, dst = f'{base}-{i}', lib / (f'{base}-{i}' + src.suffix.lower())
                i += 1
            shutil.copy2(src, dst)
            say(f'已复制入库：{dst.name}')
            store.write_json(dst.parent / f'{slug}.meta.json',
                             {'title': src.stem.strip(), 'source': 'import'})
            imp['item'] = slug
            self._refresh_roots()
            say('音频导入完成——接下来自动跑「⚡生成字幕」（转写+校对+翻译）')
            imp['done'] = True
            imp['running'] = False
        except Exception as e:
            imp['error'] = str(e)[:200]
            imp['done'] = True
            imp['running'] = False

    def _import_text_run(self, src):
        imp = self._imp

        def say(m):
            imp['log'].append(str(m))
            imp['log'] = imp['log'][-80:]

        def setprog(pct, detail):
            imp['progress'] = {'phase': 'import', 'pct': pct, 'detail': detail}

        try:
            import shutil
            text = src.read_text(encoding='utf-8', errors='replace').strip()
            if len(text) < 20:
                raise RuntimeError('文本太短（至少要有一句话）')
            _t, _a, llm = self._services()
            if not (llm and llm.ready):
                raise RuntimeError('未配置 LLM——文本导入需要它做断句+翻译（设置 → 大模型）')
            lib = Path(self.cfg.get('libraryDir') or '')
            if not str(lib).strip():
                raise RuntimeError('未设置语料库目录（设置里选一下）')
            lib.mkdir(parents=True, exist_ok=True)

            # ① LLM 断句 + 翻译（不改写原文）
            say('LLM 断句+翻译中（整篇一次，长文约半分钟）…')
            setprog(5, 'LLM 断句翻译中')
            sys_p = ('你是英语学习材料的编辑。把用户给的英文文本切成适合跟读练习的句子：'
                     '按标点和语义边界断句，每句 3~18 个词；不改写、不增删内容、不合并超长句。'
                     '每句给自然的中文翻译。只输出 JSON：'
                     '{"sentences":[{"en":"原句","zh":"中文"}]}')
            data = llm.chat_json(sys_p, text[:14000], max_tokens=9000, tag='文本导入')
            sents = [s for s in (data.get('sentences') or [])
                     if (s.get('en') or '').strip()]
            if not sents:
                raise RuntimeError('LLM 没有返回句子（重试一次通常就好）')
            say(f'断句完成：{len(sents)} 句')

            # ② 逐句 TTS（走磁盘缓存，重导入同文本不重复合成）
            voice = self.cfg.get('ttsPreset') or 'narrator'
            files = []
            for i, s in enumerate(sents):
                r = self.tts_speak(s['en'], voice)
                if not r.get('ok'):
                    raise RuntimeError(f'第 {i + 1} 句合成失败：{(r.get("msg") or "")[:80]}')
                files.append(Path(r['file']))
                if i % 5 == 0 or i == len(sents) - 1:
                    setprog(10 + 70 * (i + 1) // len(sents), f'TTS 合成 {i + 1}/{len(sents)} 句')
            say('合成完成，拼接音频…')

            # ③ 拼接：每句尾部补 0.35s 静音（统一重编码），concat demuxer 一次合并
            slug = tl.slugify(src.stem) or 'import'
            base, out = slug, lib / f'{slug}.mp3'
            k = 2
            while out.exists():
                slug, out = f'{base}-{k}', lib / f'{base}-{k}.mp3'
                k += 1
            tmp = lib / f'.{slug}_parts'
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                setprog(85, '拼接音频')
                parts, durs = [], []
                for i, f in enumerate(files):
                    p = tmp / f'p{i:04d}.mp3'
                    r = subprocess.run(
                        [tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                         '-i', str(f), '-af', 'apad=pad_dur=0.35',
                         '-ar', '24000', '-ac', '1', '-b:a', '128k', str(p)],
                        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    if r.returncode != 0 or not p.exists():
                        raise RuntimeError(f'句 {i + 1} 转码失败')
                    parts.append(p)
                    durs.append(float(tl.probe_duration(p) or 0.0))
                lst = tmp / 'list.txt'
                lst.write_text('\n'.join("file '" + str(p).replace("'", "'\\''") + "'"
                                         for p in parts) + '\n', encoding='utf-8')
                r = subprocess.run(
                    [tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                     '-f', 'concat', '-safe', '0', '-i', str(lst), '-c', 'copy', str(out)],
                    capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if r.returncode != 0 or not out.exists():
                    raise RuntimeError('拼接失败' + (r.stderr.decode('utf-8', 'replace')[-120:] or ''))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

            # ④ 字幕落盘：时间轴 = 逐句合成时长累加（天生精确，无需对齐）
            cues, t = [], 0.0
            for s, d in zip(sents, durs):
                cues.append({'start': round(t, 3), 'end': round(t + max(0.4, d - 0.35), 3),
                             'text': (s['en'] or '').strip()})
                t += d
            tl.write_srt(lib / f'{slug}.en.srt', cues)
            tl.srt_to_vtt(lib / f'{slug}.en.srt', lib / f'{slug}.en.vtt')
            zh = [{'start': c['start'], 'end': c['end'], 'text': (s.get('zh') or '').strip()}
                  for c, s in zip(cues, sents)]
            tl.write_srt(lib / f'{slug}.zh.srt', zh)
            tl.srt_to_vtt(lib / f'{slug}.zh.srt', lib / f'{slug}.zh.vtt')
            store.write_json(lib / f'{slug}.meta.json',
                             {'title': src.stem.strip(), 'source': 'import-text',
                              'duration': round(t, 1)})
            imp['item'] = slug
            self._refresh_roots()
            say(f'文本导入完成：{len(sents)} 句 / 音频 {t / 60:.1f} 分钟')
            setprog(100, '完成')
            imp['done'] = True
            imp['running'] = False
        except Exception as e:
            imp['error'] = str(e)[:200]
            imp['done'] = True
            imp['running'] = False

    def rename_item(self, item_id, new_title):
        """语料改名：音频+全部同名 sidecar 一起改，按 id 存的数据（校准/统计/摘要）同步迁移键。"""
        try:
            new_title = (new_title or '').strip()
            if not new_title:
                return {'ok': False, 'msg': '名字不能为空'}
            slug = tl.slugify(new_title)
            if not slug:
                return {'ok': False, 'msg': '名字里没有可用字符'}
            if slug == item_id:
                return {'ok': True, 'newId': slug, 'moved': 0}
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            audio = Path(it['audio'])
            d, old = audio.parent, audio.stem
            # 冲突检测：新名的音频或任一 sidecar 已存在
            for f in d.iterdir():
                if f.name.startswith(slug + '.') or f.name == slug + f.suffix:
                    if not f.name.startswith(old + '.') and f.name != old + f.suffix:
                        return {'ok': False, 'msg': f'已有同名语料：{f.name[:60]}'}
            moved = 0
            for f in list(d.iterdir()):
                if not f.is_file():
                    continue
                # 音频本体（old+ext）或 sidecar（old.xxx）；startswith(old+'.') 对
                # 含点标题安全——要求 old 之后紧跟 '.'，不会把 'abc' 误配到 'abcd.xxx'
                if f.name != old + f.suffix and not f.name.startswith(old + '.'):
                    continue
                nf = d / (slug + f.name[len(old):])
                if nf.exists():
                    continue
                f.rename(nf)
                moved += 1
            if moved == 0:
                return {'ok': False, 'msg': '没找到可改名的文件'}
            # meta 标题更新
            mp = d / (slug + '.meta.json')
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text(encoding='utf-8'))
                    meta['title'] = new_title
                    store.write_json(mp, meta)
                except Exception:
                    pass
            # 按 id 存的 app 数据迁移键（旧名 → 新名）
            for fn in ('calibration.json', 'item_stats.json', 'item_summary.json'):
                p = self.app_dir / fn
                try:
                    j = json.loads(p.read_text(encoding='utf-8'))
                    if isinstance(j, dict) and item_id in j:
                        j[slug] = j.pop(item_id)
                        store.write_json(p, j)
                except Exception:
                    pass
            self._refresh_roots()
            return {'ok': True, 'newId': slug, 'moved': moved}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    # ================= 智能任务（计划 / 打卡 / LLM 排任务） =================
    def plan_today(self):
        return self.plan.today()

    def plan_set_tasks(self, tasks, dailyGoalMin=None):
        return self.plan.set_tasks(tasks, daily_goal_min=dailyGoalMin, source='local')

    def plan_add_task(self, text):
        return self.plan.add_task(text)

    def plan_mark_done(self, task_id, done=True):
        return self.plan.mark_done(task_id, done)

    def plan_set_goal(self, minutes):
        return self.plan.set_goal(minutes)

    def plan_generate(self):
        """让配置的 LLM 基于近期练习数据生成今日任务。"""
        try:
            items = self._load_practice()[-120:]
            recent = [{
                'en': (x.get('en') or '')[:90],
                'accuracy': x.get('accuracy'),
                'itemId': x.get('itemId'),
                'index': x.get('index'),
                'kind': x.get('kind') or 'pron',
            } for x in items[-25:]]
            vocab_due = [v['text'] for v in self.vocab.all()[:200]
                         if not v.get('mastered')][:12]
            lib = []
            try:
                lib = [{'id': it['id'][:60], 'title': (it.get('title') or '')[:40]}
                       for it in tl.scan_library()[:40]]
            except Exception:
                pass
            sys_p = ('你是英语学习规划师。根据学员近 25 条练习、待复习生词和语料列表，'
                     '安排今天 4-6 项可执行任务。输出 JSON：{"tasks":[{"text":"任务一句话(中文,含做什么+语料名)",'
                     '"itemId":"语料id(可选,须来自列表)","index":句子序号(可选数字)}]}。'
                     '要求：优先练准确率最低的句子/语料；至少 1 项生词复习；至少 1 项听力；'
                     '总量匹配 20 分钟左右；不要编造列表外的 itemId。')
            r = self._services()[2].chat_json(sys_p,
                                              '练习记录：' + json.dumps(recent, ensure_ascii=False)
                                              + '\n待复习生词：' + json.dumps(vocab_due, ensure_ascii=False)
                                              + '\n语料列表：' + json.dumps(lib, ensure_ascii=False),
                                              max_tokens=900, tag='llm_task_plan')
            tasks = r.get('tasks') if isinstance(r, dict) else None
            if not tasks:
                return {'ok': False, 'msg': 'LLM 未返回有效任务，稍后再试'}
            valid_ids = {x['id'] for x in lib}
            clean = []
            for t in tasks[:6]:
                text = str(t.get('text') or '').strip()
                if not text:
                    continue
                iid = t.get('itemId') if t.get('itemId') in valid_ids else None
                idx = t.get('index')
                clean.append({'text': text[:80],
                              'itemId': iid,
                              'index': int(idx) if isinstance(idx, (int, float)) and 0 <= int(idx) < 500 else None})
            if not clean:
                return {'ok': False, 'msg': 'LLM 任务为空'}
            self.plan.set_tasks(clean, source='llm')
            return {'ok': True, 'count': len(clean), 'tasks': clean}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def stats_dashboard(self):
        """数据中心看板：KPI + 14天趋势 + 练习构成 + 生词进度。"""
        try:
            items = self._load_practice()
            from datetime import timedelta as _td
            days = []
            for i in range(13, -1, -1):
                d = (datetime.now() - _td(days=i)).strftime('%Y-%m-%d')
                day_items = [x for x in items if (x.get('at') or '')[:10] == d]
                accs = [x.get('accuracy') for x in day_items if isinstance(x.get('accuracy'), (int, float))]
                days.append({'date': d, 'count': len(day_items),
                             'avgAccuracy': round(sum(accs) / len(accs), 1) if accs else None})
            pron = sum(1 for x in items if not x.get('kind'))
            listen = sum(1 for x in items if x.get('kind') == 'listening')
            invalid = sum(1 for x in items if x.get('kind') == 'invalid')
            vocab = self.vocab.stats()
            t = self.plan.today()
            recent_acc = [x.get('accuracy') for x in items[-100:]
                          if isinstance(x.get('accuracy'), (int, float))]
            return {'ok': True,
                    'kpi': {'total': len(items), 'streak': self.plan.streak(),
                            'days14': sum(d['count'] for d in days),
                            'avgAccuracy': round(sum(recent_acc) / len(recent_acc), 1)
                                           if recent_acc else 0,
                            'invalid': invalid},
                    'trend': days,
                    'mix': {'pron': pron, 'listen': listen, 'invalid': invalid},
                    'vocab': vocab,
                    'today': {'done': t.get('doneCount', 0), 'total': t.get('totalCount', 0)},
                    'heat': self.plan.heatmap(12),
                    }
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def plan_boot_remind(self):
        """启动提醒：昨天有任务没完成且没练习。每天最多弹一次。"""
        try:
            pend = self.plan.should_remind()
            if pend:
                self.plan.mark_reminded()
                return {'ok': True, 'remind': True,
                        'tasks': [t.get('text') for t in pend][:5],
                        'streak': self.plan.streak()}
            return {'ok': True, 'remind': False}
        except Exception as e:
            return {'ok': False, 'remind': False, 'msg': str(e)}

    # ================= 句子收藏 / 句法解析 =================
    def _favs(self):
        try:
            if self._fav_file.exists():
                d = json.loads(self._fav_file.read_text(encoding='utf-8'))
                return d if isinstance(d, list) else []
        except Exception:
            pass
        return []

    def _save_favs(self, favs):
        store.write_json(self._fav_file, favs)

    def sentence_fav_add(self, text, zh='', item_id='', index=-1, kind='sentence', gspec=None,
                         bump=True):
        """bump=重复收藏时 count 是否 +1：程序内真实重复遇到=True；
        用户手动点收藏按钮的重复点击=False（幂等，不刷计数）。"""
        text = (text or '').strip()
        if not text:
            return {'ok': False, 'msg': '内容为空'}
        favs = self._favs()
        for i, f in enumerate(favs):        # 同文本去重：移到最前（bump 时计数+1）
            if f.get('text') == text and f.get('kind') == (kind or 'sentence'):
                if bump:
                    f['count'] = f.get('count', 1) + 1
                favs.insert(0, favs.pop(i))
                self._save_favs(favs)
                return {'ok': True, 'fid': f['fid'], 'existed': True, 'bumped': bool(bump)}
        fid = str(int(time.time() * 1000))
        entry = {'fid': fid, 'text': text[:300], 'zh': (zh or '')[:300],
                 'itemId': item_id or '', 'index': index,
                 'kind': kind or 'sentence', 'addedAt':
                 datetime.now().strftime('%Y-%m-%d %H:%M')}
        if kind == 'grammar' and isinstance(gspec, dict):
            # 语法收藏：带公式/解释/例句/句中片段，详情卡展示+可跳出处
            entry['gs'] = {'name_zh': str(gspec.get('name_zh') or '')[:60],
                           'name_en': str(gspec.get('name_en') or '')[:80],
                           'pattern': str(gspec.get('pattern') or '')[:120],
                           'explain_zh': str(gspec.get('explain_zh') or '')[:500],
                           'example': str(gspec.get('example') or '')[:300],
                           'span': str(gspec.get('span') or '')[:200]}
        favs.insert(0, entry)
        self._save_favs(favs[:500])
        return {'ok': True, 'fid': fid}

    def sentence_fav_list(self):
        return {'ok': True, 'items': self._favs()}

    def sentence_fav_remove(self, fid):
        favs = [f for f in self._favs() if f.get('fid') != str(fid)]
        self._save_favs(favs)
        return {'ok': True}

    def sentence_fav_parse(self, fid, force=False):
        """收藏句的解析（带缓存，C4 修复）：首次真调 LLM 并把结果存进收藏条目，
        之后秒回不再花钱等待；force=True 强制重析覆盖缓存。
        语法收藏（kind=grammar）自带讲解结构，不适用。"""
        try:
            fid = str(fid)
            favs = self._favs()
            f = next((x for x in favs if x.get('fid') == fid), None)
            if not f:
                return {'ok': False, 'msg': '收藏不存在（可能已删除）'}
            if f.get('kind') == 'grammar':
                return {'ok': False, 'msg': '语法收藏自带讲解，不需要句法解析'}
            if not force and isinstance(f.get('parse'), dict) and (f['parse'].get('grammar') is not None):
                return {'ok': True, 'fid': fid, 'parse': f['parse'], 'cached': True,
                        'parseAt': f.get('parseAt') or ''}
            r = self.sentence_parse(f.get('text') or '', zh=f.get('zh') or '',
                                    item_id=f.get('itemId') or '',
                                    index=int(f.get('index') or -1))
            if not r.get('ok'):
                return r
            f['parse'] = r.get('parse') or {}
            f['parseAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            self._save_favs(favs)
            return {'ok': True, 'fid': fid, 'parse': f['parse'], 'cached': False,
                    'parseAt': f['parseAt']}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def favorite_stats(self):
        """导航角标：句子收藏数 / 单词本总数 / 今日待复习数。"""
        try:
            favs = self._favs()
            today = datetime.now().strftime('%Y-%m-%d')
            items = self.vocab.all()
            due = sum(1 for x in items
                      if not x.get('mastered') and (x.get('nextReview') or '0000') <= today)
            return {'ok': True, 'sentences': len(favs),
                    'vocab': len(items), 'due': due}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def sentence_parse(self, en, zh='', prev_en='', next_en='', item_id='', index=-1):
        """固定模板：解析句子的语法结构 / 词汇要点 / 组成方式。

        人工修正优先：parse_overrides.json 里有这句的修正版就直接用（edited=True），
        「恢复 AI 版」走 parse_override_clear。

        Bug #5 修复：支持两种传入上下文的方式：
        1. 直接传 prev_en/next_en（练习页面已有这些信息）
        2. 传 item_id/index，后端自动从字幕文件获取前后句（语料库页面）
        """
        try:
            key = self._parse_key(en)
            ov = self._parse_overrides().get(key)
            if ov:
                return {'ok': True, 'parse': ov, 'edited': True}
            _t, _a, llm = self._services()
            if not llm.ready:
                return {'ok': False, 'msg': '还没配 LLM（设置 → 对话与纠错）'}
            sys_p, _u = prompts.get('sentence_parse')

            # Bug #5 修复：如果没有直接传入上下文，尝试从字幕文件获取
            # （走 _cues_of 统一口径——句子组装层生效后直读 raw 会拿错邻句）
            if (not prev_en and not next_en) and item_id and index >= 0:
                try:
                    _it2, ctx_cues = self._cues_of(item_id)
                    if 0 <= index < len(ctx_cues):
                        prev_en = ctx_cues[index - 1]['text'] if index > 0 else ''
                        next_en = ctx_cues[index + 1]['text'] if index < len(ctx_cues) - 1 else ''
                except Exception:
                    pass  # 获取失败也不影响主流程，只是没有上下文而已

            # 构建上下文行
            context_line = ''
            if prev_en or next_en:
                ctx_parts = []
                if prev_en:
                    ctx_parts.append('前句：' + str(prev_en).strip())
                if next_en:
                    ctx_parts.append('后句：' + str(next_en).strip())
                context_line = '\n【上下文】' + ' | '.join(ctx_parts)

            user = prompts.fmt('sentence_parse', en=str(en or '').strip(),
                               zh_line=('（中文大意：' + str(zh) + '）') if zh else '',
                               context_line=context_line)
            data = llm.chat_json(sys_p, user, max_tokens=1800, tag='句法解析')
            self._sanitize_parse_html(data)
            return {'ok': True, 'parse': data}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    @staticmethod
    def _sanitize_parse_html(data):
        """LLM 文本字段的 HTML 白名单清洗：只留 <b><mark><u>，其余标签全剥（防注入）。"""
        import re as _re
        if not isinstance(data, dict):
            return data

        def clean(s):
            s = str(s or '')
            s = _re.sub(r'<(script|style)\b[^>]*>.*?</\1\s*>', '', s,
                        flags=_re.I | _re.S)                     # 脚本/样式整块删（含内容）
            s = _re.sub(r'</?(?!/?(?:b|mark|u)\b)[^>]*>', '', s)  # 其余非白名单标签剥壳
            return s[:600]

        for g in (data.get('grammar') or []):
            if isinstance(g, dict):
                g['explain_zh'] = clean(g.get('explain_zh'))
        for p in (data.get('phrases') or []):
            if isinstance(p, dict):
                p['meaning_zh'] = clean(p.get('meaning_zh'))
        for v in (data.get('vocab') or []):
            if isinstance(v, dict):
                v['note'] = clean(v.get('note'))
        for k in ('structure', 'how'):
            data[k] = clean(data.get(k))
        return data

    @staticmethod
    def _parse_key(text):
        import re as _re
        return _re.sub(r'\s+', ' ', str(text or '').strip().lower())

    def _parse_overrides(self):
        try:
            d = json.loads((self.app_dir / 'parse_overrides.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def parse_override_save(self, en, data):
        """保存人工修正的解析结果（整卡覆盖，按规范化句子文本为键）。"""
        try:
            if not isinstance(data, dict):
                return {'ok': False, 'msg': '修正内容格式不对'}
            data = dict(data)
            data['schemaVersion'] = 2
            data['editedAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            self._sanitize_parse_html(data)
            ovs = self._parse_overrides()
            ovs[self._parse_key(en)] = data
            store.write_json(self.app_dir / 'parse_overrides.json', ovs)
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def parse_override_clear(self, en):
        """删除人工修正，恢复 AI 解析。"""
        try:
            ovs = self._parse_overrides()
            removed = ovs.pop(self._parse_key(en), None)
            store.write_json(self.app_dir / 'parse_overrides.json', ovs)
            return {'ok': True, 'removed': bool(removed)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ================= 音标训练 =================
    def phonemes(self):
        try:
            import phonemes_data as pd
            prog = {}
            try:
                if self._phon_file.exists():
                    prog = json.loads(self._phon_file.read_text(encoding='utf-8'))
            except Exception:
                prog = {}
            return {'ok': True, 'cats': pd.CATS, 'items': pd.PHONEMES, 'progress': prog}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def phoneme_master(self, pid, mastered=True):
        try:
            prog = {}
            try:
                if self._phon_file.exists():
                    prog = json.loads(self._phon_file.read_text(encoding='utf-8'))
            except Exception:
                prog = {}
            if mastered:
                prog[pid] = datetime.now().strftime('%Y-%m-%d %H:%M')
            else:
                prog.pop(pid, None)
            store.write_json(self._phon_file, prog)
            return {'ok': True, 'mastered': sum(1 for _ in prog)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ================= 生词本记忆卡（艾宾浩斯） =================
    SRS_STEPS = [1, 2, 4, 7, 15, 30]

    def vocab_due(self):
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            items = [x for x in self.vocab.all()
                     if not x.get('mastered')
                     and (x.get('nextReview') or '0000') <= today]
            return {'ok': True, 'count': len(items), 'items': items[:50]}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def vocab_review(self, vid, rating):
        """rating: again=不记得 / hard=不熟 / good=掌握。艾宾浩斯推进。"""
        try:
            steps = self.SRS_STEPS
            items = self.vocab.all()
            it = next((x for x in items if x.get('id') == str(vid)), None)
            if not it:
                return {'ok': False, 'msg': '找不到该条目'}
            stage = int(it.get('srsStage') or 0)
            if rating == 'again':
                stage = 0
            elif rating == 'hard':
                stage = stage                      # 停留本级
            else:                                  # good
                stage = min(stage + 1, len(steps) - 1)
            from datetime import timedelta
            nxt = (datetime.now() + timedelta(days=steps[stage])).strftime('%Y-%m-%d')
            self.vocab.update(vid, srsStage=stage, nextReview=nxt,
                              lastRating=rating, lastReviewDate=
                              datetime.now().strftime('%Y-%m-%d %H:%M'),
                              reviewCount=(it.get('reviewCount') or 1) + 1)
            return {'ok': True, 'srsStage': stage, 'nextReview': nxt,
                    'intervalDays': steps[stage]}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def analyze_fragment(self, text, context=''):
        """划选片段分析：判定是短语/习语/从句/残缺，给含义与运用场景。

        用户拍板：默认走查词释义专用 API（文本短、要求低），失败不回落主模型。
        """
        text = (text or '').strip()
        if not text or len(text) > 500:
            return {'ok': False, 'msg': '片段长度需在 2~500 字符'}
        try:
            coach = self._coach(lookup=True)
            if not coach.llm.ready:
                return {'ok': False, 'msg': '还没配 LLM'}
            data = coach.analyze_fragment(text, context)
            return {'ok': True, 'analysis': data, 'text': text}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def usage_overview(self):
        """Token 用量页：按模型 / 按功能 / 按日聚合 + 最近调用。"""
        try:
            import collections
            entries = []
            p = self.app_dir / 'usage_log.jsonl'
            if p.exists():
                for line in p.read_text(encoding='utf-8').splitlines():
                    try:
                        e = json.loads(line)
                        if isinstance(e, dict) and e.get('model'):
                            entries.append(e)
                    except Exception:
                        continue
            today = datetime.now().strftime('%Y-%m-%d')
            from datetime import timedelta as _td
            d7cut = (datetime.now() - _td(days=7)).strftime('%Y-%m-%d')
            def agg(rows):
                import pricing as _pricing
                models = {}
                feats = collections.defaultdict(int)
                days = {}
                tk = 0
                cost = 0.0
                unpriced = 0
                overrides = self.cfg.get('pricingOverrides') or []
                base_llm = (self.cfg.get('llmBaseUrl') or '')
                for e in rows:
                    pt, ct = (e.get('prompt') or 0), (e.get('completion') or 0)
                    tk += pt + ct
                    model = e.get('model') or '?'
                    models.setdefault(model, {'tokens': 0, 'calls': 0, 'cost': None, 'currency': 'CNY'})
                    models[model]['tokens'] += pt + ct
                    models[model]['calls'] += 1
                    feats[e.get('tag') or '其他'] += 1
                    c, cur, _cid, priced = _pricing.estimate(model, base_llm, pt, ct, overrides)
                    d = (e.get('at') or '')[:10]
                    dd = days.setdefault(d, {'tokens': 0, 'calls': 0, 'cost': 0.0})
                    dd['tokens'] += pt + ct
                    dd['calls'] += 1
                    if priced:
                        models[model]['cost'] = round(models[model]['cost'] + c, 4) if models[model]['cost'] is not None else round(c, 4)
                        models[model]['currency'] = cur
                        cost += c
                        dd['cost'] += c
                    else:
                        unpriced += 1
                return {'tokens': tk, 'models': models, 'features': dict(feats),
                        'cost': round(cost, 4), 'unpriced': unpriced,
                        'daily': {k: {'tokens': v['tokens'], 'calls': v['calls'],
                                      'cost': round(v['cost'], 4)}
                                  for k, v in sorted(days.items())}}
            today_rows = [e for e in entries if (e.get('at') or '')[:10] == today]
            d7_rows = [e for e in entries if (e.get('at') or '')[:10] >= d7cut]
            import pricing as _pricing_v
            price_version = max((ch.get('checked_at') or '')
                                for ch in _pricing_v.PRICING['channels'].values())
            try:
                budget = float(self.cfg.get('usageBudget') or 0)
            except Exception:
                budget = 0.0
            a_all = agg(entries)
            month = today[:7]
            month_cost = sum(v['cost'] for k, v in a_all['daily'].items()
                             if k.startswith(month))
            return {'ok': True, 'today': agg(today_rows), 'd7': agg(d7_rows),
                    'all': a_all, 'calls': len(entries),
                    'recent': list(reversed(entries[-60:])),
                    'daily': a_all['daily'], 'monthCost': round(month_cost, 4),
                    'budget': budget, 'priceVersion': price_version}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def usage_clear(self):
        try:
            p = self.app_dir / 'usage_log.jsonl'
            if p.exists():
                p.unlink()
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------- 按语料(item)的练习统计 ----------
    def _item_stats(self):
        try:
            d = json.loads((self.app_dir / 'item_stats.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _bump_item_stats(self, item_id, index, accuracy=None):
        """按语料聚合练习数据（item_stats.json 增量更新，practice.json 不动）。"""
        try:
            if not item_id:
                return
            st = self._item_stats()
            it = st.setdefault(str(item_id), {'rounds': 0, 'sentences': {}, 'accs': []})
            it['lastIndex'] = index
            it['lastAt'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            it['rounds'] = int(it.get('rounds') or 0) + 1
            s = (it['sentences']).setdefault(str(index), {'attempts': 0, 'best': None})
            s['attempts'] += 1
            if isinstance(accuracy, (int, float)):
                s['best'] = round(max(s['best'] or 0, accuracy), 1)
                accs = it.get('accs') or []
                accs.append(round(accuracy, 1))
                it['accs'] = accs[-30:]
            store.write_json(self.app_dir / 'item_stats.json', st)
        except Exception:
            pass

    def item_stats_get(self, item_id=''):
        """item_id 给空返回全部语料的轻量 map（语料库徽标用），否则返回单条详情。"""
        st = self._item_stats()
        if item_id:
            return {'ok': True, 'stats': st.get(str(item_id))}
        lite = {k: {'lastIndex': v.get('lastIndex'), 'rounds': v.get('rounds'),
                    'lastAt': v.get('lastAt')} for k, v in st.items()}
        return {'ok': True, 'map': lite}

    # ---------- 单词发音 / 批量合成 / TTS 缓存 ----------
    def speak_word(self, text):
        """单词/短语朗读：强制 Edge en-US（绕过场景引擎覆盖），失败退当前可用引擎。"""
        try:
            eng = self._engine()
            path, via = eng.synthesize_word(str(text or '').strip())
            self._refresh_roots()
            return {'ok': True, 'url': self._url(path), 'file': path, 'via': via}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def tts_speak_batch(self, texts, preset_id=None, scene=''):
        """批量合成（连读用）。最多 60 条，已缓存的秒回。"""
        try:
            texts = [str(t or '').strip() for t in (texts or []) if str(t or '').strip()][:60]
            if not texts:
                return {'ok': False, 'msg': '没有可合成的句子'}
            urls = []
            for t in texts:
                r = self.tts_speak(t, preset_id, scene=scene)
                if not r.get('ok'):
                    return {'ok': False, 'msg': r.get('msg') or '合成失败'}
                urls.append(r['url'])
            return {'ok': True, 'urls': urls, 'count': len(urls)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def tts_cache_info(self):
        try:
            d = self.cache_dir / 'tts'
            n, size = 0, 0
            if d.exists():
                for f in d.iterdir():
                    if f.is_file():
                        n += 1
                        size += f.stat().st_size
            # 旧版散在 cache/ 根下的 tts_*.wav 一并统计
            if self.cache_dir.exists():
                for f in self.cache_dir.glob('tts_*.wav'):
                    n += 1
                    size += f.stat().st_size
            return {'ok': True, 'count': n, 'size': size}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def tts_cache_clear(self):
        try:
            import shutil as _sh
            d = self.cache_dir / 'tts'
            if d.exists():
                _sh.rmtree(d, ignore_errors=True)
            if self.cache_dir.exists():
                for f in self.cache_dir.glob('tts_*.wav'):
                    try:
                        f.unlink()
                    except OSError:
                        pass
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------- 诊断：错误日志 + 诊断包 ----------
    def log_error(self, kind, msg, where=''):
        """前端异常落盘（error.log，JSONL），供诊断包带出。"""
        try:
            line = json.dumps({'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                               'kind': str(kind or 'js'),
                               'where': str(where or '')[:200],
                               'msg': str(msg or '')[:500]},
                              ensure_ascii=False)
            with open(self.app_dir / 'error.log', 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            return {'ok': True}
        except Exception:
            return {'ok': False}

    def error_log_recent(self, n=50):
        try:
            p = self.app_dir / 'error.log'
            if not p.exists():
                return {'ok': True, 'items': []}
            items = []
            for ln in p.read_text(encoding='utf-8').splitlines()[-int(n):]:
                try:
                    items.append(json.loads(ln))
                except Exception:
                    continue
            return {'ok': True, 'items': items}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def export_diag(self):
        """导出诊断包到程序目录：环境 + 配置（密钥脱敏）+ 最近错误 + 数据规模。"""
        try:
            cfg = dict(tl.load_config())
            for k in ('mimoApiKey', 'dashscopeApiKey', 'llmApiKey'):
                if cfg.get(k):
                    cfg[k] = str(cfg[k])[:6] + '…(已隐藏)'
            prof = dict((cfg.get('llmProfiles') or {}).get('lookup') or {})
            if prof.get('apiKey'):
                prof['apiKey'] = str(prof['apiKey'])[:6] + '…(已隐藏)'
            if prof:
                cfg['llmProfiles'] = {'lookup': prof}
            try:
                info = self.app_info()
            except Exception:
                info = {}
            n_prac = len(self._load_practice())
            n_fav = len(self._favs())
            n_vocab = len(self.vocab.all())
            errs = self.error_log_recent(80).get('items') or []
            lines = ['听与读诊断包', '生成时间：' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                     '',
                     '== 环境 ==', json.dumps(info, ensure_ascii=False, indent=1),
                     '', '== 配置（密钥已脱敏） ==', json.dumps(cfg, ensure_ascii=False, indent=1),
                     '', '== 数据规模 ==',
                     f'练习 {n_prac} 条 · 收藏 {n_fav} 条 · 单词 {n_vocab} 个',
                     '', '== 最近错误（最多 80 条） ==',
                     json.dumps(errs, ensure_ascii=False, indent=1) if errs else '（无错误记录）']
            out = self.app_dir / ('诊断包_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.txt')
            store.write_text(out, '\n'.join(lines))
            return {'ok': True, 'path': str(out)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------- 收藏标签 ----------
    def sentence_fav_tag(self, fid, tags):
        """给句子收藏打标签（最多 8 个），数据中心之外的轻组织能力。"""
        try:
            favs = self._favs()
            clean = [str(t).strip() for t in (tags or []) if str(t).strip()][:8]
            for f in favs:
                if f.get('fid') == str(fid):
                    f['tags'] = clean
                    self._save_favs(favs)
                    return {'ok': True, 'tags': clean}
            return {'ok': False, 'msg': '找不到该收藏'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------- 音同步校准 ----------
    def _calibration(self):
        try:
            d = json.loads((self.app_dir / 'calibration.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def get_calibration(self, item_id=''):
        """语料级字幕偏移（毫秒）。正=字幕滞后/音提前，播放时 start/end 都加它。"""
        cal = self._calibration()
        if item_id:
            return {'ok': True, 'offsetMs': int(cal.get(str(item_id)) or 0)}
        return {'ok': True, 'map': {k: int(v) for k, v in cal.items()}}

    def save_calibration(self, item_id, offset_ms):
        try:
            off = max(-5000, min(5000, int(float(offset_ms))))
            cal = self._calibration()
            cal[str(item_id)] = off
            store.write_json(self.app_dir / 'calibration.json', cal)
            return {'ok': True, 'offsetMs': off}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def sentence_adjust(self, item_id, index, delta_ms):
        """单句校准：只平移第 index 句的 start/end 并落盘（修复"单句校准带歪全语料"）。

        ⏱ 整体同步（save_calibration）平移的是整条语料——修"下载字幕与音频有
        固定时差"的场景；本接口只动这一句（en），中文侧按原时间窗重叠同步平移。
        4 个字幕文件同步重写（.srt 为源 → .vtt，doctor 同款链）。
        回滚备份走 .bak2（单代覆盖），不碰 doctor 的 .bak（转写出生版）。
        """
        try:
            d = max(-3000.0, min(3000.0, float(delta_ms))) / 1000.0
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            en_vtt = _audio_sib(it['audio'], '.en.vtt')
            if not en_vtt.exists():
                return {'ok': False, 'msg': '没有英文字幕，无法单句校准'}
            cues = tl.parse_vtt_cues(en_vtt.read_text(encoding='utf-8', errors='replace'))
            if not cues:
                return {'ok': False, 'msg': '字幕解析为空'}
            index = int(index)
            if index < 0 or index >= len(cues):
                return {'ok': False, 'msg': '句子序号越界'}
            c = cues[index]
            old_s, old_e = float(c['start']), float(c['end'])
            ns, ne = max(0.0, old_s + d), max(0.0, old_e + d)
            clamped = False
            if index > 0 and ns < float(cues[index - 1]['end']):      # 不越过前句
                ns = float(cues[index - 1]['end']); clamped = True
            if index < len(cues) - 1 and ne > float(cues[index + 1]['start']):   # 不吃进后句
                ne = float(cues[index + 1]['start']); clamped = True
            if ne - ns < 0.2:                                          # 挤压保底
                ne = ns + 0.2; clamped = True
            eff = ns - old_s
            if abs(eff) < 0.001 and abs(ne - old_e) < 0.001:
                return {'ok': True, 'index': index, 'start': old_s, 'end': old_e,
                        'deltaMs': 0, 'clamped': False, 'zhMoved': 0}
            c['start'], c['end'] = ns, ne

            outdir = Path(it['audio']).parent
            stem = Path(it['audio']).stem

            def _backup(p):
                if p.exists():
                    try:
                        p.with_suffix(p.suffix + '.bak2').write_bytes(p.read_bytes())
                    except Exception:
                        pass

            for suffix in ('.en.vtt', '.en.srt'):
                _backup(outdir / (stem + suffix))
            tl.write_srt(outdir / f'{stem}.en.srt', cues)
            tl.srt_to_vtt(outdir / f'{stem}.en.srt', outdir / f'{stem}.en.vtt')

            zh_moved = 0
            zh_vtt = _audio_sib(it['audio'], '.zh.vtt')
            if zh_vtt.exists():
                zh = tl.parse_vtt_cues(zh_vtt.read_text(encoding='utf-8', errors='replace'))
                for z in zh:
                    # 新旧窗口并集：正向调 +d 命中的集合 == 反向调回 -d 命中的集合（往返复原）
                    if (z['start'] < old_e and z['end'] > old_s) \
                            or (z['start'] < ne and z['end'] > ns):
                        z['start'] = float(z['start']) + eff
                        z['end'] = float(z['end']) + eff
                        zh_moved += 1
                if zh_moved:
                    for suffix in ('.zh.vtt', '.zh.srt'):
                        _backup(outdir / (stem + suffix))
                    tl.write_srt(outdir / f'{stem}.zh.srt', zh)
                    tl.srt_to_vtt(outdir / f'{stem}.zh.srt', outdir / f'{stem}.zh.vtt')
            self._refresh_roots()
            return {'ok': True, 'index': index, 'start': round(ns, 3), 'end': round(ne, 3),
                    'deltaMs': round(eff * 1000), 'clamped': clamped, 'zhMoved': zh_moved}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def _cues_of_item(self, item_id):
        """按 id 取语料与英文字幕 cues（合并/拆分/校准共用的取数）。

        必须按 start 排好序再动：有的源字幕是滚动式（同句反复出现、时间重叠）或
        顺序错乱，按文件顺序「并入上一句」会把时间上并不相邻的两句粘在一起，
        表现就是用户说的「合完前面莫名多出一句」。"""
        items = tl.scan_library(self.all_dirs())
        it = next((x for x in items if x['id'] == item_id), None)
        if not it:
            return None, None, '找不到该语料'
        en_vtt = _audio_sib(it['audio'], '.en.vtt')
        if not en_vtt.exists():
            return it, None, '没有英文字幕，无法逐句编辑'
        cues = tl.parse_vtt_cues(en_vtt.read_text(encoding='utf-8', errors='replace'))
        if not cues:
            return it, None, '字幕解析为空'
        cues = [c for c in cues if (c.get('text') or '').strip()]
        starts = [float(c['start']) for c in cues]
        if starts != sorted(starts):
            cues.sort(key=lambda c: (float(c['start']), float(c['end'])))
        return it, cues, ''

    @staticmethod
    def _frag_ratio(cues):
        """碎片化粗判：句尾无句末标点、且下一条以小写开头的占比。

        第11问实测：94 分钟朗读落盘的 .en.vtt 有 21.6% 这种句尾、26.2% 下一条小写起头
        ——一条完整句被拦腰切成两三条。这个比例是用来决定"落盘前要不要再组装一遍"的。
        """
        if not cues:
            return 0.0
        n = bad = 0
        for i, c in enumerate(cues):
            text = (c.get('text') or '').strip()
            if not text:
                continue
            n += 1
            core = text.rstrip().rstrip('\'"\u2019\u201d\uff09\u300b\u300d').rstrip()
            if core.endswith(('.', '!', '?', '\u2026', '\u3002', '\uff01', '\uff1f')):
                continue
            nxt = ''
            if i + 1 < len(cues):
                nxt = (cues[i + 1].get('text') or '').lstrip()
            if nxt[:1].islower():
                bad += 1
        return bad / float(n or 1)

    def _selfheal_cues(self, cues, kind=None, log=None):
        """落盘前兜底组装：碎片化超过 6% 就再跑一遍语义段落组装，并写清楚日志。

        为什么要在写入侧兜底：读取侧（`_cues_of`）本来就有组装兜底，可 .对照.md、
        字幕视图、导出的 SRT 都是直接读文件的——只有落盘的是好句子，用户看到的
        才是好句子。任何生成路径（含老构建、旁支脚本）只要漏了组装层，这里补上。
        """
        try:
            ratio = self._frag_ratio(cues)
        except Exception:
            return cues
        if ratio <= 0.06:
            return cues
        try:
            if log:
                log('检测到字幕碎片化（%.0f%% 的句子结尾没有句末标点、下一条接着小写），'
                    '落盘前自动按语义段落重组装…' % (ratio * 100))
            if hasattr(videosrc, 'resegment_cues'):
                fixed = videosrc.resegment_cues(cues, kind=kind, log=log)
            else:
                fixed = videosrc.assemble_sentences(cues)
            fixed = [c for c in (fixed or []) if (c.get('text') or '').strip()]
            if fixed and len(fixed) > 1:
                if log:
                    log('自动重组装完成：%d 句 → %d 句' % (len(cues), len(fixed)))
                return fixed
        except Exception as e:
            if log:
                log('自动重组装失败（%s），按原样落盘' % str(e)[:80])
        return cues

    @staticmethod
    def _doctor_split_params(it):
        """校准/切句的切分上限：按来源类型取（videokind.rules）。

        以前四个调用点全写死 max_words=16 / max_dur=9.0。16 词比一句普通英文句子还短，
        结果把完整句子按逗号剁成一条条——用户第11问说的"死板切成一条条"就是这里来的。
        """
        try:
            import videokind as _vk
            r = _vk.rules(((it or {}).get('sourceKind') or '').strip() or None)
            return int(r.get('max_words') or 45), float(r.get('max_dur') or 12.0)
        except Exception:
            return 45, 12.0

    def _write_en_with_backup(self, it, cues):
        outdir = Path(it['audio']).parent
        stem = Path(it['audio']).stem

        def _backup(p):
            if p.exists():
                try:
                    p.with_suffix(p.suffix + '.bak2').write_bytes(p.read_bytes())
                except Exception:
                    pass

        for suffix in ('.en.vtt', '.en.srt'):
            _backup(outdir / (stem + suffix))
        tl.write_srt(outdir / f'{stem}.en.srt', cues)
        tl.srt_to_vtt(outdir / f'{stem}.en.srt', outdir / f'{stem}.en.vtt')

    def _remap_words_i(self, it, mapper, new_count):
        """合并/拆分后重映射 .words.json 的句号 i（mapper(旧i,词时间)->新i 或 None 丢弃）。"""
        wp = _audio_sib(it['audio'], '.words.json')
        if not wp.exists():
            return
        try:
            wj = json.loads(wp.read_text(encoding='utf-8'))
            ws = wj.get('words') or []
            for w in ws:
                ni = mapper(w.get('i'), float(w.get('s') or 0))
                if ni is None:
                    w['drop'] = True
                else:
                    w['i'] = ni
            wj['words'] = [w for w in ws if not w.get('drop')]
            wj['cueCount'] = new_count
            store.write_json(wp, wj)
        except Exception:
            wp.unlink(missing_ok=True)     # sidecar 坏了不如没有（doctor 会重建）

    def sentence_merge(self, item_id, index):
        """句级手动合并（用户排期①）：把第 index 句并入上一句。

        文本空格拼接、时间窗=上句 start ~ 本句 end（音频窗口自然合并）；
        中文侧落在该窗内的也一并合成一条（否则显示时「按时间重叠拼接」会把两句中文
        都塞进这一句，看着就像"没真合并"）；词级 sidecar 重映射句号。
        """
        try:
            it, cues, err = self._cues_of_item(item_id)
            if err:
                return {'ok': False, 'msg': err}
            index = int(index)
            if index < 1 or index >= len(cues):
                return {'ok': False, 'msg': '已经是第一句，前面没有可合并的句子'}
            prev, cur = cues[index - 1], cues[index]
            merged_text = ((prev.get('text') or '').strip() + ' '
                           + (cur.get('text') or '').strip()).strip()
            prev['text'] = merged_text
            prev['end'] = max(float(prev['end']), float(cur['end']))
            win_s, win_e = float(prev['start']), float(prev['end'])
            del cues[index]
            self._write_en_with_backup(it, cues)
            # 中文同步合并：以前只动英文字幕，合并后这句的时间窗横跨两条中文，
            # 显示时「按时间重叠拼接」就把两句中文都塞进同一句（用户看到的“多出一句”
            # 观感来源之一）。中文窗口落在合并窗内的也合成一条，中英保持一一对应。
            zh_merged = self._merge_zh_in_window(it, win_s, win_e)
            self._remap_words_i(it, lambda i, s: (index - 1 if i == index
                                                  else (i - 1 if i > index else i)),
                                len(cues))
            self._refresh_roots()
            return {'ok': True, 'index': index - 1, 'total': len(cues),
                    'text': merged_text[:120], 'zhMerged': zh_merged}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def _merge_zh_in_window(self, it, win_s, win_e):
        """把 .zh.vtt 里时间窗落在 [win_s, win_e] 的中文句合成一条（英中合并后调用）。

        只处理「确实落在窗内」的句子；窗外的中文时间不动，避免整体漂移。
        返回被合并掉的中文条数（0 = 没动中文）。"""
        zh_vtt = _audio_sib(it['audio'], '.zh.vtt')
        if not zh_vtt.exists():
            return 0
        try:
            zh = tl.parse_vtt_cues(zh_vtt.read_text(encoding='utf-8', errors='replace'))
        except Exception:
            return 0
        if not zh:
            return 0
        hits = [z for z in zh
                if float(z['start']) < win_e - 0.05 and float(z['end']) > win_s + 0.05]
        if len(hits) < 2:
            return 0
        try:
            first = hits[0]
            first['text'] = ''.join((z.get('text') or '').strip() for z in hits)
            first['end'] = max(float(z['end']) for z in hits)
            drop = {id(z) for z in hits[1:]}
            zh = [z for z in zh if id(z) not in drop]
            outdir = Path(it['audio']).parent
            stem = Path(it['audio']).stem
            for suffix in ('.zh.vtt', '.zh.srt'):
                p = outdir / (stem + suffix)
                if p.exists():
                    try:
                        p.with_suffix(p.suffix + '.bak2').write_bytes(p.read_bytes())
                    except Exception:
                        pass
            tl.write_srt(outdir / f'{stem}.zh.srt', zh)
            tl.srt_to_vtt(outdir / f'{stem}.zh.srt', outdir / f'{stem}.zh.vtt')
            return len(hits) - 1
        except Exception:
            return 0

    def sentence_split(self, item_id, index, at_words):
        """句级手动拆分：第 index 句从第 at_words 个词后拆成两句。

        拆分时间点优先用词级时间戳（.words.json 该句第 at_words+1 个词的起点），
        没有则按词数比例插值。中文不动；词级 sidecar 按时间归属重映射。
        """
        try:
            it, cues, err = self._cues_of_item(item_id)
            if err:
                return {'ok': False, 'msg': err}
            index = int(index)
            if index < 0 or index >= len(cues):
                return {'ok': False, 'msg': '句子序号越界'}
            c = cues[index]
            words = (c.get('text') or '').split()
            at_words = int(at_words)
            if at_words < 1 or at_words >= len(words):
                return {'ok': False, 'msg': f'拆分点要在 1~{len(words) - 1} 个词之间（这句共 {len(words)} 词）'}
            s0, e0 = float(c['start']), float(c['end'])
            # 拆点：词级时间戳优先
            t = None
            wp = _audio_sib(it['audio'], '.words.json')
            if wp.exists():
                try:
                    wj = json.loads(wp.read_text(encoding='utf-8'))
                    cands = [w for w in (wj.get('words') or []) if w.get('i') == index]
                    if len(cands) > at_words:
                        t = float(cands[at_words]['s'])
                except Exception:
                    t = None
            if t is None or not (s0 < t < e0):
                t = s0 + (e0 - s0) * at_words / len(words)
            front = {'start': s0, 'end': round(t, 3), 'text': ' '.join(words[:at_words])}
            back = {'start': round(t, 3), 'end': e0, 'text': ' '.join(words[at_words:])}
            cues[index] = front
            cues.insert(index + 1, back)
            self._write_en_with_backup(it, cues)
            self._remap_words_i(it, lambda i, s: (i + 1 if i > index
                                                  else (index + 1 if (i == index and s >= t) else i)),
                                len(cues))
            self._refresh_roots()
            return {'ok': True, 'index': index, 'total': len(cues),
                    'front': front['text'][:60], 'back': back['text'][:60]}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:150]}

    def item_resegment(self, item_id):
        """离线重切句（第11问）：把已有 .en.vtt 按「语义段落」重组装，不重跑语音识别、零费用。

        为什么需要：长视频/整本书朗读的 ASR 是按停顿出句的，实测 94 分钟朗读 1230 条里
        21.5% 结尾没有句末标点、20.7% 下一条以小写/连词起头——句子被拦腰切断；反过来
        讲解类又会一条一句、失去段落感。用户要的是「一整段含义一条」。
        这里复用 videosrc 的组装层（碎片合并 → 句末拆分 → 含义段落合并 → 只在语义弱
        边界切超长 → 静音吸附），英文重建后中文按时间重叠重映射、.对照.md 同步重建。
        词级 sidecar（.words.json）与新句号对不上就丢弃，doctor 会按需重建。
        """
        try:
            it, cues, err = self._cues_of_item(item_id)
            if err:
                return {'ok': False, 'msg': err}
            kind = (it.get('sourceKind') or '').strip() or None
            dur = float(it.get('duration') or 0) or None
            log = []

            def _l(msg):
                log.append(str(msg))

            try:
                new = videosrc.resegment_cues(cues, audio_path=it.get('audio'),
                                              duration=dur, kind=kind, log=_l)
            except AttributeError:
                new = videosrc.assemble_sentences(cues)     # 老版本兜底：只做碎片合并
            new = [c for c in (new or []) if (c.get('text') or '').strip()]
            if not new:
                return {'ok': False, 'msg': '重组装结果为空，未改动任何文件'}
            if len(new) == len(cues) and all(
                    (a.get('text') or '').strip() == (b.get('text') or '').strip()
                    for a, b in zip(new, cues)):
                return {'ok': True, 'changed': False, 'total': len(new),
                        'msg': '当前字幕已经是语义段落，不需要重切', 'log': log[-6:]}
            before = len(cues)
            self._write_en_with_backup(it, new)
            zh_n = self._reflow_zh_to_windows(it, new)
            self._try_write_bilingual(it, new)
            wp = _audio_sib(it['audio'], '.words.json')
            try:
                if wp.exists():
                    wj = json.loads(wp.read_text(encoding='utf-8'))
                    if int(wj.get('cueCount') or -1) != len(new):
                        wp.unlink(missing_ok=True)
            except Exception:
                try:
                    wp.unlink(missing_ok=True)
                except Exception:
                    pass
            self._refresh_roots()
            return {'ok': True, 'changed': True, 'before': before, 'total': len(new),
                    'zhCues': zh_n, 'msg': '已重切：%d 条 → %d 条' % (before, len(new)),
                    'log': log[-8:]}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:180]}

    @staticmethod
    def _zh_split_pieces(text):
        """中文按句末标点切块（重映射到多条英文句时按块分配，尽量不切半句）。"""
        t = (text or '').strip()
        if not t:
            return []
        parts, buf = [], ''
        for ch in t:
            buf += ch
            if ch in '。！？；!?;':
                parts.append(buf.strip())
                buf = ''
        if buf.strip():
            parts.append(buf.strip())
        return parts or [t]

    def _reflow_zh_to_windows(self, it, new_cues):
        """把旧 .zh.vtt 按时间重叠重新映射到新的英文句窗口（重切句后调用）。

        规则：每条旧中文句**只归给与它重叠最多的那一条新英文句**（完全没重叠的
        归给起点最近的一条），再把归到同一窗口的中文按时间顺序拼回去。
        这样中文不丢、不重复，也不会出现「一句英文配两段不相干中文」的错位
        ——第一版按窗口取样实测 1032 条中文只剩 821 条，就是丢了。
        返回新中文条数（0 = 没有中文或失败）。
        """
        zh_vtt = _audio_sib(it['audio'], '.zh.vtt')
        if not zh_vtt.exists():
            return 0
        try:
            old = tl.parse_vtt_cues(zh_vtt.read_text(encoding='utf-8', errors='replace'))
        except Exception:
            return 0
        if not old:
            return 0
        import bisect
        old.sort(key=lambda z: float(z.get('start') or 0))
        wins = sorted(new_cues, key=lambda c: float(c.get('start') or 0))
        if not wins:
            return 0
        starts = [float(c.get('start') or 0) for c in wins]
        buckets = [[] for _ in wins]
        for z in old:
            zs = float(z.get('start') or 0)
            ze = float(z.get('end') or 0)
            k = max(0, bisect.bisect_right(starts, zs) - 1)
            best, best_ov = None, 0.0
            while k < len(wins) and starts[k] < max(ze, zs + 0.01):
                ov = min(ze, float(wins[k].get('end') or 0)) - max(zs, starts[k])
                if ov > best_ov:
                    best, best_ov = k, ov
                k += 1
            if best is None:                # 中文落在句间静音里：给起点最近的一条
                best = min(range(len(wins)), key=lambda q: abs(starts[q] - zs))
            text = (z.get('text') or '').strip()
            if text:
                buckets[best].append((zs, text))
        out = []
        for c, items in zip(wins, buckets):
            if not items:
                continue
            items.sort(key=lambda x: x[0])
            text = ''.join(t for _s, t in items).strip()
            if text:
                out.append({'start': round(float(c.get('start') or 0), 3),
                            'end': round(float(c.get('end') or 0), 3),
                            'text': text})
        if not out:
            return 0
        outdir = Path(it['audio']).parent
        stem = Path(it['audio']).stem
        for suffix in ('.zh.vtt', '.zh.srt'):
            p = outdir / (stem + suffix)
            if p.exists():
                try:
                    p.with_suffix(p.suffix + '.bak2').write_bytes(p.read_bytes())
                except Exception:
                    pass
        tl.write_srt(outdir / (stem + '.zh.srt'), out)
        tl.srt_to_vtt(outdir / (stem + '.zh.srt'), outdir / (stem + '.zh.vtt'))
        return len(out)

    def _try_write_bilingual(self, it, en_cues):
        """重切后同步重建 .对照.md（对照阅读页看到的就是新句子）。失败不影响主流程。"""
        try:
            zh = []
            zp = _audio_sib(it['audio'], '.zh.vtt')
            if zp.exists():
                zh = tl.parse_vtt_cues(zp.read_text(encoding='utf-8', errors='replace'))
            outdir = Path(it['audio']).parent
            stem = Path(it['audio']).stem
            tl.write_bilingual(outdir / (stem + '.对照.md'), en_cues, zh,
                               it.get('title') or stem,
                               '重切句子于 %s（仅重组装，未重跑语音识别）'
                               % datetime.now().strftime('%Y-%m-%d %H:%M'))
            return True
        except Exception:
            return False

    # ---------- 语料 AI 摘要 ----------
    def _summaries(self):
        try:
            d = json.loads((self.app_dir / 'item_summary.json').read_text(encoding='utf-8'))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _clip_subtitle(cues, max_chars=9000):
        """超长字幕截断：前 25% + 中段 30% + 后 35%，接缝标注。"""
        texts = [(c.get('text') or '').strip() for c in cues or []]
        texts = [t for t in texts if t]
        full = ' '.join(texts)
        if len(full) <= max_chars:
            return full, False
        n_head = int(max_chars * 0.25)
        n_mid = int(max_chars * 0.30)
        n_tail = max_chars - n_head - n_mid
        mid0 = (len(full) - n_mid) // 2
        return (full[:n_head] + ' [——中间部分省略——] '
                + full[mid0:mid0 + n_mid] + ' [——中间部分省略——] '
                + full[-n_tail:]), True

    def item_summary_get_all(self):
        """语料库卡片用：已生成摘要的轻量 map（不触发生成）。"""
        cache = self._summaries()
        lite = {k: {'summary': v.get('summary'), 'keywords': v.get('keywords') or [],
                    'scene': v.get('scene') or '', 'topics': v.get('topics') or []}
                for k, v in cache.items()}
        return {'ok': True, 'map': lite}

    def item_summary(self, item_id, force=False):
        """语料库 AI 摘要：这份演讲大概讲什么（item_summary.json 缓存，字幕没变不重花）。"""
        try:
            _t, _a, llm = self._services()
            if not llm.ready:
                return {'ok': False, 'msg': '还没配 LLM'}
            _it, cues = self._cues_of(item_id)
            texts = [(c.get('text') or '').strip() for c in cues]
            texts = [t for t in texts if t]
            if not texts:
                return {'ok': False, 'msg': '这个语料没有英文字幕（可先在卡片上点「⚡生成字幕」）'}
            import hashlib as _hl
            sub_hash = _hl.sha1(' '.join(texts).encode('utf-8')).hexdigest()[:12]
            cache = self._summaries()
            ent = cache.get(str(item_id))
            pv = prompts.version_of('item_summary')
            if ent and not force and ent.get('subtitleHash') == sub_hash                     and ent.get('promptVersion') == pv:
                return {'ok': True, **ent, 'cached': True}
            clipped, truncated = self._clip_subtitle(cues)
            sys_p, _u = prompts.get('item_summary')
            user = prompts.fmt('item_summary', clipped=clipped)
            data = llm.chat_json(sys_p, user, max_tokens=1000, tag='语料摘要')
            ent = {'scene': str(data.get('scene') or '')[:40],
                   'topics': [str(t)[:12] for t in (data.get('topics') or [])][:4],
                   'summary': str(data.get('summary') or '')[:800],
                   'keywords': [str(k)[:30] for k in (data.get('keywords') or [])][:8],
                   'audience': str(data.get('audience') or '')[:80],
                   'subtitleHash': sub_hash, 'promptVersion': pv, 'truncated': truncated,
                   'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M')}
            if ent['summary']:
                cache[str(item_id)] = ent
                store.write_json(self.app_dir / 'item_summary.json', cache)
            return {'ok': True, **ent, 'cached': False}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ---------- 缺字幕语料：ASR 转写 + LLM 翻译补生成 ----------
    def _quarantine_16k(self, log=None):
        """把库目录里历史残留的 .16k 中间文件移到 cache/discard 隔离（不删除）。

        曾因中间 wav 落在语料目录被当成语料，产出 .16k.16k.* 孤儿字幕。
        """
        moved = 0
        try:
            discard = self.app_dir / 'cache' / 'discard'
            discard.mkdir(parents=True, exist_ok=True)
            for d in self.all_dirs():
                p = Path(d)
                if not p.is_dir():
                    continue
                for f in p.iterdir():
                    if not f.is_file():
                        continue
                    # .16k 中间音频 + .16k.16k.* 双后缀孤儿字幕（67集那批历史产物）一起隔离
                    if f.stem.endswith('.16k') or '.16k.16k.' in f.name:
                        try:
                            f.rename(discard / f.name)
                            moved += 1
                        except OSError:
                            pass
            if moved and log:
                log(f'已隔离 {moved} 个历史残留的 .16k 中间文件（移到 cache/discard）')
        except Exception:
            pass
        return moved

    # ================= 使用帮助：环境检测 / 缺失安装 =================
    def env_check(self):
        """小白友好环境体检：ffmpeg / WebView2 / 网络 / 库目录，缺啥给装法。"""
        items = []
        # ① ffmpeg（音频转码/切片/合成核心）
        ff = tl.find_ffmpeg()
        ver = ''
        if ff:
            try:
                import subprocess as _sp
                r = _sp.run([ff, '-version'], capture_output=True, text=True, timeout=10,
                            creationflags=getattr(_sp, 'CREATE_NO_WINDOW', 0))
                ver = (r.stdout or '').split(chr(10))[0][:60]
            except Exception:
                ver = ''
        items.append({'key': 'ffmpeg', 'name': 'FFmpeg（音频处理核心）',
                      'ok': bool(ff), 'detail': ver or '未找到——下载/转写/合成全都依赖它',
                      'fix': 'winget install Gyan.FFmpeg',
                      'fixUrl': 'https://www.gyan.dev/ffmpeg/builds/'})
        # ② WebView2（界面引擎，Win10/11 一般自带）
        wv = False
        try:
            import winreg as _wr
            for root in (_wr.HKEY_LOCAL_MACHINE, _wr.HKEY_CURRENT_USER):
                for sub in (r'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                            r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'):
                    try:
                        with _wr.OpenKey(root, sub) as k:
                            _wr.QueryValueEx(k, 'pv'); wv = True; break
                    except OSError:
                        pass
                if wv:
                    break
        except Exception:
            wv = True   # 检测不了就假定有（窗口能打开=有）
        items.append({'key': 'webview2', 'name': 'WebView2 运行时（界面引擎）',
                      'ok': wv, 'detail': '已安装' if wv else '缺失（界面打不开的根源）',
                      'fix': '从微软官网下载 Evergreen 安装包',
                      'fixUrl': 'https://developer.microsoft.com/microsoft-edge/webview2/'})
        # ③ 网络（下载与 API 调用）
        net_ok, net_detail = True, ''
        try:
            import urllib.request as _ur
            _ur.urlopen('https://www.baidu.com', timeout=5)
            net_detail = '直连正常'
        except Exception:
            try:
                import proxydetect
                port = proxydetect.detect()
                if port:
                    net_detail = f'直连受限，检测到代理 127.0.0.1:{port}'
                else:
                    net_ok, net_detail = False, '无法联网（检查网络或代理）'
            except Exception:
                net_ok, net_detail = False, '无法联网'
        items.append({'key': 'net', 'name': '网络连接', 'ok': net_ok, 'detail': net_detail,
                      'fix': 'TED/YouTube 下载需要代理；B站直连即可', 'fixUrl': ''})
        # ④ 语料库目录可写
        lib_ok, lib_detail = True, ''
        try:
            d = (self.cfg.get('libraryDir') or '').strip()
            if d:
                p4 = Path(d)
                p4.mkdir(parents=True, exist_ok=True)
                probe = p4 / '.write_test'
                probe.write_text('x', encoding='utf-8'); probe.unlink()
                lib_detail = d
            else:
                lib_ok, lib_detail = False, '未设置（设置 → 语料与目录）'
        except Exception as e:
            lib_ok, lib_detail = False, f'不可写：{str(e)[:60]}'
        items.append({'key': 'lib', 'name': '语料库目录', 'ok': lib_ok, 'detail': lib_detail,
                      'fix': '换个有写权限的目录', 'fixUrl': ''})
        return {'ok': True, 'items': items}

    def env_install_ffmpeg(self):
        """一键安装 ffmpeg（winget，后台跑 + env_install_status 轮询）。"""
        if getattr(self, '_env_inst', None) and self._env_inst.get('running'):
            return {'ok': False, 'msg': '已在安装中'}
        self._env_inst = {'running': True, 'done': False, 'log': [], 'code': None}

        def work():
            try:
                import subprocess as _sp
                self._env_inst['log'].append('运行 winget install Gyan.FFmpeg …（首次可能要下载几十 MB）')
                r = _sp.run(['winget', 'install', '--id', 'Gyan.FFmpeg', '-e',
                             '--accept-source-agreements', '--accept-package-agreements'],
                            capture_output=True, text=True, timeout=1800,
                            creationflags=getattr(_sp, 'CREATE_NO_WINDOW', 0))
                self._env_inst['code'] = r.returncode
                tail = ((r.stdout or '') + (r.stderr or '')).strip().split(chr(10))[-3:]
                self._env_inst['log'].extend(tail)
                self._env_inst['log'].append('完成——请重启应用后重新检测（Path 环境变量重启后生效）')
            except Exception as e:
                self._env_inst['log'].append('失败：' + str(e)[:150])
            finally:
                self._env_inst['running'] = False
                self._env_inst['done'] = True

        threading.Thread(target=work, daemon=True).start()
        return {'ok': True}

    def env_install_status(self):
        j = getattr(self, '_env_inst', None) or {}
        return {'ok': True, **j}

    # ================= 智能下载收尾（下载即成品：转写质检 + 自动摘要） =================
    def _omni_audio_check(self, audio, cues, log=None):
        """omni 转写质检：抽头/中/尾三段 15s 音频，让 qwen3.8-omni-flash 复述内容，
        与字幕文本做词重叠比对——防静默漏转（61 批 omni 漏 85% 事故的检测器）。
        返回 (通过段数, 详情)。"""
        import base64 as _b64
        import subprocess as _sp
        key = (self.cfg.get('dashscopeApiKey') or '').strip()
        if not key or not getattr(tl, 'FFMPEG', ''):
            return None, '未配 DashScope Key（质检跳过）'
        dur = tl.probe_duration(audio) or 0
        if dur < 30:
            return None, '音频过短（质检跳过）'
        import random
        random.seed(42)
        marks = [2.0, dur * 0.5, max(2.0, dur - 17)]
        ok_n, details = 0, []
        for mi, m in enumerate(marks):
            try:
                import tempfile as _tf
                td = _tf.mkdtemp(prefix='ted_qc_')
                seg = Path(td) / 's.wav'
                r = _sp.run([tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                             '-ss', str(m), '-t', '15', '-i', str(audio),
                             '-ac', '1', '-ar', '16000', str(seg)],
                            capture_output=True, creationflags=getattr(_sp, 'CREATE_NO_WINDOW', 0))
                if r.returncode != 0 or not seg.exists():
                    continue
                b = _b64.b64encode(seg.read_bytes()).decode()
                import urllib.request as _ur
                import json as _js
                body = {'model': 'qwen3.8-omni-flash', 'messages': [{'role': 'user', 'content': [
                    {'type': 'input_audio', 'input_audio': {'data': 'data:audio/wav;base64,' + b}},
                    {'type': 'text', 'text': 'Please transcribe this audio clip verbatim. English text only.'}]}]}
                req = _ur.Request('https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
                                  data=_js.dumps(body).encode(), method='POST',
                                  headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
                with _ur.urlopen(req, timeout=120) as resp:
                    j = _js.loads(resp.read())
                said = (((j.get('choices') or [{}])[0].get('message') or {}).get('content') or '')
                if isinstance(said, list):
                    said = ' '.join(str(x.get('text', '')) if isinstance(x, dict) else str(x) for x in said)
                # 与该窗口字幕比对词重叠
                w0, w1 = m, m + 15
                sub_text = ' '.join((c.get('text') or '') for c in cues if c['end'] > w0 and c['start'] < w1)
                ns = lambda t: set(w.lower().strip(".,!?'\"") for w in t.split() if len(w) > 2)
                a_w, s_w = ns(said), ns(sub_text)
                cov = len(a_w & s_w) / max(1, len(a_w)) if a_w else 0
                passed = cov >= 0.4
                ok_n += 1 if passed else 0
                details.append({'pos': round(m), 'cov': round(cov, 2), 'ok': passed})
                if log:
                    log(('✓' if passed else '⚠') + f' 质检段{mi + 1}（{int(m)}s处）：词重叠率 {cov:.0%}' + ('' if passed else '——疑似漏转！'))
                try:
                    import shutil as _sh
                    _sh.rmtree(td, ignore_errors=True)
                except Exception:
                    pass
            except Exception as e:
                details.append({'pos': round(m), 'err': str(e)[:60]})
        return ok_n, details

    def _smart_finalize(self, item_id, log=None):
        """智能下载收尾：omni 转写质检（3 抽样）+ 自动 AI 摘要（场景/话题直接进卡片）。"""
        try:
            if self.cfg.get('smartDownload') is False:
                if log:
                    log('智能收尾已关闭（设置→下载）')
                return
            it, cues = self._cues_of(item_id)
            if not cues:
                return
            if log:
                log('智能收尾：omni 转写质检（抽 3 段听音频比对）…')
            ok_n, details = self._omni_audio_check(it['audio'], cues, log)
            if ok_n is not None and details and isinstance(ok_n, int):
                if ok_n >= 2:
                    if log:
                        log(f'✓ 转写质检通过（{ok_n}/{len([d for d in details if "err" not in d])} 段词重叠达标）')
                else:
                    if log:
                        log('⚠ 转写质检不合格——下载完成后请点「🩺 诊断修正」（对齐环节会用词级时间轴重新定稿）')
            # 自动摘要（scene/topics 进卡片）
            try:
                r = self.item_summary(item_id, force=True)
                if r.get('ok') and log:
                    log('✓ AI 摘要已生成（场景/话题直接显示在卡片上）')
            except Exception as e:
                if log:
                    log('摘要跳过：' + str(e)[:80])
        except Exception as e:
            if log:
                log('智能收尾出错（不影响下载成果）：' + str(e)[:120])

    # ================= 🤖 AI 补全（agent 式：查状态→定方案→执行→复查循环） =================
    def agent_polish(self, item_id):
        """Agent 补全语料：LLM 在循环里决定补什么（翻译缺口/重译段），执行后复查，
        直到自检通过或步数用尽。面向"下载后不完整"（如 TED 无官方中文）场景。
        后台线程 + agent_status 轮询（前端 TaskFloat 展示步骤日志）。"""
        if getattr(self, '_agent_job', None) and self._agent_job.get('running'):
            return {'ok': False, 'msg': '已有 AI 补全任务在跑'}
        self._agent_job = {'running': True, 'done': False, 'error': '', 'steps': [], 'item': item_id}

        def say(m):
            self._agent_job['steps'].append(m)
            self._plog('[AI补全] ' + str(m))

        def work():
            try:
                import videosrc as vs
                it, cues = self._cues_of(item_id)
                audio = Path(it['audio'])
                zhp = _audio_sib(it['audio'], '.zh.vtt')
                zh = tl.parse_vtt_cues(zhp.read_text(encoding='utf-8', errors='replace')) if zhp.exists() else []
                # zh 与 en 按时间重叠配对，找出无译句
                have = set()
                for z in zh:
                    best, bo = -1, -1.0
                    for i, c in enumerate(cues):
                        ov = min(z['end'], c['end']) - max(z['start'], c['start'])
                        if ov > bo:
                            bo, best = ov, i
                    if best >= 0 and bo > 0.3:
                        have.add(best)
                missing = [i for i in range(len(cues)) if i not in have]
                llm = self._services()[2]
                if not llm or not getattr(llm, 'ready', False):
                    raise RuntimeError('未配置 LLM（设置 → 对话与纠错）')
                say(f'状态：英文 {len(cues)} 句，已译 {len(cues) - len(missing)} 句，缺口 {len(missing)} 句')
                # Agent 循环：每轮让 LLM 看缺口决定范围（≤6 轮）
                zh_out = {z_i: z for z_i, z in zip(have, zh)} if False else {}
                zh_full = list(zh)
                for round_n in range(1, 7):
                    if not missing:
                        say('✓ 自检通过：全部英文句已有中文')
                        break
                    plan_sys = ('你是字幕补全 agent。给出本轮翻译计划 JSON：{"from":起始句号,"to":结束句号}。'
                                '规则：从缺口里连续取 ≤40 句；句号为英文句数组下标；只输出 JSON。')
                    m0 = missing[0]
                    plan = llm.chat_json(plan_sys + chr(10) + '全部缺口句号：' + str(missing[:80]), '请给计划',
                                         max_tokens=200, tag='agent_plan')
                    a = max(0, int(plan.get('from', m0)))
                    b = min(len(cues) - 1, int(plan.get('to', m0 + 39)))
                    say(f'第 {round_n} 轮：翻译句 {a}~{b}（{b - a + 1} 句）')
                    seg = cues[a:b + 1]
                    seg_zh = vs._translate_cues(seg, llm, say, {}, None)
                    if not seg_zh:
                        say('⚠ 本轮翻译失败，重试一次')
                        seg_zh = vs._translate_cues(seg, llm, say, {}, None)
                    if not seg_zh:
                        raise RuntimeError('翻译连续失败（LLM 无返回）')
                    # 合并：按时间段插入（简单实现=重建 zh 全量：已有 zh + 新段）
                    zh_full = [z for z in zh_full]
                    zh_full.extend(seg_zh)
                    zh_full.sort(key=lambda x: x['start'])
                    # 落盘（每轮都落，中途取消也有成果）
                    srt_p = audio.parent / (audio.stem + '.zh.srt')
                    tl.write_srt(srt_p, zh_full)
                    tl.srt_to_vtt(srt_p, zhp)
                    # 复查
                    have |= set(range(a, b + 1))
                    missing = [i for i in range(len(cues)) if i not in have]
                    say(f'累计：{len(have)}/{len(cues)} 句已译')
                # 对照 md 重生成（若存在）
                bi = audio.parent / (audio.stem + '.对照.md')
                if bi.exists():
                    try:
                        en_all = tl.parse_vtt_cues(_audio_sib(it['audio'], '.en.vtt').read_text(encoding='utf-8', errors='replace'))
                        lines_md = ['# 中英对照', '']
                        for i, c in enumerate(en_all):
                            z = ''
                            bz, bo = None, -1.0
                            for zz in zh_full:
                                ov = min(zz['end'], c['end']) - max(zz['start'], c['start'])
                                if ov > bo:
                                    bo, bz = ov, zz
                            if bz and bo > 0.3:
                                z = bz['text']
                            lines_md.append(f'**{i + 1}.** {c["text"]}  ')
                            lines_md.append(f'> {z}  ')
                            lines_md.append('')
                        bi.write_text(chr(10).join(lines_md), encoding='utf-8')
                        say('中英对照文稿已更新')
                    except Exception as e:
                        say('对照文稿更新跳过：' + str(e)[:80])
                say(f'✅ AI 补全完成：中文 {len(zh_full)} 句')
            except Exception as e:
                self._agent_job['error'] = str(e)[:200]
                say('❌ ' + str(e)[:150])
            finally:
                self._agent_job['running'] = False
                self._agent_job['done'] = True

        threading.Thread(target=work, daemon=True).start()
        return {'ok': True}

    def agent_status(self):
        j = getattr(self, '_agent_job', None) or {}
        return {'ok': True, **j}

    def generate_subtitles(self, item_id):
        """后台线程给缺英文字幕的语料生成 en.vtt/zh.vtt（复用下载管线的转写+翻译）。"""
        try:
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            if _audio_sib(it['audio'], '.en.vtt').exists():
                return {'ok': False, 'msg': '这个语料已有英文字幕，不用生成'}
            self._subgen = {'running': True, 'log': [], 'done': False, 'error': '', 'item': item_id,
                           'progress': {'phase': 'init', 'pct': 0, 'speed': '', 'detail': ''}}

            def say(m):
                self._subgen['log'].append(str(m))
                self._subgen['log'] = self._subgen['log'][-80:]

            def setprog(phase, pct=None, speed='', detail=''):
                self._subgen['progress'] = {'phase': phase, 'pct': pct,
                                            'speed': speed, 'detail': detail}

            def run():
                mon = {'on': False}
                try:
                    _t, asr, llm = self._services()
                    dur = it.get('duration') or 0
                    mode = (getattr(asr, 'mode_hint', '') or 'traditional')
                    # 阶段1：ffmpeg 转 16k WAV（很快，不确定动画）
                    setprog('convert', None, '', '转换音频格式…')
                    say(f'开始语音转写（模式：{ {"traditional":"传统","parallel":"分片并行","gummy":"一体"} .get(mode,mode)}）…')
                    t0 = time.time()
                    # 阶段2：转写（黑盒接口 → 估算进度；分片并行按 4 路等效倍速）
                    est_x = 12.0 * (4.0 if mode == 'parallel' else 1.0)
                    if dur and dur > 0:
                        mon['on'] = True

                        def monitor():
                            time.sleep(4.0)            # 给 convert 阶段的 shimmer 留展示时间
                            while mon['on']:
                                el = time.time() - t0 - 4.0
                                if el < 0:
                                    time.sleep(1.0)
                                    continue
                                est = min(95.0, el * est_x / dur * 100.0)
                                det = '收尾中（识别速率有波动，正常）' if est >= 85 else '语音转写中（按历史速度估算）'
                                spd = (el > 1.5) and ('%.1fx 实时' % (dur / el)) or ''
                                setprog('asr', round(est, 1), spd, det)
                                time.sleep(2.0)
                        threading.Thread(target=monitor, daemon=True).start()
                    else:
                        setprog('asr', None, '', '语音转写中…')
                    cues, gummy_zh = videosrc._asr_to_cues(it['audio'], Path(it['audio']).parent,
                                                           asr, say, dur, mode=mode)
                    cues = self._selfheal_cues(          # 第11问：落盘前兜底
                        cues, kind=(it.get('sourceKind') or None), log=say)
                    mon['on'] = False
                    setprog('asr', 100.0, '', '转写完成')
                    say(f'转写完成：{len(cues)} 句')
                    outdir = Path(it['audio']).parent
                    slug = Path(it['audio']).stem
                    en_srt = outdir / f'{slug}.en.srt'
                    tl.write_srt(en_srt, cues)
                    tl.srt_to_vtt(en_srt, outdir / f'{slug}.en.vtt')
                    zh = gummy_zh or []
                    if gummy_zh:
                        say(f'一体模式自带中文 {len(zh)} 句（跳过 LLM 翻译）')
                    else:
                        say('英文字幕已生成，翻译中文…')
                        setprog('translate', None, '', '准备翻译（第一批约需十几秒）…')
                        # 阶段3：LLM 批量翻译（真实批次进度）
                        def tprog(p):
                            setprog('translate', p.get('percent'),
                                    '%d/%d 批' % (p.get('done', 0), p.get('total', 0)),
                                    'AI 翻译中')
                        zh = videosrc._translate_cues(cues, llm, say, None, tprog) or []
                    if zh:
                        zh_srt = outdir / f'{slug}.zh.srt'
                        tl.write_srt(zh_srt, zh)
                        tl.srt_to_vtt(zh_srt, outdir / f'{slug}.zh.vtt')
                        say(f'完成：英文 {len(cues)} 句 · 中文 {len(zh)} 句')
                    else:
                        say('完成：英文 {0} 句（中文翻译没配 LLM，跳过）'.format(len(cues)))
                    setprog('done', 100.0, '', '完成')
                    try:
                        tl.write_bilingual(outdir / f'{slug}.对照.md', cues, zh,
                                           it.get('title') or slug,
                                           f'ASR 补生成于 {datetime.now():%Y-%m-%d %H:%M}')
                    except Exception:
                        pass
                    self._subgen['done'] = True
                    self._subgen['running'] = False
                except Exception as ex:
                    mon['on'] = False
                    self._subgen['error'] = str(ex)[:300]
                    self._subgen['running'] = False
                    self._subgen['done'] = True

            threading.Thread(target=run, daemon=True).start()
            return {'ok': True, 'started': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def subtitle_job_status(self, item_id=''):
        st = getattr(self, '_subgen', None) or {}
        if item_id and st.get('item') != item_id:
            return {'ok': True, 'running': False, 'log': [], 'done': False, 'error': '',
                    'progress': None}
        return {'ok': True,
                **{k: st.get(k) for k in ('running', 'log', 'done', 'error', 'progress')}}

    # ---------- AI 诊断修正字幕 ----------
    def _sync_check(self, cues):
        """音画同步自检：抽样头/中/尾 3 句，检查时长/词数语速合理性。

        正常语速约 2.5 词/秒；>5 词/秒（太快=时间戳过短）或 <0.8 词/秒
        （太慢=时间戳过长/含长静音）视为异常。全局偏差→建议 CAL 校准；
        局部离谱→建议重转写。
        """
        def _rate(c):
            n = len((c.get('text') or '').split())
            d = float(c.get('end') or 0) - float(c.get('start') or 0)
            if n <= 0 or d <= 0.3:
                return None
            return n / d
        idxs = []
        n_all = len(cues)
        if n_all >= 3:
            idxs = [0, n_all // 2, n_all - 1]
        elif n_all:
            idxs = list(range(n_all))
        rates = []
        for i in idxs:
            r = _rate(cues[i])
            if r is not None:
                rates.append((i, round(r, 2)))
        if not rates:
            return {'bad': False, 'detail': '', 'advice': '', 'rates': []}
        bad_local = [(i, r) for i, r in rates if r > 5.0 or r < 0.8]
        detail = '、'.join(f'第 {i + 1} 句 {r} 词/秒' for i, r in bad_local) or '正常'
        if bad_local:
            fast = sum(1 for _i, r in bad_local if r > 5.0)
            slow = len(bad_local) - fast
            # 多处同向 → 全局偏移用 CAL；过快型（时间戳过短的典型失真）→
            # 重新生成；过慢型 → 校准或诊断修正
            if len(bad_local) >= 2 and (fast == 0 or slow == 0):
                return {'bad': True, 'detail': detail,
                        'advice': '用「⏱ 校准」做语料级偏移', 'rates': rates}
            if fast > 0:
                return {'bad': True, 'detail': detail,
                        'advice': '时间戳失真（过短），建议「⚡ 重新生成字幕」'
                                  '或「🩺 诊断修正」', 'rates': rates}
            return {'bad': True, 'detail': detail,
                    'advice': '时间戳偏长，可用「⏱ 校准」或「🩺 诊断修正」', 'rates': rates}
        return {'bad': False, 'detail': '', 'advice': '', 'rates': rates}

    def subtitle_doctor(self, item_id, scope='full', index=-1, indexes=None, user_note=''):
        """后台线程：诊断并修正字幕（终稿 P1b 预检体系的执行端）。

        scope: full=全文（原行为）/ one=只修第 index 句 / multi=按 indexes
        列表修多个句（每个 index 一个时间窗，窗口间互不挤压，一次落盘）。
        user_note: 用户在纠正弹窗里勾/写的反馈（"时间戳太快"等），进提示词让 LLM 重点处理。
        暂停：_docgen['pause']（threading.Event）——UI 调 subtitle_doctor_pause/resume 控制。
        """
        if scope == 'multi' and not (isinstance(indexes, list) and indexes):
            return {'ok': False, 'msg': 'multi 模式需要 indexes 句号列表'}
        if scope == 'multi' and len(indexes) > 50:
            return {'ok': False, 'msg': '一次最多修正 50 句（请分批）'}
        try:
            items = tl.scan_library(self.all_dirs())
            it = next((x for x in items if x['id'] == item_id), None)
            if not it:
                return {'ok': False, 'msg': '找不到该语料'}
            _it2, cues = self._cues_of(item_id)
            if not cues:
                return {'ok': False, 'msg': '没有英文字幕可诊断——请先在卡片上点「⚡生成字幕」转写，完成后再回来修正'}
            zh_path = _audio_sib(it['audio'], '.zh.vtt')
            zh_cues = []
            if zh_path.exists():
                zh_cues = tl.parse_vtt_cues(zh_path.read_text(encoding='utf-8', errors='replace'))

            self._docgen = {'running': True, 'log': [], 'done': False, 'error': '', 'item': item_id,
                            'idMap': {},
                            'pause': threading.Event(),          # set=运行中 clear=暂停（wait() 阻塞点）
                            'stop': threading.Event(),           # set=请求取消（检查点抛出，不落盘）
                            'progress': {'phase': 'diagnose', 'pct': None, 'speed': '',
                                         'detail': '读取与诊断中…'}}
            self._docgen['pause'].set()
            _sw, _sd = self._doctor_split_params(it)   # 第11问：按来源类型切，不再写死 16 词

            def _ck():
                """取消检查点：用户点了 ⏹ 后在批次间/落盘前抛出，字幕原样不写。"""
                if self._docgen.get('stop') and self._docgen['stop'].is_set():
                    raise RuntimeError('已取消（未落盘，字幕保持原样）')
            if (user_note or '').strip():
                self._docgen['userNote'] = user_note.strip()[:200]
            else:
                self._docgen['userNote'] = ''
            # 修正前后 id 映射基准：修正/隔离过程中语料若换 id（改名/重新归组），
            # 前端还攥着旧 id 操作会全部落空——完成后按音频路径对出 old→new
            pre_ids = {x['id']: str(Path(x['audio']).resolve()).lower() for x in items}

            def say(m):
                self._docgen['log'].append(str(m))
                self._docgen['log'] = self._docgen['log'][-80:]

            def setprog(phase, pct=None, speed='', detail=''):
                self._docgen['progress'] = {'phase': phase, 'pct': pct,
                                            'speed': speed, 'detail': detail}

            def _mk_prog(base, span, label):
                """生成带 ETA 的对齐进度回调。

                local_align 只报 (done_wins, total_wins)，长视频用户看不到还剩多久：
                MMS_FA 约 3.9× 实时，2 小时音频要跑 ≈47 分钟。这里按已完成比例
                外推剩余时间；前 2% 不估（样本太少，估出来会乱跳）。
                """
                t0 = [time.time()]

                def _p(d, t, ph):
                    frac = (d / t) if t else 0.0
                    eta = ''
                    if d > 0 and frac > 0.02:
                        el = time.time() - t0[0]
                        if el >= 1.0:
                            remain = el / frac * (1.0 - frac)
                            if remain >= 60:
                                eta = (f'（剩余约 {int(remain // 60)} 分 '
                                       f'{int(remain % 60)} 秒）')
                            elif remain >= 1:
                                eta = f'（剩余约 {int(remain)} 秒）'
                    setprog('align', base + int(span * frac), ph or '',
                            f'{label} {d}/{t} 窗{eta}')

                return _p

            def run():
                try:
                    self._quarantine_16k(say)
                    _t, asr, llm = self._services()
                    model_name = (llm.cfg.get('llmModel') or 'LLM') if (llm and llm.ready) else ''
                    say(f"并行：{min(self.cfg.get('llmBatchWorkers', 3), 6)} 路 LLM 批次 / "
                        f"{min(self.cfg.get('asrAlignWorkers', 3), 6)} 路对齐分组")
                    # ---- scope=one/multi：计算待修时间窗集合 ----
                    wins = []                          # [(s,e), ...]
                    if scope == 'one' and 0 <= index < len(cues):
                        c0 = cues[index]
                        wins = [(float(c0['start']), float(c0['end']))]
                    elif scope == 'multi' and indexes:
                        for ix in sorted(set(int(i) for i in indexes)):
                            if 0 <= ix < len(cues):
                                c0 = cues[ix]
                                wins.append((float(c0['start']), float(c0['end'])))
                        say(f'多句修正：{len(wins)} 个句窗')
                    win_s = wins[0][0] if wins else None
                    win_e = wins[-1][1] if wins else None
                    # ---- 阶段1 诊断（本地规则，秒出） ----
                    long_n = sum(1 for c in cues if len((c['text'] or '').split()) > 22)
                    overlap_n = sum(1 for i in range(len(cues) - 1)
                                    if (cues[i]['end'] or 0) > (cues[i + 1]['start'] or 0) + 0.01)
                    zh_cover = (len(zh_cues) / max(1, len(cues))) if cues else 0
                    say(f'诊断：{len(cues)} 句英文 · 超长句 {long_n} · 时间戳重叠 {overlap_n} '
                        f'· 中文覆盖 {zh_cover:.0%}')
                    # 音画同步自检（终稿 P0-2：抽样头/中/尾 3 句查语速合理性）
                    sync = self._sync_check(cues)
                    if sync['bad']:
                        say(f'⚠ 音画同步异常：{sync["detail"]}（建议：{sync["advice"]}）')
                    else:
                        say('音画同步自检：抽样 3 句语速均正常')
                    if model_name:
                        say(f"校对模型：{model_name}")

                    # ---- 阶段1.9 对齐引擎准备 + 时间轴出生即坏 → 全文重定位（先治本再切分）----
                    # 对齐专用引擎：永远 paraformer（词级时间戳）——与用户练习用的识别引擎
                    # 无关。练习用 MiMo 没问题，但时间轴对齐必须带时间戳。
                    # 退化特征：转写没拿到时间戳 → 时间按词数编造（中位"语速">4.2 或覆盖
                    # 不足 55%）。此时必须在**自然整句**上重定位（碎片化后再定位会连锁失锚），
                    # 拿到真实时间后才做本地切分。
                    import asr_align
                    align_asr = None
                    _ds_key = (self.cfg.get('dashscopeApiKey') or '').strip()
                    if _ds_key:
                        try:
                            _acfg = dict(self.cfg)
                            _acfg['asrProvider'] = 'dashscope'
                            _acfg['asrModel'] = 'paraformer-realtime-v2'
                            _a2 = speech_asr.ASR(_acfg, say)
                            if _a2.test().get('ok'):
                                say('对齐引擎：paraformer-realtime-v2（词级时间戳专用——omni 没有词级，时间轴校准离不开它；文本转写不受影响）')
                                align_asr = _a2
                        except Exception:
                            align_asr = None
                    if align_asr is None:
                        _prov_a = getattr(asr, 'provider', lambda: '')()
                        try:
                            align_asr = asr if (_prov_a != 'mimo'
                                                and bool(asr.test().get('ok'))) else None
                        except Exception:
                            align_asr = None
                    if align_asr is not None and align_asr is not asr:
                        say('对齐引擎：阿里云 paraformer（专用，与你练习用的识别无关）')
                    # 本地强制对齐（MMS_FA）可用性：可用则 2.5 走【音文连贯性校对门】
                    # （LLM 前取逐句证据），阶段 3.5 校对定稿后一次定时间轴。
                    _use_fa = False
                    if self.cfg.get('asrAlign') is not False:
                        try:
                            import local_align
                            _use_fa = local_align.available(log=say)
                        except Exception:
                            _use_fa = False
                    if _use_fa:
                        say('本地强制对齐引擎就绪（MMS_FA）：LLM 前先过【音文连贯性校对门】，'
                            '校对定稿后时间轴一次定稿（词级精度，零 ASR 费用）')
                    _did_realign = False
                    _realign_stats = {}
                    if self.cfg.get('asrAlign') is not False and cues and align_asr is not None:
                        _rates = sorted(
                            len((c.get('text') or '').split()) /
                            max(0.01, float(c.get('end') or 0) - float(c.get('start') or 0))
                            for c in cues if (c.get('text') or '').strip())
                        _med = _rates[len(_rates) // 2] if _rates else 0.0
                        _adur = float(it.get('duration') or 0)
                        _cov = (max(float(c.get('end') or 0) for c in cues)
                                - min(float(c.get('start') or 0) for c in cues)) if cues else 0.0
                        if (_med > 4.2) or (_adur > 30 and _cov < _adur * 0.55):
                            if _use_fa:
                                # 本地对齐可用：时间轴反正由 3.5 词级定稿，不花 paraformer
                                # 钱 realign——文本是否可信交给阶段 2.5' 音文连贯性校对门
                                say(f'⚠ 时间轴出生即坏（中位 {_med:.1f} 词/秒，字幕覆盖 '
                                    f'{_cov:.0f}s/音频 {_adur:.0f}s）——时间轴将由本地对齐'
                                    '定稿；文本是否可信先过【音文连贯性校对门】（下一步）')
                            else:
                                say(f'⚠ 时间轴出生即坏（中位 {_med:.1f} 词/秒，字幕覆盖 {_cov:.0f}s'
                                    f'/音频 {_adur:.0f}s）——转写时没拿到真实时间戳，微调/校准'
                                    '都救不了；自动【全文重转写定位时间轴】（文本不动）')
                                setprog('align', 5, 'paraformer 全文重定位',
                                        '整段重转写 + 逐句定位（时间以转写为准，文本不动）')
                                import tempfile as _tf
                                _rdir = Path(_tf.mkdtemp(prefix='ted_realign_'))
                                try:
                                    # 长音频走分块版（E-07）：realign_all 有 90 分钟硬上限，
                                    # 超过就直接抛错，用户只能重新生成字幕。分块版把音频切成
                                    # 若干 8 分钟的责任窗口各自识别后拼成一条全局词流，再走
                                    # 与短音频完全相同的逐句定位逻辑，因此行为一致、
                                    # 只是不再有长度上限。短音频内部直接转调 realign_all。
                                    _rc, _realign_stats = asr_align.realign_all_chunked(
                                        it['audio'], cues, align_asr, tl.FFMPEG, _rdir,
                                        duration=_adur, log=say,
                                        pause_wait=lambda: (self._docgen['pause'].wait(), _ck())[1])
                                    cues[:] = _rc  # 就地替换：cues 是外层闭包变量，重绑定会让 run() 里的先前读取 UnboundLocalError
                                finally:
                                    import shutil as _sh2
                                    _sh2.rmtree(_rdir, ignore_errors=True)
                                _did_realign = True
                                setprog('align', 100, '', '全文重定位完成，进入本地修正')
                                # 锚定率过低 = 原转写文本顺序已乱（MiMo 交错句），
                                # "保留文本重定位"无从谈起——升级【重转写重建】：
                                # 重新转写拿到真实文本+词级时间轴（67集 agent 修法的全自动版），
                                # 旧中文时间轴同步作废，阶段3 全量重译。
                                _anch = int(_realign_stats.get('aligned') or 0)
                                if _anch < max(3, int(len(cues) * 0.3)):
                                    say(f'⚠ 保留文本定位只锚定 {_anch}/{len(cues)} 句'
                                        '（原转写的句子顺序已乱，插值时间轴不可信）——'
                                        '升级【重转写重建】：重新转写出真实文本+时间轴，再重新校对翻译')
                                    setprog('align', 10, 'paraformer 重转写',
                                            '整段重转写重建字幕（文本+时间轴全新，约 1-2 分钟）')
                                    self._docgen['pause'].wait()
                                    _ck()
                                    import tempfile as _tf2
                                    _rdir2 = Path(_tf2.mkdtemp(prefix='ted_rebuild_'))
                                    try:
                                        _newcues, _ = videosrc._asr_to_cues(
                                            it['audio'], _rdir2, align_asr, say, _adur,
                                            mode='traditional', keep_asr_model=True)
                                    finally:
                                        import shutil as _sh3
                                        _sh3.rmtree(_rdir2, ignore_errors=True)
                                    cues[:] = _newcues
                                    zh_cues[:] = []    # 旧中文跟着旧假时间轴走，一并作废（阶段3 全量重译）
                                    _realign_stats = {'total': len(cues), 'aligned': len(cues),
                                                      'kept': 0, 'per_cue': []}
                                    say(f'重转写重建完成：{len(cues)} 句（真实词级时间戳），进入本地修正')
                                    setprog('align', 100, '', '重转写重建完成，进入本地修正')
                    if align_asr is None and not _use_fa:
                        say('ASR 对齐跳过：没有可用的词级时间戳引擎（练习用 MiMo 可以，'
                            '但时间轴对齐需要阿里云 paraformer Key）——校对/翻译不受影响')

                    setprog('fix-en', 0, model_name and ('模型 ' + model_name) or '',
                            '本地修正中' + ('（校对走 ' + model_name + '）' if model_name else '（未配 LLM，只做本地修正）'))

                    # ---- 阶段2 修正：先净化时间戳，再切超长句 + 时间戳修复（本地） ----
                    if wins:
                        # 按窗口逐个处理：窗内 sanitize+切分（末句 end 钳到窗界防挤压邻句），窗外原样
                        def in_win(c):
                            return any(float(c['start']) < we + 0.05 and float(c['end']) > ws - 0.05
                                       for ws, we in wins)
                        fixed = []
                        for k, (ws, we) in enumerate(wins):
                            inside = [c for c in cues
                                      if float(c['start']) < we + 0.05
                                      and float(c['end']) > ws - 0.05]
                            fw = videosrc.sanitize_cues(inside)
                            fw = videosrc._split_long_cues(fw, max_words=_sw, max_dur=_sd)
                            for i in range(len(fw) - 1):
                                if fw[i]['end'] > fw[i + 1]['start']:
                                    fw[i]['end'] = max(fw[i]['start'] + 0.4, fw[i + 1]['start'])
                            if fw:
                                fw[-1]['end'] = min(fw[-1]['end'], we)   # 钳到窗界
                                fw[0]['start'] = max(fw[0]['start'], ws)  # 头部也钳
                            fixed.extend(fw)
                        fixed.extend([c for c in cues if not in_win(c)])
                        fixed.sort(key=lambda x: x['start'])
                        say(f'本地修正：{len(wins)} 个窗 → {len(fixed)} 句')
                    else:
                        fixed = videosrc.sanitize_cues(cues)
                        fixed = videosrc._split_long_cues(fixed, max_words=_sw, max_dur=_sd)
                        for i in range(len(fixed) - 1):
                            if fixed[i]['end'] > fixed[i + 1]['start']:
                                fixed[i]['end'] = max(fixed[i]['start'] + 0.4,
                                                      fixed[i + 1]['start'])
                        if len(fixed) != len(cues) or overlap_n:
                            say(f'本地修正：{len(cues)} → {len(fixed)} 句（长句切分+时间戳）')
                    setprog('fix-en', 100.0, '', '本地修正完成')

                    # ---- 阶段2.5 ASR 强制对齐（1.9 已全文重定位的直接跳过逐窗对齐） ----
                    # 只取 paraformer 的词级时间戳、不采转写文本；匹配率低的句保留原时间。
                    # P0-B：顺手产出逐句失配证据（匹配率/偏移），阶段3 进 LLM 提示词定向处理。
                    evidence_list = []
                    astats = {}
                    if (self.cfg.get('asrAlign') is not False and cues
                            and align_asr is not None and not _did_realign and not _use_fa):
                        try:
                            setprog('align', 0, 'paraformer 词级对齐',
                                    'ASR 强制对齐中（只校准时间轴，不改文本）')
                            fixed, astats = asr_align.align_cues(
                                it['audio'], fixed, align_asr, say,
                                win=(win_s, win_e) if wins else None,
                                ffmpeg=tl.FFMPEG,
                                pause_wait=lambda: (self._docgen['pause'].wait(), _ck())[1],
                                workers=min(self.cfg.get('asrAlignWorkers', 3), 6))
                            # 二遍精对齐（音频精度提升）：第一遍修大偏移/塌缩，
                            # 第二遍在新时间轴上细校句首句尾边界——把"边界擦边"
                            # （切段吃到邻句尾巴）再压一截。费用约再一倍（≈0.4 元/时）。
                            fixed, astats2 = asr_align.align_cues(
                                it['audio'], fixed, align_asr, say,
                                win=(win_s, win_e) if wins else None,
                                ffmpeg=tl.FFMPEG,
                                pause_wait=lambda: (self._docgen['pause'].wait(), _ck())[1],
                                workers=min(self.cfg.get('asrAlignWorkers', 3), 6))
                            if astats2.get('per_cue'):
                                astats = astats2            # 证据/统计以精校后为准
                            astats['aligned'] = max(int(astats.get('aligned') or 0),
                                                    int(astats2.get('aligned') or 0))
                        except Exception as e:
                            astats = {}
                            say(f'ASR 对齐失败（跳过，不影响后续校对）：{str(e)[:100]}')
                    elif _use_fa and fixed:
                        # ---- 阶段2.5' 音文连贯性校对门（用户 2026-10-13 需求）----
                        # LLM 之前用本地强制对齐验证"每个句子的词音频里真说了没"：
                        #   · 低分句（过半词对不上）→ 证据进 LLM 提示词定向修复
                        #   · 大面积低分 = 原转写文本不可信（MiMo 交错事故形态）→ 重转写重建
                        # 只取证据不改任何东西；时间轴统一由 3.5 定稿。
                        try:
                            setprog('align', 5, '音文连贯性校对',
                                    '本地强制对齐验证句子与音频（LLM 校对前的质量门）')
                            _g_cues, _g_stats, _g_words = local_align.align_cues(
                                it['audio'], fixed, tl.FFMPEG, log=say,
                                pause_wait=lambda: (self._docgen['pause'].wait(), _ck())[1],
                                progress=_mk_prog(5, 25, '连贯性校对'),
                                cache_dir=self.cache_dir)
                            _per = _g_stats.get('per_cue') or []
                            _low = [i for i, e in enumerate(_per)
                                    if e and e['total'] > 0 and e['matched'] * 2 < e['total']]
                            if len(fixed) >= 10 and len(_low) > len(fixed) * 0.4 \
                                    and align_asr is not None:
                                say(f'⚠ 音文连贯性校对门：{len(_low)}/{len(fixed)} 句过半词'
                                    '对不上音频——原转写文本不可信（交错/乱序），'
                                    '升级【重转写重建】：重新转写出真实文本+时间轴')
                                setprog('align', 40, 'paraformer 重转写',
                                        '整段重转写重建字幕（文本+时间轴全新，约 1-2 分钟）')
                                self._docgen['pause'].wait()
                                _ck()
                                import tempfile as _tf3
                                _rdir3 = Path(_tf3.mkdtemp(prefix='ted_gate_rebuild_'))
                                try:
                                    _newcues, _ = videosrc._asr_to_cues(
                                        it['audio'], _rdir3, align_asr, say,
                                        float(it.get('duration') or 0),
                                        mode='traditional', keep_asr_model=True)
                                finally:
                                    import shutil as _sh4
                                    _sh4.rmtree(_rdir3, ignore_errors=True)
                                cues[:] = _newcues
                                zh_cues[:] = []       # 旧中文跟着旧文本走，一并作废（阶段3 全量重译）
                                fixed = videosrc.sanitize_cues(cues)
                                fixed = videosrc._split_long_cues(fixed, max_words=_sw, max_dur=_sd)
                                say(f'重转写重建完成：{len(cues)} 句，继续 LLM 校对（新文本无需证据）')
                                astats = {}
                                evidence_list = []
                            else:
                                astats = {'total': len(fixed),
                                          'aligned': len(fixed) - len(_low),
                                          'kept': len(_low), 'per_cue': _per}
                                evidence_list = _per
                                say(f'音文连贯性校对：{len(fixed) - len(_low)}/{len(fixed)} 句'
                                    '与音频一致' + (f'，{len(_low)} 句待 LLM 重点修复'
                                                    '（已带证据进提示词）' if _low else ''))
                        except Exception as _ge:
                            if '已取消' in str(_ge):
                                raise
                            astats = {}
                            say(f'音文连贯性校对失败（{str(_ge)[:80]}）——按无证据校对继续')
                    elif _did_realign:
                        astats = _realign_stats
                        say(f"时间轴已全文重定位：{astats.get('aligned') or 0}/"
                            f"{astats.get('total') or 0} 句锚定到真实时间流，其余在锚点间插值")
                    if astats:
                        if astats.get('aligned') and not _did_realign and not _use_fa:
                            say(f"ASR 对齐：{astats['aligned']}/{astats['total']} 句时间轴已校准"
                                f"（{astats.get('kept') or 0} 句匹配率低保留原时间）")
                        elif not astats.get('aligned') and not _did_realign \
                                and not astats.get('error') and not _use_fa:
                            say('ASR 对齐：没有句子通过匹配门槛，全部保留原时间')
                        per_cue = astats.get('per_cue') or []
                        low_match_n = sum(1 for e in per_cue
                                          if e and e['total'] > 0 and e['matched'] < e['total'])
                        big_shift_n = sum(1 for e in per_cue
                                          if e and abs(e.get('shift_ms', 0)) > 500)
                        if low_match_n or big_shift_n:
                            say(f"证据：{low_match_n} 句低匹配 / {big_shift_n} 句大偏移（已进提示词，LLM 将优先处理）")
                        evidence_list = per_cue
                    self._docgen['pause'].wait()
                    _ck()

                    # ---- 阶段3 LLM 批量：修错词 + （重）翻译 ----
                    need_llm = bool(llm and llm.ready)
                    zh_out = []
                    # one/multi：翻译范围=各窗口内的句
                    if wins:
                        def in_any_win(c):
                            return any(float(c['start']) >= ws - 0.05
                                       and float(c['end']) <= we + 0.05 for ws, we in wins)
                        fix_idx = [i for i, c in enumerate(fixed) if in_any_win(c)]
                    else:
                        fix_idx = list(range(len(fixed)))
                    if need_llm and fix_idx:
                        B = 30
                        groups = [fix_idx[i:i + B] for i in range(0, len(fix_idx), B)]
                        total = len(groups)
                        en_fixed_n = 0
                        # 用户附言进提示词（P1-4）：模板 v3 的 {user_note} 占位符
                        note = (self._docgen.get('userNote') or '').strip()
                        # 材料类型自动判定（流程固化）：多人对话=问答轮次密集或标题含对话关键词；
                        # 单人演讲=其余。注入提示词让 LLM 按对应风格校对/翻译/断句。
                        _title = str(it.get('title') or '') + ' ' + str(it.get('id') or '')
                        _q_ratio = sum(1 for c in fixed if '?' in (c.get('text') or '')) / max(1, len(fixed))
                        _dialogue = (_q_ratio >= 0.15 or any(
                            k in _title.lower() for k in ('conversation', 'podcast', 'dialogue',
                                                          '对话', '播客', '访谈', 'interview')))
                        _mat = '多人对话/播客' if _dialogue else '单人演讲/独白'
                        say(f'材料类型判定：{_mat}（问句占比 {_q_ratio*100:.0f}%）——校对/翻译/断句按此风格')
                        _mat_line = (f'【材料类型】{_mat}——'
                                     + ('按对话轮次处理：翻译口语化、问答成对断句、说话人切换即句边界。'
                                        if _dialogue else
                                        '按演讲独白处理：保持长句结构与书面连贯，断句按语法边界，翻译偏书面。'))
                        note_line = (_mat_line + '\n'
                                     + ('⚠ 用户反馈（这些是用户亲耳听到的问题，请重点排查处理）：' + note
                                        if note else '（用户未指出具体问题，按常规流程校对。）'))
                        # P2-1：LLM 没给译文的句回落原中文（按时间重叠找）——
                        # en/zh 永不缺句，配对（时间重叠式）永不错位
                        def orig_zh_of(s0, s1):
                            best, best_ov = '', 0.0
                            for z in zh_cues:
                                ov = min(float(s1), float(z['end'])) - max(float(s0), float(z['start']))
                                if ov > best_ov:
                                    best_ov, best = ov, (z.get('text') or '')
                            return (best or '').strip()
                        zh_fallback_n = 0
                        why_lines = []

                        # --- P0-A 提速：LLM 批次并行（默认 3 路可调）+ P0-B 证据标注入行 ---
                        from concurrent.futures import ThreadPoolExecutor, as_completed

                        # --- P3/H-04：LLM 批次结果缓存 ---
                        # 同一批文本 + 同一提示词版本 + 同一模型 → 直接秒回不再请求。
                        # 实现在模块级 _llm_json_cached（可单测），这里只绑定本次上下文。
                        try:
                            _pv = str(prompts.version_of('subtitle_doctor') or '')
                        except Exception:
                            _pv = ''
                        _llm_stat = {'hit': 0, 'miss': 0}

                        def _llmc(_sys, _user):
                            return _llm_json_cached(self.cache_dir, _pv, model_name,
                                                    _sys, _user, _llm_stat, llm)

                        def _batch_task(bi):
                            """单批任务：暂停检查→拼行（带 ⚠ 证据标）→chat_json。
                            工作线程只返回数据，say/setprog 留给 doctor 主线程。"""
                            if self._docgen.get('stop') and self._docgen['stop'].is_set():
                                return {'bi': bi, 'data': None, 'err': '已取消',
                                        'chunk': [fixed[i] for i in groups[bi]]}
                            try:
                                self._docgen['pause'].wait()      # 暂停点：未发出的批挂在这
                            except Exception:
                                pass
                            chunk_idx = groups[bi]
                            chunk = [fixed[i] for i in chunk_idx]
                            parts = []
                            has_ev = False
                            for k, ci in enumerate(chunk_idx):
                                txt = chunk[k]['text']
                                ev = evidence_list[ci] if ci < len(evidence_list) else None
                                if ev and (ev['total'] > 0 and ev['matched'] < ev['total']
                                           or abs(ev.get('shift_ms', 0)) > 500):
                                    parts.append(f"{k}|{txt} ⚠匹配{ev['matched']}/{ev['total']}"
                                                 f"·偏移{ev.get('shift_ms', 0):+d}ms")
                                    has_ev = True
                                else:
                                    parts.append(f"{k}|{txt}")
                            lines = '\n'.join(parts)
                            evidence_param = ('⚠ 本批各行带"⚠匹配m/t·偏移"标注——这是语音识别实际听到的'
                                              '与字幕的差异证据，请优先按④处理这些句'
                                              if has_ev else '（本批无对齐证据，按常规校对）')
                            try:
                                sys_p, _u = prompts.get('subtitle_doctor')
                                user = prompts.fmt('subtitle_doctor', lines=lines, user_note=note_line,
                                                   evidence=evidence_param)
                                data = _llmc(sys_p, user)
                                return {'bi': bi, 'data': data, 'err': None, 'chunk': chunk}
                            except Exception as e:
                                return {'bi': bi, 'data': None, 'err': str(e)[:80], 'chunk': chunk}

                        results = [None] * total
                        logq = []
                        pool = ThreadPoolExecutor(max_workers=min(self.cfg.get('llmBatchWorkers', 3), 6))
                        try:
                            from concurrent.futures import FIRST_COMPLETED
                            from concurrent.futures import wait as _fwait
                            futs = {pool.submit(_batch_task, bi): bi for bi in range(total)}
                            done_n = 0
                            pending = set(futs)
                            while pending:
                                done, pending = _fwait(pending, timeout=0.5,
                                                        return_when=FIRST_COMPLETED)
                                # 每 0.5s 查一次取消：不等下一批完成（否则点了 ⏹ 还要
                                # 干等在飞批次跑完才走到检查点）
                                _ck()
                                for fut in done:
                                    bi = futs[fut]
                                    try:
                                        r = fut.result()
                                    except Exception as e:            # 理论到不了（任务内已兜底）
                                        r = {'bi': bi, 'data': None, 'err': str(e)[:80],
                                             'chunk': [fixed[i] for i in groups[bi]]}
                                    results[bi] = r
                                    if r['err']:
                                        logq.append(f"批次 {bi + 1}/{total} LLM 失败（保留原句）：{r['err']}")
                                    done_n += 1
                                    setprog('fix-zh', done_n / total * 100.0,
                                            f'{done_n}/{total} 批 · {model_name}',
                                            '校对+翻译中（并行）')
                        finally:
                            # 取消：不能等整队批次跑完（with 池的 shutdown(wait=True) 会把
                            # 排队批次全部执行——用户点了 ⏹ 还在烧 LLM 的真凶）。
                            # cancel_futures 丢弃未启动的批；正在跑的最多 N 批自然结束、结果作废。
                            if self._docgen.get('stop') and self._docgen['stop'].is_set():
                                pool.shutdown(wait=False, cancel_futures=True)
                            else:
                                pool.shutdown(wait=True)
                        for msg in logq:
                            say(msg)

                        # 按批序应用结果（主线程，无竞争）：en 修正 + level + zh + why 收集
                        for r in results:
                            if r is None:
                                continue
                            chunk = r['chunk']
                            data = r['data']
                            by_i = {}
                            raw_items = data.get('items') if isinstance(data, dict) else (
                                data if isinstance(data, list) else [])
                            for x in (raw_items or []):
                                try:
                                    by_i[int(x.get('i'))] = x
                                except Exception:
                                    pass
                            for k, c in enumerate(chunk):
                                x = by_i.get(k)
                                if x:
                                    en_fix = str(x.get('en') or '').strip()
                                    if en_fix and en_fix != (c.get('text') or ''):
                                        c['text'] = en_fix
                                        en_fixed_n += 1
                                    lv = str(x.get('level') or '').strip().lower()
                                    if lv in ('simple', 'normal', 'complex'):
                                        c['level'] = lv
                                    zh_txt = str(x.get('zh') or '').strip()
                                    w = str(x.get('why') or '').strip()
                                    if w and len(why_lines) < 3:
                                        why_lines.append(w)
                                else:
                                    zh_txt = ''
                                if not zh_txt:                    # LLM 缺译 → 保留原中文，绝不丢句
                                    zh_txt = orig_zh_of(c['start'], c['end'])
                                    if zh_txt:
                                        zh_fallback_n += 1
                                zh_out.append({'start': c['start'], 'end': c['end'],
                                               'text': zh_txt})
                        zh_out = [z for z in zh_out if z['text']]
                        if _llm_stat['hit']:
                            say(f'LLM 批次命中缓存 {_llm_stat["hit"]} 批'
                                f'（这批文本没变，未再请求模型）')
                        if en_fixed_n:
                            say(f'LLM 校对：修正错词 {en_fixed_n} 句')
                        if zh_fallback_n:
                            say(f'中文兜底：{zh_fallback_n} 句 LLM 没给译文，已保留原中文（不缺句不错位）')
                        if why_lines:
                            say('修改理由示例：' + ' | '.join(why_lines))
                    else:
                        # 无 LLM：中文同样过一遍切分（与英文句保持时间对齐）
                        zh_out = videosrc._split_long_cues(zh_cues, max_words=_sw, max_dur=_sd)
                        say('未配 LLM：只做本地修正（切句+时间戳），不校对翻译')

                    # ---- 阶段4 落盘（覆盖原字幕，自动留 .bak） ----
                    self._docgen['pause'].wait()                  # 落盘前的最后暂停点
                    _ck()                                        # 取消的最后机会：落盘前中止=字幕零改动
                    # 终局 sanitize（修"怎么都修不正确"）：doctor 只保证窗内/组内不重叠，
                    # 窗外旧句、组间缝合处仍可能残留重叠——落盘前对全列表统一排序+去重叠
                    def _final_deoverlap(cs):
                        cs = sorted(cs, key=lambda x: (float(x['start'] or 0), float(x['end'] or 0)))
                        out = []
                        for c in cs:
                            s, e = float(c['start'] or 0), float(c['end'] or 0)
                            if e <= s:                                  # 倒时长 → 保底 0.3s
                                e = s + 0.3
                            elif e - s < 0.2 - 1e-6:                    # 过短 → 拉到 0.2s（1e-6 容差：3 位小数相减会得 0.1999999999999886）
                                e = s + 0.2
                            if out and s < out[-1]['end'] + 0.02:
                                # 与前句重叠：优先**剪前句的 end**（前句尾音频少一点点），
                                # 不推本句 start——start 不动 = 时间重叠式 en/zh 配对保持稳定
                                new_prev_end = max(out[-1]['start'] + 0.2, s - 0.02)
                                if new_prev_end < out[-1]['end']:
                                    out[-1]['end'] = round(new_prev_end, 3)
                                if s < out[-1]['end'] + 0.02:         # 前句剪到 0.2s 地板仍重叠 → 才推本句
                                    s = out[-1]['end'] + 0.02
                                    if e <= s:
                                        e = s + 0.3
                            c['start'], c['end'] = round(s, 3), round(e, 3)
                            out.append(c)
                        return out

                    def _enforce_invariants(cs, dur=None):
                        """落盘前最后一道闸：强制时间轴不变式，返回 (cues, violations)。

                        _final_deoverlap 已覆盖倒挂/过短/句间重叠；这里补它管不到的两类：
                          I3 越界——end 超出音频真实时长（实测语料有 (Applause) 标到 1151s
                             而音频只有 1148.32s，播放器 seek 会失败/停在末尾）
                          I1 兜底——修正后仍倒挂/过短的极端情况
                        只做"钳制到合法"，不改文本、不动别的句；全部记日志，便于回溯。
                        """
                        v = []
                        out = []
                        prev_end = 0.0
                        for i, c in enumerate(cs):
                            s = float(c.get('start') or 0)
                            e = float(c.get('end') or 0)
                            if e <= s:                              # I1 倒挂
                                e = s + 0.3
                                v.append(('INVERT', i))
                            if e - s < 0.2 - 1e-6:                  # I1 过短（同上，容差防浮点假阳性）
                                e = s + 0.2
                                v.append(('TOO_SHORT', i))
                            if s < prev_end - 0.02:                 # I2 与前句重叠
                                s = prev_end
                                v.append(('OVERLAP', i))
                            if dur and dur > 0:
                                # 用 floor 到毫秒，避免 round(1148.319637,3)=1148.32 仍高 0.36ms
                                _dur_ms = math.floor(dur * 1000) / 1000.0
                                if s >= _dur_ms:                    # I3 start 已越过音频末尾
                                    s = max(0.0, _dur_ms - 0.5)
                                    v.append(('START_BEYOND', i))
                                if e > _dur_ms:                     # I3 end 越界
                                    e = _dur_ms
                                    v.append(('OVERRUN', i))
                            if e <= s:
                                e = s + 0.2
                            c = dict(c)
                            c['start'] = round(s, 3)
                            c['end'] = round(e, 3)
                            out.append(c)
                            prev_end = c['end']
                        return out, v

                    def _clamp_words_to_cues(words, cues):
                        """把词级时间夹进所属句的区间（E-09），返回 (新词表, 被夹数量)。

                        `.words.json` 在阶段 3.5 强制对齐时写盘，而 `_enforce_invariants`
                        要到之后才夹句界（典型：(Applause) 句 end 1151.000 → 1148.319），
                        词就可能落到句界外。前端 markWordAt 拿词时间与播放位置比对，
                        落在界外的词永远不亮 → 表现成「这一句逐词高亮整句失效」。

                        处理原则——宁可不亮，也不能亮到别的句子上：
                          · 与句区间有交集 → s/e 夹进 [cue.start, cue.end]
                          · 完全在界外     → 丢弃该词
                        """
                        n = 0
                        kept = []
                        for w in words:
                            try:
                                i = int(w.get('i'))
                            except (TypeError, ValueError):
                                kept.append(w)
                                continue
                            if not (0 <= i < len(cues)):
                                kept.append(w)
                                continue
                            cs = float(cues[i]['start'])
                            ce = float(cues[i]['end'])
                            ws_ = float(w.get('s') or 0)
                            we_ = float(w.get('e') or 0)
                            ns, ne = max(ws_, cs), min(we_, ce)
                            if ne <= ns:                 # 完全在界外 → 丢
                                n += 1
                                continue
                            if abs(ns - ws_) > 1e-6 or abs(ne - we_) > 1e-6:
                                w = dict(w)
                                w['s'] = round(ns, 3)
                                w['e'] = round(ne, 3)
                                n += 1
                            kept.append(w)
                        return kept, n

                    # ---- 落盘前统一到句子口径（B2 根治 + 去重落盘）----
                    # 上游 fixed 是 16 词切分粒度（给 LLM 批次用的），落盘前重新组装：
                    # 碎片拼回 + 重复句去重 + 句末拆分。这样 盘=展示(_cues_of)=lvl
                    # 三者 cueCount 永远一致——scoped/full 修正都不再出现口径漂移。
                    pre_n = len(fixed)
                    fixed = videosrc.assemble_sentences(fixed)
                    if len(fixed) != pre_n:
                        say(f'落盘句子组装：{pre_n} → {len(fixed)} 句（碎片拼回+去重）')
                    fixed = _final_deoverlap(fixed)
                    # ---- 阶段3.5 本地强制对齐定稿（MMS_FA）：文本定稿 → 一次全量词级对齐 ----
                    # 对齐"校对后的最终文本"：判别式 CTC 单调对齐，结构上不可能交错；
                    # 词级时间戳（几十 ms 精度）重写每句 start/end；副产品 .words.json
                    # 供跟读页卡拉OK高亮。失败/无 torch 保留现有时间轴（永远不比现状差）。
                    _fa_map = None   # [(旧窗口, 新窗口)] en 映射表（中文句跟随平移用）
                    _fa_words = []   # 词级时间戳（阶段 3.5 产出；E-09 夹紧要读，必须先初始化）
                    if _use_fa and fixed:
                        try:
                            _fa_old = [(float(c['start']), float(c['end'])) for c in fixed]
                            setprog('align', 8, '本地强制对齐', '词级时间戳对齐中（文本不动）')
                            _fixed2, _fa_stats, _fa_words = local_align.align_cues(
                                it['audio'], fixed, tl.FFMPEG, log=say,
                                pause_wait=lambda: (self._docgen['pause'].wait(), _ck())[1],
                                progress=_mk_prog(8, 87, '本地对齐'),
                                cache_dir=self.cache_dir)
                            if _fa_stats.get('aligned'):
                                fixed = _fixed2
                                _fa_map = list(zip(
                                    _fa_old, [(float(c['start']), float(c['end'])) for c in fixed]))
                                _fa_outdir = Path(it['audio']).parent
                                _fa_slug = Path(it['audio']).stem
                                store.write_json(_fa_outdir / f'{_fa_slug}.words.json',
                                                 {'v': 1, 'src': 'mms_fa',
                                                  'cueCount': len(fixed), 'words': _fa_words})
                                say(f"词级时间戳已落盘 .words.json（{len(_fa_words)} 词，"
                                    f"跟读页卡拉OK高亮可用）")
                            else:
                                say('本地对齐没有锚定任何句——保留现有时间轴')
                        except Exception as _fae:
                            if '已取消' in str(_fae):
                                raise
                            say(f'本地对齐失败（{str(_fae)[:90]}）——保留现有时间轴，不影响落盘')
                            _fa_map = None
                    _fast = [c for c in fixed
                             if (float(c['end']) - float(c['start'])) > 0
                             and len((c['text'] or '').split()) / (float(c['end']) - float(c['start'])) > 5.0]
                    if _fast:
                        if _fa_map:
                            say(f"ℹ {len(_fast)} 句词级语速>5词/秒（对齐后时间轴=首末词真实区间，"
                                "短促句本来就快，通常无需处理）")
                        else:
                            say(f"⚠ 仍有 {len(_fast)} 句语速>5词/秒（对齐证据不足以校准，建议用「⏱ 校准」整体平移或换句修）")
                    # ---- 不变式守卫：落盘前最后一次校对时间轴（I3 越界 / I1 兜底）----
                    # 位置必须在阶段 3.5 强制对齐之后——那一步会用词级时间戳重写 start/end，
                    # 放在它之前等于没检查。音频时长取 ffprobe 实测值（带缓存）。
                    try:
                        _audio_dur = tl.probe_duration(it['audio'])
                    except Exception:
                        _audio_dur = None
                    fixed, _viol = _enforce_invariants(fixed, _audio_dur)
                    if _viol:
                        say(f'时间轴不变式修正 {len(_viol)} 处（音频时长 '
                            f'{"%.2fs" % _audio_dur if _audio_dur else "未知"}）：'
                            f'{_viol[:6]}')
                    outdir = Path(it['audio']).parent
                    slug = Path(it['audio']).stem
                    # ---- E-09：句界被守卫夹短后，把词级时间同步夹回并重写 sidecar ----
                    # .words.json 在阶段 3.5 就写了，_enforce_invariants 之后才夹句界，
                    # 不夹回来的话前端那一句的逐词高亮会整句失效。
                    if _fa_words:
                        _fa_words, _wc = _clamp_words_to_cues(_fa_words, fixed)
                        if _wc:
                            say(f'词级时间戳按句界夹紧 {_wc} 个词（.words.json 同步重写）')
                            try:
                                store.write_json(outdir / f'{slug}.words.json',
                                                 {'v': 1, 'src': 'mms_fa',
                                                  'cueCount': len(fixed),
                                                  'words': _fa_words})
                            except Exception as _we:
                                say(f'⚠ .words.json 重写失败：{str(_we)[:70]}')
                    # 备份：单代覆盖（旧 .bak 已存在先删，WinError183 修复）
                    def _bak(p):
                        if not p.exists():
                            return
                        b = p.with_suffix(p.suffix + '.bak')
                        if b.exists():
                            b.unlink()
                        p.rename(b)
                    for suffix in ('.en.vtt', '.en.srt'):
                        _bak(outdir / (slug + suffix))
                    tl.write_srt(outdir / f'{slug}.en.srt', fixed)
                    tl.srt_to_vtt(outdir / f'{slug}.en.srt', outdir / f'{slug}.en.vtt')
                    if wins and zh_out and zh_cues:
                        # 各窗外旧中文句保留：窗口内的换成新译
                        def z_out(z):
                            return not any(float(z['start']) < we + 0.05
                                           and float(z['end']) > ws - 0.05 for ws, we in wins)
                        zh_final = sorted([z for z in zh_cues if z_out(z)] + zh_out,
                                          key=lambda x: x['start'])
                    else:
                        zh_final = zh_out
                    if _fa_map and zh_final:
                        # en 时间轴被 3.5 词级重写后，中文句按新旧窗口映射跟随：
                        # zh 窗口覆盖的 en 句集合 → 它们新窗口的并集（不覆盖的保留原值）。
                        for z in zh_final:
                            _hits = [(ns, ne) for (os_, oe_), (ns, ne) in _fa_map
                                     if os_ < float(z['end']) and oe_ > float(z['start'])]
                            if _hits:
                                z['start'] = min(h[0] for h in _hits)
                                z['end'] = max(h[1] for h in _hits)
                        say('中文时间轴已按英文新窗口同步')
                    if zh_final:
                        # 注意：中文不做句子组装（分词/标点/语速规则全是英文口径，会误杀）——
                        # 只做终局去重叠；中文按时间重叠与英文句配对，粒度差异无害
                        zh_final = _final_deoverlap(zh_final)      # 中文同样终局去重叠（乱跳的另一半根源）
                        for suffix in ('.zh.vtt', '.zh.srt'):
                            _bak(outdir / (slug + suffix))
                        tl.write_srt(outdir / f'{slug}.zh.srt', zh_final)
                        tl.srt_to_vtt(outdir / f'{slug}.zh.srt', outdir / f'{slug}.zh.vtt')
                    say(f'完成：英文 {len(fixed)} 句 · 中文 {len(zh_final or [])} 句（原字幕备份为 .bak）')
                    # 逐句难度 sidecar（听力分级用；cueCount 校验，句数不符自动回退本地规则）
                    try:
                        lv_count = sum(1 for c in fixed if c.get('level'))
                        store.write_json(outdir / f'{slug}.lvl.json',
                                         {'cueCount': len(fixed),
                                          'promptVersion': prompts.version_of('subtitle_doctor'),
                                          'levels': [c.get('level') or '' for c in fixed]})
                        if lv_count:
                            say(f'听力难度标记：{lv_count}/{len(fixed)} 句（AI 分级）')
                    except Exception:
                        pass
                    setprog('done', 100.0, '', '修正完成')
                    # id 变化兜底：按音频路径对出 old→new，前端用它同步 PR.itemId/LIB
                    try:
                        post_by_audio = {str(Path(x['audio']).resolve()).lower(): x['id']
                                         for x in tl.scan_library(self.all_dirs())}
                        id_map = {old: post_by_audio[ap] for old, ap in pre_ids.items()
                                  if post_by_audio.get(ap) and post_by_audio[ap] != old}
                        if id_map:
                            self._docgen['idMap'] = id_map
                            say('语料 id 有变化：' + '，'.join(f'{k}→{v}' for k, v in id_map.items()))
                    except Exception:
                        pass
                    self._docgen['done'] = True
                    self._docgen['running'] = False
                except Exception as ex:
                    self._docgen['error'] = str(ex)[:300]
                    self._docgen['running'] = False
                    self._docgen['done'] = True

            threading.Thread(target=run, daemon=True).start()
            return {'ok': True, 'started': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def subtitle_precheck(self, item_id):
        """只读预检（秒出、零费用）：全部句子列表 + 每句问题标记。

        给「音频和句子纠正」弹窗用：句号/时间/词数/语速/标记
        （too_long=超长 / fast=语速过快即时间戳过短 / slow=偏长 / overlap=与下一句重叠 / no_zh=缺中文）。
        """
        try:
            _it, cues = self._cues_of(item_id)
            # 字幕可见性自检：模拟前端 openPlayer 完整读取链（本地服务 URL→HTTP→解析），
            # 「看不到字幕」类问题在这里直接给结论（52-62 批排查经验固化）
            subview = {'en': 0, 'zh': 0, 'err': ''}
            try:
                import urllib.request as _ur
                _av = Path(_it['audio'])
                for _sfx, _kk in (('.en.vtt', 'en'), ('.zh.vtt', 'zh')):
                    _p = _av.parent / (_av.stem + _sfx)
                    if _p.exists():
                        _u = self._url(_p)
                        if _u:
                            with _ur.urlopen(_u, timeout=10) as _r:
                                subview[_kk] = len(tl.parse_vtt_cues(_r.read(3000000).decode('utf-8', 'replace')))
                        else:
                            subview['err'] = '本地服务无法提供该字幕 URL'
            except Exception as _e:
                subview['err'] = str(_e)[:120]
            if not cues:
                return {'ok': False, 'msg': '这个语料没有英文字幕（可先在卡片上点「⚡生成字幕」）'}
            zh = []
            zhp = _audio_sib(_it['audio'], '.zh.vtt')
            if zhp.exists():
                zh = tl.parse_vtt_cues(zhp.read_text(encoding='utf-8', errors='replace'))
            zh_has = bool(zh)
            items = []
            for i, c in enumerate(cues):
                n = len((c['text'] or '').split())
                dur = max(0.01, float(c['end'] or 0) - float(c['start'] or 0))
                rate = n / dur
                flags = []
                if n > 22:
                    flags.append('too_long')
                if rate > 5.0:
                    flags.append('fast')
                elif rate < 0.8 and n >= 4:
                    flags.append('slow')
                if i < len(cues) - 1 and float(c['end'] or 0) > float(cues[i + 1]['start'] or 0) + 0.01:
                    flags.append('overlap')
                if i > 0 and float(c['start'] or 0) - float(cues[i - 1]['end'] or 0) > 5.0:
                    flags.append('gap')       # A5：句间大空档（转写漏段/纯音乐段）可见化
                if not zh_has:
                    flags.append('no_zh')
                items.append({'index': i, 'start': round(float(c['start']), 2),
                              'end': round(float(c['end']), 2), 'words': n,
                              'rate': round(rate, 1), 'text': (c['text'] or '')[:120],
                              'flags': flags})
            # 整体统计
            n_bad = sum(1 for x in items if x['flags'])
            sync = self._sync_check(cues)
            # P3-1：ASR 对齐可用性 + 费用预估（paraformer 录音识别 ≈ 0.29 元/时）
            # 对齐有专用 paraformer 引擎（与练习识别无关）：有阿里云 Key 即可；
            # 本地强制对齐（MMS_FA/torch）可用时即使 MiMo 用户也能对齐，且零 ASR 费。
            dur = max((float(c.get('end') or 0) for c in cues), default=0.0)
            _fa_ok = False
            try:
                import local_align as _la
                _fa_ok = _la.available()
            except Exception:
                _fa_ok = False
            align_ok = False
            try:
                _t, _asr, _l = self._services()
                align_ok = ((_fa_ok
                             or bool((self.cfg.get('dashscopeApiKey') or '').strip())
                             or getattr(_asr, 'provider', lambda: '')() != 'mimo')
                            and self.cfg.get('asrAlign') is not False)
            except Exception:
                align_ok = _fa_ok
            # 时间轴退化检测（与 doctor 同口径）：出生即坏 → doctor 将自动全文重定位
            _rates = sorted(len((c.get('text') or '').split()) /
                            max(0.01, float(c.get('end') or 0) - float(c.get('start') or 0))
                            for c in cues if (c.get('text') or '').strip())
            _med = _rates[len(_rates) // 2] if _rates else 0.0
            _adur = float(_it.get('duration') or 0)
            _cov = (max((float(c.get('end') or 0) for c in cues), default=0.0)
                    - min((float(c.get('start') or 0) for c in cues), default=0.0))
            _degen = (_med > 4.2) or (_adur > 30 and _cov < _adur * 0.55)
            _base = max(dur, _adur if _degen else 0.0)
            # 本地对齐接管精对齐（2.5→3.5）：paraformer 只剩退化场景一遍，费用减半计
            align_cost = (round(_base / 3600.0 * 0.29 * (1 if (_degen or _fa_ok) else 2), 3)
                          if align_ok else None)
            # 费用预估（P1-8）：LLM 批量校对 30 句/批；按典型 token 量 × pricing 单价，估的是全文修正
            batches = (len(items) + 29) // 30
            cost = None
            priced = False
            cur = 'CNY'
            try:
                import pricing as _pricing
                _t, _a, llm = self._services()
                if llm and getattr(llm, 'ready', False):
                    # 每批典型量：输入≈系统提示120+30句×28词×1.4≈1300；输出≈30句×70≈2100
                    cost, cur, _cid, priced = _pricing.estimate(
                        (llm.cfg.get('llmModel') or ''), (llm.cfg.get('llmBaseUrl') or ''),
                        1300 * batches, 2100 * batches,
                        self.cfg.get('pricingOverrides') or [])
                else:
                    priced, cost = True, 0.0      # 没配 LLM：只做本地修正，零费用
            except Exception:
                pass
            return {'ok': True, 'subview': subview, 'total': len(items), 'badCount': n_bad,
                    'hasZh': zh_has, 'items': items,
                    'sync': {'bad': sync['bad'], 'detail': sync['detail'],
                             'advice': sync['advice']},
                    'batches': batches,
                    'cost': (round(cost, 4) if cost is not None else None),
                    'costCurrency': cur, 'costPriced': priced,
                    'alignOk': align_ok, 'alignCost': align_cost, 'alignDegenerate': _degen}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    # ========== 供应商中心 API（设置页卡片用；key 永不明文回前端） ==========
    def providers_list(self, category):
        """供应商清单（category='llm'|'asr'|'tts'），合并内置注册表与用户自定义。"""
        try:
            import speech_providers as spv
            if category not in ('llm', 'asr', 'tts'):
                return {'ok': False, 'msg': '不支持的类别'}
            out = {}
            for pid, spec in spv.PROVIDERS.get(category, {}).items():
                out[pid] = dict(spec, hasKey=bool(self._provider_key(category, pid)))
            for pid, spec in (self.cfg.get('customProviders') or {}).get(category, {}).items():
                if pid in out:                     # 自定义 id 不许撞内置
                    continue
                out[pid] = {'id': pid, 'label': spec.get('name') or pid,
                            'baseUrl': spec.get('baseUrl', ''), 'logo': (spec.get('name') or pid)[:1].upper(),
                            'canPull': True, 'builtin': spec.get('models') or [],
                            'auth': 'key', 'keyHint': '', 'docs': '', 'note': '自定义供应商',
                            'features': [], 'custom': True,
                            'hasKey': bool(self._provider_key(category, pid))}
            return {'ok': True, 'providers': out}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:120]}

    def _provider_key(self, category, pid):
        """该供应商当前有没有可用的 key（不回传明文）。"""
        try:
            import speech_providers as spv
            pdef = spv.PROVIDERS.get(category, {}).get(pid) or {}
            kn = pdef.get('keyName') or ''
            if kn and self.cfg.get(kn):
                return self.cfg.get(kn)
            uc = (self.cfg.get('speechProviders') or {}).get(pid) or {}
            return uc.get('apiKey') or ''
        except Exception:
            return ''

    def provider_models(self, category, pid, base_url='', api_key=''):
        """拉模型清单：canPull 走 GET /models，失败回落内置。"""
        try:
            import speech_providers as spv
            builtin_reg = spv.PROVIDERS.get(category, {}).get(pid, {})
            custom_reg = (self.cfg.get('customProviders') or {}).get(category, {}).get(pid, {})
            if not (builtin_reg.get('canPull') or custom_reg):
                models = [m['id'] for m in builtin_reg.get('builtin', [])]
                return {'ok': True, 'models': models, 'source': 'builtin', 'msg': ''}
            # 拉取地址：用户自定义 base 优先；传的是注册表默认 base 时换 pullBaseUrl
            # （阿里云原生 api/v1 没有 /models，得走 compatible-mode）
            reg_base = builtin_reg.get('baseUrl', '')
            pull_base = builtin_reg.get('pullBaseUrl', '')
            url = (base_url or '').strip()
            if (not url) or (reg_base and url == reg_base):
                url = pull_base or reg_base
            if not url:
                return {'ok': False, 'msg': '缺少 baseUrl'}
            if not (api_key or '').strip():
                api_key = self._provider_key(category, pid) or ''   # 前端不传 key → 用存量配置
            pulled = spv.pull_models(url, api_key, timeout=15)
            if pulled is not None:
                # /models 返回的是全量清单（绝大多数是文本对话模型）——语音类别必须
                # 按关键词过滤，否则用户在几百个模型里找不到语音模型（实测痛点）
                import re as _re
                if category == 'asr':
                    pat = _re.compile(r'asr|paraformer|sensevoice|whisper|speech[-_.]?recog|seed[-_.]?asr', _re.I)
                elif category == 'tts':
                    pat = _re.compile(r'tts|cosyvoice|speech[-_.]?syn|voice[-_.]?(clone|design)|audio[-_.]?gen|seed[-_.]?tts', _re.I)
                else:
                    pat = None
                speech_only = [m for m in pulled if not pat or pat.search(m)] if pat else pulled
                # 双保险：明确排除非合成/非识别家族（realtime=对话/asr=识别/filetrans=离线转写/embed/rerank）
                _bad = _re.compile(r'realtime|filetrans|embed|rerank', _re.I)
                if category == 'tts':
                    speech_only = [m for m in speech_only if not _bad.search(m) and 'asr' not in m.lower()]
                else:
                    speech_only = [m for m in speech_only if not _bad.search(m)]
                builtin_ids = [m['id'] for m in builtin_reg.get('builtin', [])]
                if category == 'tts':
                    # #8 兜底：拉取路径也不能把旧代模型灌进下拉——统一过供应商目录过滤
                    # （内置最新一代为准；网关还返回旧别名时由 aliases 映射拦掉）
                    try:
                        speech_only, _fnote = spv.filter_pulled_models(pid, speech_only)
                        if _fnote:
                            msg_extra = _fnote
                        else:
                            msg_extra = ''
                    except Exception:
                        msg_extra = ''
                else:
                    msg_extra = ''
                merged = list(dict.fromkeys(builtin_ids + speech_only))   # 内置保底在前 + 去重
                msg = (f'拉到 {len(pulled)} 个，过滤出 {len(speech_only)} 个语音模型'
                       + (f'（另并入内置 {len(builtin_ids)} 个）' if speech_only else '（用内置清单兜底）')
                       + (f'；{msg_extra}' if msg_extra else ''))
                return {'ok': True, 'models': merged, 'source': 'pull', 'msg': msg}
            models = [m['id'] for m in (builtin_reg.get('builtin') or custom_reg.get('models') or [])]
            return {'ok': True, 'models': models, 'source': 'builtin',
                    'msg': '拉取 /models 失败，已回落内置清单'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:120]}

    def provider_speed_test(self, category, pid, base_url='', api_key='', model=''):
        """实测一次最小调用：LLM=聊天/ASR=识别或连通/TTS=合成或连通。返回 {ok,ms,msg}。"""
        try:
            import speech_providers as spv
            if category not in ('llm', 'asr', 'tts'):
                return {'ok': False, 'msg': '不支持的类别'}
            # 没传 key 的走存量配置（前端只想"再测一次"时不必重填）
            if not (api_key or '').strip():
                api_key = self._provider_key(category, pid) or ''
            if not (base_url or '').strip():
                pdef = spv.PROVIDERS.get(category, {}).get(pid) or {}
                base_url = pdef.get('baseUrl', '')
            if category == 'llm':
                ms = spv.speed_test_llm(base_url, api_key, model)
                return {'ok': True, 'ms': ms, 'msg': f'往返 {ms} ms'}
            if category == 'asr':
                ms, real = spv.speed_test_asr(pid, base_url, api_key, model, tl.FFMPEG)
                return {'ok': True, 'ms': ms,
                        'msg': (f'真识别 {ms} ms' if real else f'连通 {ms} ms（非识别延迟）')}
            ms, real = spv.speed_test_tts(pid, base_url, api_key, model, tl.FFMPEG)
            return {'ok': True, 'ms': ms,
                    'msg': (f'真合成 {ms} ms' if real else f'连通 {ms} ms（非合成延迟）')}
        except Exception as e:
            return {'ok': False, 'ms': -1, 'msg': str(e)[:140]}

    def providers_save_custom(self, category, spec):
        """保存自定义供应商 {id,name,baseUrl,models?[]} 到 cfg.customProviders。"""
        try:
            if category not in ('llm', 'asr', 'tts'):
                return {'ok': False, 'msg': '不支持的类别'}
            cid = str(spec.get('id') or '').strip()
            if not cid or not cid.replace('_', '').isalnum():
                return {'ok': False, 'msg': 'id 只能字母数字下划线'}
            import speech_providers as spv
            if cid in spv.PROVIDERS.get(category, {}):
                return {'ok': False, 'msg': 'id 与内置供应商重名'}
            cfg = tl.load_config()
            cfg.setdefault('customProviders', {}).setdefault(category, {})[cid] = {
                'id': cid, 'name': str(spec.get('name') or cid)[:30],
                'baseUrl': str(spec.get('baseUrl') or '').strip()[:200],
                'models': [str(m)[:80] for m in (spec.get('models') or [])][:40],
            }
            tl.save_config(cfg)
            self.cfg = cfg
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:120]}

    def providers_remove_custom(self, category, cid):
        """删除自定义供应商。"""
        try:
            if category not in ('llm', 'asr', 'tts'):
                return {'ok': False, 'msg': '不支持的类别'}
            cfg = tl.load_config()
            try:
                cfg.get('customProviders', {}).get(category, {}).pop(cid, None)
                tl.save_config(cfg)
                self.cfg = cfg
            except Exception:
                pass
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'msg': str(e)[:120]}

    def subtitle_doctor_pause(self, item_id=''):
        """暂停正在跑的诊断修正（批次间挂起，已完成批次保留；落盘前也会停住）。"""
        st = getattr(self, '_docgen', None) or {}
        if not st.get('running'):
            return {'ok': False, 'msg': '当前没有在跑的修正任务'}
        if item_id and st.get('item') != item_id:
            return {'ok': False, 'msg': '任务不匹配'}
        st['pause'].clear()
        st['log'].append('⏸ 已暂停（当前批次完成后挂起，点 ▶ 继续）')
        st['progress'] = {**st.get('progress', {}), 'detail': '已暂停'}
        return {'ok': True, 'paused': True}

    def subtitle_doctor_cancel(self, item_id=''):
        """取消正在跑的诊断修正：在最近的检查点（批次间/落盘前）中止，
        **不落盘**——字幕保持原样，已花的 LLM/ASR 费用不退。"""
        st = getattr(self, '_docgen', None) or {}
        if not st.get('running'):
            return {'ok': False, 'msg': '当前没有在跑的修正任务'}
        if item_id and st.get('item') != item_id:
            return {'ok': False, 'msg': '任务不匹配'}
        st.setdefault('stop', threading.Event()).set()
        st['pause'].set()                    # 若正暂停着，先放行才能走到检查点
        st['log'].append('⏹ 收到取消请求：将在当前批次后中止（不落盘，字幕保持原样）')
        st['progress'] = {**st.get('progress', {}), 'detail': '正在取消…'}
        return {'ok': True, 'cancelling': True}

    def subtitle_doctor_resume(self, item_id=''):
        """继续被暂停的诊断修正。"""
        st = getattr(self, '_docgen', None) or {}
        if not st.get('running'):
            return {'ok': False, 'msg': '当前没有在跑的修正任务'}
        if item_id and st.get('item') != item_id:
            return {'ok': False, 'msg': '任务不匹配'}
        st['pause'].set()
        st['log'].append('▶ 已继续')
        return {'ok': True, 'paused': False}

    def doctor_job_status(self, item_id=''):
        st = getattr(self, '_docgen', None) or {}
        if item_id and st.get('item') != item_id:
            return {'ok': True, 'running': False, 'log': [], 'done': False, 'error': '',
                    'progress': None, 'idMap': {}, 'paused': False}
        paused = False
        if st.get('running'):
            try:
                paused = not st['pause'].is_set()
            except Exception:
                paused = False
        return {'ok': True,
                **{k: st.get(k) for k in ('running', 'log', 'done', 'error', 'progress', 'idMap')},
                'paused': paused}

    # ---------- 每日提示 ----------
    def daily_tip(self, shuffle=False):
        try:
            if shuffle:
                import random as _rd
                return {'ok': True, 'tip': _rd.choice(DAILY_TIPS)}
            import hashlib as _hl
            seed = datetime.now().strftime('%Y-%m-%d')
            idx = int(_hl.md5(seed.encode()).hexdigest(), 16) % len(DAILY_TIPS)
            return {'ok': True, 'tip': DAILY_TIPS[idx], 'total': len(DAILY_TIPS)}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def download_probe(self, url):
        """下载页「测试连通」：判断链接是否可连通、字幕是否可取、建议直连还是代理。"""
        url = (url or '').strip()
        if not url.startswith('http'):
            return {'ok': False, 'msg': '请先填一个 TED / B站 / YouTube 链接'}
        plat = videosrc.detect_platform(url)
        checks = []
        def add(name, ok, detail=''):
            checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:200]})
        # 0) 代理环境
        proxy_used = ''
        try:
            import proxydetect
            r = proxydetect.detect_proxy()
            proxy_used = r.get('proxy') or ''
            add('代理环境', True,
                ('自动检测到 ' + proxy_used + '（' + (r.get('via') or '') + '）') if proxy_used
                else '未检测到代理（B站不受影响；TED/YouTube 直连可能偏慢）')
        except Exception as e:
            add('代理环境', False, str(e)[:120])
        try:
            if plat == 'bilibili':
                import re as _re
                import urllib.request
                m = _re.search(r'BV[0-9A-Za-z]{10}', url)
                target = url
                if not m and 'b23.tv' in url:
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        target = resp.geturl()
                    m = _re.search(r'BV[0-9A-Za-z]{10}', target)
                if not m:
                    add('B站解析', False, '没解析到 BV 号')
                else:
                    api = 'https://api.bilibili.com/x/web-interface/view?bvid=' + m.group(0)
                    req = urllib.request.Request(api, headers={
                        'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        j = json.loads(resp.read().decode('utf-8', 'replace'))
                    if j.get('code') == 0:
                        d = j.get('data') or {}
                        dur = int(d.get('duration') or 0)
                        add('B站连通', True,
                            '《' + str(d.get('title') or '')[:70] + '》 ' +
                            str(dur // 60) + '分' + str(dur % 60) + '秒')
                        add('字幕提示', True, '下载时会自动抓字幕；没有字幕会自动语音转写+翻译')
                    else:
                        add('B站连通', False, str(j.get('message') or ('code=' + str(j.get('code')))))
            elif plat == 'youtube':
                ck = videosrc._auto_cookies()
                pv = (self.cfg.get('downloadProxy') or '').strip().lower()
                if pv in ('', 'auto'):
                    import proxydetect as _pd
                    proxy_used = _pd.detect_proxy().get('proxy') or ''
                elif pv in ('直连', 'direct', 'off', 'none'):
                    proxy_used = ''
                else:
                    proxy_used = pv
                extra = videosrc._yt_extra_args(proxy_used, ck,
                                                'youtube:player_client=android,web_safari')
                info = videosrc._ytdlp_meta(url, extra)
                add('YouTube 解析', True,
                    '《' + str(info.get('title') or '')[:80] + '》 ' +
                    str(int(info.get('duration') or 0) // 60) + ' 分钟')
                subs = list((info.get('subtitles') or {}).keys())
                add('字幕', True, ('有手工字幕：' + ','.join(subs[:6])) if subs
                    else '无手工字幕（下载时会自动语音转写+翻译）')
            else:
                info = tl.parse_ted_page(url)
                en, zh = tl.pick_langs(info.get('cues') or {})
                add('TED 解析', True,
                    '《' + str(info.get('title') or '')[:70] + '》 ' +
                    str((info.get('duration') or 0) // 60) + ' 分钟 · 英文字幕 ' +
                    str(len(en) if en else 0) + ' 句 · 中文字幕 ' + str(len(zh) if zh else 0) + ' 句')
                if not en:
                    add('字幕警告', False, '这场在 TED 上没有字幕，将自动语音转写（时间较长）')
        except Exception as e:
            add((plat or '站点') + ' 解析', False, str(e)[:200])
        bad = [c for c in checks if not c.get('ok')]
        return {'ok': True, 'platform': plat or 'ted', 'checks': checks,
                'summary': ('全部通过，可以下载' if not bad
                            else '有 ' + str(len(bad)) + ' 项未通过：' + '、'.join(c['name'] for c in bad))}

    def use_proxy_fallback():
        return ''

    def proxy_detect(self):
        """手动触发一次代理探测（下载页「检测」按钮）。"""
        try:
            import proxydetect
            r = proxydetect.detect_proxy()
            if r['proxy']:
                tag = '已验证可出国际网' if r['verified'] else '端口开放但未验证通过'
                return {'ok': True, 'found': True, 'proxy': r['proxy'], 'via': r['via'],
                        'verified': r['verified'], 'msg': f"发现代理 {r['proxy']}（{r['via']}，{tag}）"}
            return {'ok': True, 'found': False, 'proxy': '', 'via': '', 'verified': False,
                    'msg': '未检测到本机代理（常见端口和系统代理都没有）'}
        except Exception as e:
            return {'ok': False, 'found': False, 'msg': str(e)}

    # ---------------- 统计
    def stats(self):
        items = tl.scan_library(self.all_dirs())
        total_sec = sum((x.get('duration') or 0) for x in items)
        total_size = sum(x.get('size') or 0 for x in items)
        return {
            'count': len(items),
            'hours': round(total_sec / 3600, 1),
            'sizeMB': round(total_size / 1024 / 1024, 1),
            'withZh': sum(1 for x in items if x['hasZh']),
        }


def main():
    # 冻结后路径最容易出问题，先把关键路径落盘一份，排查时直接看这个文件
    try:
        import sys as _s
        import json as _j
        diag = {
            'frozen': bool(getattr(_s, 'frozen', False)),
            'meipass': str(getattr(_s, '_MEIPASS', '')),
            'executable': _s.executable,
            'app_dir': str(APP_DIR),
            'lib_app_dir': str(tl.APP_DIR),
            'config_path': str(tl.CONFIG),
            'config_exists': tl.CONFIG.exists(),
            'ui_dir': str(_ui_dir()),
            'ui_index_exists': (_ui_dir() / 'index.html').exists(),
            'libraryDir': tl.load_config().get('libraryDir', ''),
        }
        (tl.APP_DIR / '_diag.json').write_text(
            _j.dumps(diag, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception:
        pass

    # 可选参数：启动时直接落到某个标签页 / 直接打开某个语料（方便做桌面快捷方式）
    import argparse
    ap = argparse.ArgumentParser(description='听与读')
    ap.add_argument('--tab', default='',
                    choices=['', 'home', 'lib', 'practice', 'listen', 'ph', 'tasks', 'vocab', 'sent', 'dl', 'usage', 'help', 'set', 'play'],
                    help='启动时打开哪个标签页')
    ap.add_argument('--play', default='', help='启动后直接打开标题含该关键词的语料（跟读页）')
    ap.add_argument('--shot', default='', help='自检：页面就绪后把窗口截图存到这个路径')
    ap.add_argument('--shot-delay', type=float, default=10.0, help='截图前等多少秒（默认 10）')
    args, _unknown = ap.parse_known_args()

    # 先起本地文件服务：界面与媒体都走 http://127.0.0.1:port，
    # 比 file:// 在 WebView2 里可靠得多（音频 Range 请求、字幕 fetch 都正常）。
    ui = _ui_dir()
    cfg = tl.load_config()
    roots = [str(ui), cfg.get('libraryDir', '')] + list(cfg.get('extraDirs', []))
    server = localfs.LocalServer(roots)
    port = server.start()

    api = Api(server)
    api._refresh_roots()
    api.boot_tab = args.tab
    api.boot_play = args.play

    window = webview.create_window(
        '听与读',
        url=f'{server.base()}/index.html',
        js_api=api,
        width=1380, height=920, min_size=(1080, 660),
        background_color='#0b0e13', text_select=True,
    )

    # 自检模式：页面渲染完成后截一张图，用于验证界面（绕开外部截图工具的坑）
    if args.shot:
        def _auto_shot():
            time.sleep(args.shot_delay)
            try:
                Path(args.shot).parent.mkdir(parents=True, exist_ok=True)
                window.resize(1400, 900)
            except Exception:
                pass
            time.sleep(1.0)
            try:
                if getattr(window, 'screenshot', None) is None:
                    print('这个 pywebview 版本不支持 screenshot()')
                else:
                    window.screenshot(args.shot)
                    print('截图已保存:', args.shot)
            except Exception as e:
                print('截图失败:', e)
            # 自检模式：存完就退出，避免进程一直挂着
            try:
                window.destroy()
            except Exception:
                pass
        threading.Thread(target=_auto_shot, daemon=True).start()
    try:
        webview.start(debug=False, private_mode=False)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    finally:
        server.stop()


if __name__ == '__main__':
    main()
