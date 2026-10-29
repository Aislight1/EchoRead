#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音识别（ASR）+ 本地录音

识别走阿里云 DashScope 的 paraformer-realtime-v2 ——
它最大的价值是**返回逐词时间戳**（words[].begin_time / end_time），
这是"指出到底哪个词读错了"的基础。

实测返回结构（2026-10）：
    {"sentence": [{"text": "So I'll start with this.", "begin_time": 0, "end_time": 3260,
                   "words": [{"text": "So ", "begin_time": 0, "end_time": 652}, ...]}]}
"""
import audioop
import json
import re
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

SAMPLE_RATE = 16000
CHANNELS = 1


# ---------------------------------------------------------------- 录音
class Recorder:
    """本地麦克风录音。可查询设备、可自检。"""

    def __init__(self, log=None):
        self.log = log
        self._rec = None
        self._stream = None
        self._frames = []
        self._running = False
        self._level = 0.0
        self.device = None
        self._lock = threading.Lock()

    def _say(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    @staticmethod
    def available():
        try:
            import sounddevice  # noqa
            return True, ''
        except ImportError:
            return False, '缺少 sounddevice：pip install sounddevice'
        except Exception as e:
            return False, str(e)

    @staticmethod
    def list_devices():
        """列出可用输入设备。

        注意：值必须转成原生 int/str —— sounddevice 返回的是 numpy 标量，
        直接交给 webview 的 JSON 序列化会报
        "Object of type ... is not JSON serializable"。
        """
        try:
            import sounddevice as sd
            out = []
            for d in sd.query_devices():
                if int(d['max_input_channels']) > 0:
                    out.append({'index': int(d['index']), 'name': str(d['name']),
                                'channels': int(d['max_input_channels']),
                                'sr': int(d['default_samplerate'])})
            default = sd.default.device
            if isinstance(default, (list, tuple)):
                default = default[0]
            try:
                default = int(default)
            except (TypeError, ValueError):
                default = -1
            return {'ok': True, 'devices': out, 'default': default}
        except Exception as e:
            return {'ok': False, 'msg': str(e), 'devices': []}

    def start(self, device=None):
        """开始录音（累积到内存）。"""
        ok, msg = self.available()
        if not ok:
            raise RuntimeError(msg)
        import sounddevice as sd
        with self._lock:
            if self._running:
                return
            self._frames = []
            self._level = 0.0
            self._running = True
            self.device = device

        def cb(indata, frames, time_info, status):
            if not self._running:
                return
            self._frames.append(bytes(indata))
            try:
                self._level = audioop.rms(indata, 2) / 32768.0
            except Exception:
                self._level = 0.0

        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='int16',
            device=device, callback=cb, blocksize=1024)
        self._stream.start()
        self._say('开始录音…')

    def level(self):
        """当前音量 0~1，给界面画电平条。"""
        return round(self._level, 4)

    def stop(self, out_path):
        """停止录音并写成 WAV，返回 (路径, 时长秒, 峰值)。"""
        with self._lock:
            self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        data = b''.join(self._frames)
        self._frames = []
        if not data:
            raise RuntimeError('没录到声音（检查麦克风设备是否选对）')

        peak = 0
        try:
            peak = audioop.max(data, 2) / 32768.0
        except Exception:
            peak = 0.0

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), 'wb') as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(data)
        dur = len(data) / (SAMPLE_RATE * CHANNELS * 2)
        self._say(f'录音结束：{dur:.1f} 秒，峰值 {peak:.2f}')
        return str(out), round(dur, 2), round(peak, 3)


# ---------------------------------------------------------------- 识别
def _part_words(t):
    return set(w for w in re.split(r"[^a-z0-9']+", (t or '').lower()) if w)


def _merge_part_sentences(bounds, results):
    """并行切片转写结果合并 v2（治 67集 A-A-B-A-C 交错重复的根）。

    跨边界句在相邻两片里必然各出现一版：前片版常被片尾截断（半句），
    后片版因 1.5s 重叠有上下文而完整。判重不靠 (start,text) 精确键——
    两片断句不可能一致——而是：绝对时间排序后，只与**最近 3 句**比
    （跨界重复必在邻域），"时间重叠>0.3s 或起点差<2s" 且 "词集覆盖率
    ≥0.55"（半句词 ⊂ 全句词 → 覆盖率 1.0）视为同一句，**保词多/更长的
    一版**。远处正常重复的口头禅（时间不邻）不受影响。
    """
    all_s = []
    for i, (s0, _s1) in enumerate(bounds):
        for s in (results[i] or {}).get('sentences') or []:
            txt = (s.get('text') or '').strip()
            if not txt:
                continue
            all_s.append({'text': txt,
                          'start': round(float(s.get('start') or 0) + s0, 3),
                          'end': round(float(s.get('end') or 0) + s0, 3)})
    all_s.sort(key=lambda x: x['start'])
    out = []
    for s in all_s:
        sset = _part_words(s['text'])
        s_words_n = len(sset)
        s_dur = s['end'] - s['start']
        sims = []                          # 邻域内判为"同一句"的既有句下标
        for j in range(len(out) - 1, max(-1, len(out) - 4), -1):
            t = out[j]
            ov = min(s['end'], t['end']) - max(s['start'], t['start'])
            if ov < 0.25:
                continue                     # 不重叠/仅抖动式擦边 → 绝不是跨片重复
            tw = _part_words(t['text'])
            if not sset or not tw:
                continue
            if min(len(sset), len(tw)) <= 2:
                continue                     # "Yeah./Okay." 这类应答短句不参与判重（教学场景易误杀）
            sim = len(sset & tw) / max(1, min(len(sset), len(tw)))
            da = abs(s['start'] - t['start'])
            # 双门槛：大范围重叠+中相似；或轻微重叠但起点几乎重合（同一物理语音）+高相似。
            # 教学播客连续句起首再像，起点差=前句时长（秒级）且基本不重叠 → 不进门。
            if (ov >= 0.4 and sim >= 0.6) or (ov >= 0.25 and da <= 1.5 and sim >= 0.75):
                sims.append(j)
        if not sims:
            out.append(s)
            continue
        # 新句比任一相似句更完整 → 新句是全句版：相似句（半句/碎片）全删，换上新句；
        # 否则已有版本更完整 → 丢弃新句。半句+碎片+全句三连交错即在此收敛。
        if any((s_words_n, s_dur) > (len(_part_words(out[j]['text'])),
                                     out[j]['end'] - out[j]['start']) for j in sims):
            for j in sorted(sims, reverse=True):
                del out[j]
            out.append(s)
            out.sort(key=lambda x: x['start'])
    return _enforce_monotonic(out)


def _enforce_monotonic(sents, tol=0.02, min_dur=0.2):
    """合并后全局单调化（E-08）：消除跨片残留重叠与塌缩。

    `_merge_part_sentences` 只按词集相似度删跨片重复句，删完**时间轴不保证单调**：
    相邻两片各自断出的长句可在边界处交叠 1~1.5s（约等于 ASR 的 overlap 宽度），
    前端按 start 找当前句就会出现「高亮已跳到下一句、音频还在上一句」。

    规则（与 app._enforce_invariants 的 I1/I2 同口径）：
      · start[i] >= end[i-1] - tol
      · end[i] - start[i] >= min_dur
    优先压缩前一句的尾巴（它本来就是跨片冗余的重叠部分），
    前一句压到 min_dur 仍不够时，才把后一句整体后推。
    原地修改并返回 sents（调用方传入的都是本函数新建的 dict，可安全改写）。
    """
    if not sents:
        return sents
    sents.sort(key=lambda x: x['start'])
    # 首句没有"前一句"可对齐，塌缩要单独兜底（否则 end<=start 的首句会漏网）
    if sents[0]['end'] - sents[0]['start'] < min_dur:
        sents[0]['end'] = round(sents[0]['start'] + min_dur, 3)
    for i in range(1, len(sents)):
        p, c = sents[i - 1], sents[i]
        if c['start'] < p['end'] - tol:
            cut = max(p['start'] + min_dur, c['start'])   # 前句至少留 min_dur
            if cut < p['end']:
                p['end'] = round(cut, 3)
            if c['start'] < p['end'] - tol:               # 前句已到最短仍重叠 → 后句后推
                c['start'] = round(p['end'], 3)
        if c['end'] - c['start'] < min_dur:               # 塌缩句兜底
            c['end'] = round(c['start'] + min_dur, 3)
    return sents


class ASR:
    """语音识别。两个 provider：

      dashscope（默认）阿里云 paraformer —— 强项是**返回逐词时间戳**，
                          这是判断"哪个词读错"的基础，发音练习用它。
      mimo              小米 MiMo-V2.5-ASR —— OpenAI 兼容协议，返回纯文本。
                          注意：**只能传音频、不能带文字**，
                          网关自己注入提示词（否则报 "ASR request must not include text parts"）。
    """

    def __init__(self, cfg=None, log=None):
        self.cfg = cfg or {}
        self.log = log
        # 长音频转写模式：traditional / parallel / gummy（设置页可选）
        self.mode_hint = (self.cfg.get('asrMode') or 'traditional').lower()
        self._ready = False
        self._client = None

    def _say(self, m):
        if self.log:
            try:
                self.log(m)
            except Exception:
                pass

    def provider(self):
        return (self.cfg.get('asrProvider') or 'dashscope').lower()

    # ---------- MiMo ASR
    def _mimo_client(self):
        if self._client is not None:
            return self._client
        key = (self.cfg.get('mimoApiKey') or '').strip()
        if not key:
            raise RuntimeError('MiMo 识别需要 MiMo API Key（设置 → 语音合成 里填的那个）')
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError('缺少 openai 库：pip install openai')
        base = (self.cfg.get('mimoBaseUrl') or 'https://api.xiaomimimo.com/v1').strip()
        self._client = OpenAI(api_key=key, base_url=base, timeout=120)
        return self._client

    def _mimo_recognize(self, wav_path):
        import base64
        import subprocess
        model = (self.cfg.get('mimoAsrModel') or '').strip()
        # MiMo V2.5 已于 2026-10-21 下线：不再默认拿死模型去打请求
        try:
            import speech_providers as _spv
            retired = {str(x).lower() for x in getattr(_spv, 'MIMO_RETIRED_MODELS', set())}
            if not model or model.lower() in retired:
                raise RuntimeError(getattr(_spv, 'MIMO_RETIRED_NOTE',
                                           'MiMo V2.5 已下线，请改用 paraformer-realtime-v2'))
        except RuntimeError:
            raise
        except Exception:
            if not model:
                raise RuntimeError('MiMo ASR 模型未配置（V2.5 已下线，请改用 paraformer-realtime-v2）')
        self._say(f'MiMo 识别中…（{model}）')
        audio_path = Path(wav_path)
        fmt = 'wav'
        # MiMo 限制 input_audio ≤10MB：超限自动压成 32k 单声道 mp3（约覆盖 40 分钟录音）
        try:
            if audio_path.stat().st_size > 9 * 1024 * 1024:
                import tedlibrarian as _tl
                if _tl.FFMPEG:
                    self._say('录音较长，自动压缩后识别…')
                    mp3 = audio_path.with_suffix('.asr.mp3')
                    r = subprocess.run(
                        [_tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                         '-i', str(audio_path), '-ac', '1', '-ar', '16000',
                         '-b:a', '32k', str(mp3)],
                        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    if mp3.exists() and 0 < mp3.stat().st_size < 9.5 * 1024 * 1024:
                        audio_path, fmt = mp3, 'mp3'
                    else:
                        mp3.unlink(missing_ok=True)
        except Exception:
            pass
        if audio_path.stat().st_size > 10 * 1024 * 1024:
            raise RuntimeError('录音超过 MiMo 10MB 上限且压缩后仍超限，请缩短录音或在设置里改用阿里云识别')
        b64 = base64.b64encode(audio_path.read_bytes()).decode()
        c = self._mimo_client().chat.completions.create(
            model=model,
            messages=[{'role': 'user', 'content': [
                {'type': 'input_audio', 'input_audio': {'data': b64, 'format': fmt}}]}],
        )
        try:
            if fmt == 'mp3':
                audio_path.unlink(missing_ok=True)
        except Exception:
            pass
        text = (c.choices[0].message.content or '').strip()
        return {'text': text, 'words': [],
                'sentences': [{'text': text, 'start': 0, 'end': 0}] if text else []}

    # ---------- 阿里云
    def _prepare(self):
        key = (self.cfg.get('dashscopeApiKey') or '').strip()
        if not key:
            raise RuntimeError('还没填阿里云 DashScope API Key（设置 → 语音识别 里填）')
        import os
        os.environ['DASHSCOPE_API_KEY'] = key
        try:
            import dashscope  # noqa
        except ImportError:
            raise RuntimeError('缺少 dashscope 库：pip install dashscope')
        self._ready = True

    def _dashscope_recognize(self, wav_path, language='en'):
        self._prepare()
        from dashscope.audio.asr import Recognition

        model = (self.cfg.get('asrModel') or 'paraformer-realtime-v2').strip()
        self._say(f'阿里云识别中…（{model}）')
        vid = self._vocab_id()
        if vid:
            self._say(f'带热词表 {vid[:16]}…（{len(self.cfg.get("asrHotwords") or [])} 词）')
        rec = Recognition(
            model=model,
            format='wav',
            sample_rate=SAMPLE_RATE,
            language_hints=[language] if language else None,
            callback=None,
            **({'vocabulary_id': vid} if vid else {}),
        )
        res = rec.call(file=str(wav_path))
        code = getattr(res, 'status_code', 0)
        if code != 200:
            raise RuntimeError(f"识别失败（{code}）：{getattr(res, 'message', '')}")

        out = getattr(res, 'output', None) or {}
        return self._parse(out)

    # ---------- 统一入口
    def recognize(self, wav_path, language='en', provider=None):
        """识别本地 WAV，返回 {text, words:[{text,start,end}], sentences:[...]}。
        模型分发（供应商中心）：qwen3-asr* → OpenAI 兼容 chat 口；
        qwen-audio-3.1-asr* → 多模态口（实验）；seed-ASR* → 豆包未接（给指引）；
        其余按 provider 走 Recognition/mimo。报错统一过翻译层（错误码+人话提示）。"""
        p = (provider or self.provider()).lower()
        model = (self.cfg.get('asrModel') or '').strip()
        try:
            ml = model.lower()
            if ('qwen3-asr' in ml and 'realtime' not in ml and 'filetrans' not in ml) or 'omni' in ml:
                return self._qwen3_compat_recognize(wav_path, language)
            if 'qwen-audio-3.1-asr' in model:
                return self._qwen3_recognize(wav_path)
            if 'seed-asr' in model.lower():
                raise RuntimeError('豆包引擎未接：需在火山控制台·豆包语音·API 服务中心申请 AppID+Access Token，'
                                   '并在设置 → 供应商卡片填好后再试（当前为接口预留）')
            if p == 'mimo':
                return self._mimo_recognize(wav_path)
            return self._dashscope_recognize(wav_path, language)
        except RuntimeError as e:
            import speech_providers as _spv
            raise RuntimeError(_spv.err_hint(model, str(e), 'asr')) from None

    def _qwen3_recognize(self, wav_path):
        """Qwen-Audio-3.1-ASR（实验·多模态口）。无词级时间戳——words 留空，
        对齐器见空词表会自动保留原时间戳，不会拿假数据硬对。"""
        self._prepare()
        from dashscope import MultiModalConversation
        import base64
        model = (self.cfg.get('asrModel') or 'qwen-audio-3.1-asr-flash-message').strip()
        self._say(f'Qwen-Audio-3.1 转写中…（{model}，实验）')
        wav_b64 = base64.b64encode(Path(wav_path).read_bytes()).decode('utf-8')
        messages = [{'role': 'user', 'content': [
            {'audio': f'data:audio/wav;base64,{wav_b64}'},
            {'text': 'Transcribe this audio. Output plain text only.'}]}]
        vid = self._vocab_id()
        kw = {'vocabulary_id': vid} if vid else {}
        res = MultiModalConversation.call(model=model, messages=messages, **kw)
        code = getattr(res, 'status_code', 0)
        if code != 200:
            raise RuntimeError(f'转写失败（{code}）：{str(getattr(res, "message", ""))[:120]}（实验特性）')
        out = getattr(res, 'output', None) or {}
        choices = out.get('choices', []) if isinstance(out, dict) else []
        if not choices:
            raise RuntimeError('未返回识别结果')
        content = (choices[0].get('message', {}) or {}).get('content') or ['']
        text = (content[0].get('text', '') if isinstance(content[0], dict) else str(content[0])).strip()
        if not text:
            raise RuntimeError('转写结果为空')
        return {'text': text, 'words': [], 'sentences': [{'text': text, 'start': 0, 'end': 0}]}

    def _qwen3_compat_recognize(self, wav_path, language='en'):
        """文件转写型模型（OpenAI 兼容 chat 口，base64 音频直传，官方口径 2026-10）：
        - qwen3-asr-flash：messages 带 input_audio + 顶层 asr_options{language,enable_itn}
        - qwen3.8-omni-flash 等 omni 系：不带 asr_options（不认），加 text 指令引导转写；
          多模态理解型模型，长音频文件转写质量优于 paraformer-realtime（实时流式模型）
          ——56 批实测：20s 音频 10s 出稿、转写准确；asr-flash-message 只收公网 URL 已弃。
        两系均无词级时间戳 → 按句号切句 + 2.5 词/秒线性插值（后续 doctor 阶段
        MMS_FA/paraformer 对齐定稿校准，转写与对齐解耦）。
        enable_itn 默认关：字幕要"按读音拼写"（five 而不是 5）。"""
        key = (self.cfg.get('dashscopeApiKey') or '').strip()
        if not key:
            raise RuntimeError('还没填阿里云 DashScope API Key（设置 → 语音识别 里填）')
        import base64
        import json as _json
        import re as _re
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        host = (self.cfg.get('dashscopeBaseUrl') or 'https://dashscope.aliyuncs.com').rstrip('/')
        model = (self.cfg.get('asrModel') or 'qwen3-asr-flash').strip()
        is_omni = 'omni' in model.lower()
        wav = Path(wav_path)
        if wav.stat().st_size > 10 * 1024 * 1024:
            # 正常路径永不走到这里：videosrc.plan_asr_route 会先把长音频改道
            # recognize_parallel（4 分钟静音切片）。命中说明调用方**绕过了统一路由**
            # 直接 recognize 整段——报错里明说，便于定位绕行入口（#6 审计用）。
            raise RuntimeError('这段音频超过文件转写型模型的单次上限（10MB/约 5 分钟），'
                               '且该入口没走自动分片（recognize_parallel）。'
                               '请改走 videosrc._asr_to_cues，或换 paraformer')
        b64 = base64.b64encode(wav.read_bytes()).decode()
        content = [{'type': 'input_audio',
                    'input_audio': {'data': f'data:audio/wav;base64,{b64}'}}]
        if is_omni:
            lang_txt = '中文' if language == 'zh' else '英文'
            content.append({'type': 'text',
                            'text': f'请逐字转写这段{lang_txt}音频，只输出转写文本，不要解释。'})
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': content}],
            'stream': False,
        }
        if not is_omni:
            payload['asr_options'] = {'language': 'zh' if language == 'zh' else 'en',
                                      'enable_itn': False}
        self._say(f'文件转写中…（{model}）')
        req = Request(host + '/compatible-mode/v1/chat/completions',
                      data=_json.dumps(payload).encode('utf-8'), method='POST',
                      headers={'Authorization': f'Bearer {key}',
                               'Content-Type': 'application/json'})
        try:
            with urlopen(req, timeout=300) as resp:
                res = _json.loads(resp.read().decode('utf-8'))
        except HTTPError as e:
            detail = ''
            try:
                detail = (e.read().decode('utf-8') or '')[:200]
            except Exception:
                pass
            raise RuntimeError(f'HTTP {e.code}: {detail or e.reason}')
        choices = res.get('choices') or []
        if not choices:
            err = res.get('error') or {}
            raise RuntimeError(f"未返回识别结果：{(err.get('message') or str(res))[:150]}")
        text = (((choices[0].get('message') or {}).get('content')) or '').strip()
        if not text:
            raise RuntimeError('识别结果为空（再试一次，说响一点）')
        # 无词级时间戳 → 按句号切句 + 2.5 词/秒线性插值（doctor 对齐环节会校准）
        dur = self._wav_duration(wav)
        parts = [p.strip() for p in _re.split(r'(?<=[.!?])\s+', text) if p.strip()] or [text]
        sents, t = [], 0.0
        total_w = sum(len(p.split()) for p in parts) or 1
        for p in parts:
            d = max(0.6, len(p.split()) / 2.5)
            sents.append({'text': p, 'start': round(t, 3), 'end': round(t + d, 3)})
            t += d
        if sents and dur > 0:
            # 以真实音频时长为锚整体缩放：k = 真实时长 / 插值总长，本质是"静音占比"
            # 的补偿系数。旧版只在 0.5<k<2.0 时缩放——94 分钟长音频静音多，k 常 >2，
            # 反而**完全不缩放** → 全部字幕挤在音频前 1/3，越往后越对不上
            # （#1「音频与句子不同步」的根因之一）。现在一律按 k 缩放，只把 k 夹在
            # [0.5, 8.0] 防坏数值；缩放后末句 end ≈ 真实时长。
            k = dur / max(t, 0.001)
            k = min(max(k, 0.5), 8.0)
            sents = [{'text': s['text'], 'start': round(s['start'] * k, 3),
                      'end': round(s['end'] * k, 3)} for s in sents]
            self._say(f'无词级时间戳：按 {k:.2f}× 把插值时间轴锚到真实时长 {dur:.1f}s')
        return {'text': text, 'words': [], 'sentences': sents}

    # ---------- 热词绑定（预编译词表 vocabulary_id：免费，target_model 须与识别模型一致）
    def _vocab_id(self):
        """当前应绑定的热词表 id：开关关/没建过表 → 空串。
        绑定点=paraformer Recognition 与 qwen-audio-3.1-asr 多模态口
        （qwen3-asr 兼容口官方未开热词）。"""
        if not self.cfg.get('asrHotwordOn'):
            return ''
        return (self.cfg.get('asrVocabularyId') or '').strip()

    # ---------- 长音频三模式（传统 recognize / 分片并行 / Gummy 一体）
    def recognize_parallel(self, wav_path, language='en', workers=4, log=None):
        """分片并行转写：沿静音点切 3~5 分钟片段，多路并发 recognize，按偏移合并。

        提速约 min(workers, 片数) 倍。**局部失败不许拖垮整体**：单片失败错峰串行
        重试一次 → 仍失败按半长重切再试两轮（下界 90s）→ 全部失败才整段回退
        paraformer（带逐词时间戳）。返回结构与 recognize 相同。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import subprocess as _sp
        import tedlibrarian as _tl
        wav_path = Path(wav_path)
        total = self._wav_duration(wav_path)
        if total <= 300 or not _tl.FFMPEG:
            if log:
                log('音频不长于 5 分钟，直接整段转写')
            return self.recognize(wav_path, language)

        # ① 静音点探测（-35dB 以下持续 0.4s 视为可切点）
        r = _sp.run([_tl.FFMPEG, '-hide_banner', '-i', str(wav_path),
                     '-af', 'silencedetect=noise=-35dB:d=0.4', '-f', 'null', '-'],
                    capture_output=True, text=True, errors='replace',
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        cuts = []
        for m in re.finditer(r'silence_start: ([\d.]+)', r.stderr or ''):
            cuts.append(float(m.group(1)))
        if log:
            log(f'检测到 {len(cuts)} 个静音切点')

        # ② 组片：目标 240s 一片，取最近的静音点（含 1.5s 重叠——盖住句首，
        # 让跨边界句在下一片有上下文、能识别成完整句；0.3s 太薄是老版半句根源之一）
        TARGET, OVL = 240.0, 1.5
        bounds, pos = [], 0.0
        ci = 0
        while total - pos > TARGET * 1.5:
            want = pos + TARGET
            best = want                       # 找不到静音点就按目标硬切
            for c in cuts:
                if pos + TARGET * 0.5 < c <= want + TARGET * 0.5:
                    if abs(c - want) < abs(best - want):
                        best = c
            bounds.append((max(0.0, pos - (OVL if pos > 0 else 0)), min(total, best + OVL)))
            pos = best
        bounds.append((max(0.0, pos - OVL), total))
        if log:
            log(f'切成 {len(bounds)} 片并行转写（{workers} 路并发）')

        # ③ 并发识别：任何单片的失败都不许拖垮整体（旧版用 ex.map，一片抛错就整轮
        #    抛异常 = 「长音频直接失败」的另一半根因）。降级链：
        #    单片失败 → 错峰串行重试一次 → 仍失败 → 该片对半重切再试（两轮，下界 90s）
        #    → 仍有片缺失但其余成功 → 用成功的片合并（日志记缺口）
        #    → 全部失败 → 整段回退 paraformer（逐词时间戳、单次 5 小时）
        # 切片放系统临时目录：绝不落语料目录（避免被扫成重复语料）
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix='ted_parts_'))

        def cut(i, s0, s1):
            part = tmpdir / f'p{i:03d}_{int(max(0.0, s1 - s0)):04d}.wav'
            if not part.exists():
                c = _sp.run([_tl.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                             '-ss', f'{s0:.3f}', '-to', f'{s1:.3f}', '-i', str(wav_path),
                             '-ac', '1', '-ar', '16000', str(part)],
                            capture_output=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if c.returncode != 0 or not part.exists():
                    raise RuntimeError(f'切片 {i} 失败')
            return part

        def do(i, s0=None, s1=None):
            a, b = bounds[i]
            s0 = a if s0 is None else s0
            s1 = b if s1 is None else s1
            part = cut(i, s0, s1)
            try:
                return i, self.recognize(part, language), (s0, s1)
            except Exception as e:
                if log:
                    log(f'片段 {i} 失败（{str(e)[:60]}），稍后串行重试')
                time.sleep(2.0 * (i + 1))          # 错峰，避开限流窗口
                return i, self.recognize(part, language), (s0, s1)

        results = [None] * len(bounds)
        failed = {}
        try:
            with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
                futs = {ex.submit(do, i): i for i in range(len(bounds))}
                for fu in as_completed(futs):
                    i = futs[fu]
                    try:
                        _k, res, seg = fu.result()
                        results[i] = res
                        bounds[i] = seg             # 半长重切后以实际切点为准
                    except Exception as e:
                        failed[i] = str(e)[:80]
                        if log:
                            log(f'片段 {i} 重试后仍失败（{str(e)[:60]}），转半长重切')

            # 降级：失败片对半重切再试（最多两轮；单片已不足 90s 不再细分）
            for _round in range(2):
                if not failed:
                    break
                retry, failed = failed, {}
                for i, why in retry.items():
                    a, b = bounds[i]
                    if b - a < 90.0:
                        failed[i] = why
                        if log:
                            log(f'片段 {i} 仅 {b - a:.0f}s，不再细分：{why}')
                        continue
                    mid = (a + b) / 2.0
                    for x, y in ((a, min(b, mid + 0.75)), (max(a, mid - 0.75), b)):
                        idx = len(bounds)
                        bounds.append((x, y))
                        results.append(None)
                        try:
                            _k, res, seg = do(idx, x, y)
                            results[idx] = res
                            bounds[idx] = seg
                        except Exception as e:
                            failed[idx] = str(e)[:80]
                            if log:
                                log(f'半片子片 {idx} 失败（{str(e)[:50]}）')
        finally:
            import shutil as _sh
            _sh.rmtree(tmpdir, ignore_errors=True)

        # ④ 合并（v2，治交错重复的根）：绝对时间排序 + 邻域内按词集相似度判重，
        # 保更完整的一版。旧版按 (start,text) 精确键去重——两片把同一句识别成
        # 不同断句（半句 vs 全句）时键不同 → 双双保留 → A-A-B-A-C 交错（67集事故）。
        merged = _merge_part_sentences(bounds, results) if any(results) else []
        if not merged:                  # 全部失败 → 绝不整体爆炸，整段回退 paraformer
            if log:
                log('各分片均失败，回退 paraformer-realtime-v2 整段转写（带逐词时间戳）')
            merged = self._paraformer_fallback_sentences(wav_path, language, log)
        if log:
            log(f'合并完成：{len(merged)} 句')
        return {'text': ' '.join(m['text'] for m in merged), 'words': [], 'sentences': merged}

    def _paraformer_fallback_sentences(self, wav_path, language='en', log=None):
        """分片全败时的整段兜底：临时把 asrModel 换成 paraformer-realtime-v2 跑一次
        _dashscope_recognize（单次可达 5 小时、带逐词时间戳），**不改用户配置**。"""
        old = self.cfg.get('asrModel')
        try:
            self.cfg['asrModel'] = 'paraformer-realtime-v2'
            res = self._dashscope_recognize(wav_path, language)
            if log:
                log('paraformer 兜底成功：%d 句' % len(res.get('sentences') or []))
            return res.get('sentences') or []
        except Exception as e:
            raise RuntimeError('分片转写与 paraformer 兜底均失败：' + str(e)[:120])
        finally:
            if old is None:
                self.cfg.pop('asrModel', None)
            else:
                self.cfg['asrModel'] = old

    def recognize_gummy(self, wav_path, log=None):
        """Gummy 一体模式：转写+翻译一步出（DashScope gummy-realtime-v1）。

        返回 {en:{text,sentences}, zh:{sentences}|None}；翻译句缺失时 zh=None，
        调用方可回落 LLM 翻译。
        """
        from dashscope.audio.asr.translation_recognizer import (
            TranslationRecognizerRealtime, TranslationRecognizerCallback)
        en_sents = []
        err = {}

        class Cb(TranslationRecognizerCallback):
            def on_event(self, request_id, transcription_result,
                         translation_result, usage):
                try:
                    tr = transcription_result
                    if not getattr(tr, 'is_sentence_end', False):
                        return
                    for s in (getattr(tr, 'sentences', None) or []):
                        if not s.text:
                            continue
                        # 翻译：TranslationResult 没有 sentences——每句取
                        # get_translation('zh').text（SDK 源码确认的结构）
                        zh = ''
                        if translation_result is not None:
                            t = translation_result.get_translation('zh')
                            if t is not None and getattr(t, 'text', None):
                                zh = t.text.strip()
                        # 说话人：SDK 句对象没有 speaker 字段，但 _raw_data 保留原始
                        # JSON——diarization 开启时句级 speaker 在里面（有则带，无则空）
                        raw = getattr(s, '_raw_data', None) or {}
                        spk = raw.get('speaker') if isinstance(raw, dict) else None
                        txt = s.text.strip()
                        if spk:
                            txt = f'[S{spk}] ' + txt
                        en_sents.append({'text': txt,
                                         'start': (s.begin_time or 0) / 1000.0,
                                         'end': (s.end_time or 0) / 1000.0,
                                         'sid': s.sentence_id,
                                         'zh': zh})
                except Exception:
                    pass

            def on_error(self, message):
                err['msg'] = str(getattr(message, 'message', message))[:300]

        self._prepare()
        recognizer = TranslationRecognizerRealtime(
            model='gummy-realtime-v1', format='wav', sample_rate=16000,
            transcription_enabled=True, translation_enabled=True,
            source_language='en', target_languages=['zh'],
            diarization_enabled=True)
        recognizer.call(file=str(wav_path), callback=Cb())
        if err:
            raise RuntimeError('Gummy 调用失败：' + err['msg'])
        if not en_sents:
            raise RuntimeError('Gummy 没有返回转写结果')
        ok_zh = sum(1 for s in en_sents if s['zh'])
        if log:
            log(f'Gummy 完成：英文 {len(en_sents)} 句，自带中文翻译 {ok_zh} 句')
        en_clean = [{'text': s['text'], 'start': s['start'], 'end': s['end']} for s in en_sents]
        zh_clean = ([{'text': s['zh'], 'start': s['start'], 'end': s['end']}
                     for s in en_sents if s['zh']] if ok_zh else None)
        return {'en': {'text': ' '.join(s['text'] for s in en_clean), 'sentences': en_clean},
                'zh': {'sentences': zh_clean} if zh_clean else None}

    @staticmethod
    def _wav_duration(wav_path):
        import wave as _wave
        try:
            with _wave.open(str(wav_path), 'rb') as w:
                return w.getnframes() / float(w.getframerate() or 16000)
        except Exception:
            return 0.0

    @staticmethod
    def _parse(output):
        """把 DashScope 的输出整理成统一结构。"""
        sents = output.get('sentence') or output.get('sentences') or []
        if isinstance(sents, dict):
            sents = [sents]

        sentences, words = [], []
        for s in sents:
            if not isinstance(s, dict):
                continue
            txt = (s.get('text') or '').strip()
            if txt:
                sentences.append({
                    'text': txt,
                    'start': (s.get('begin_time') or 0) / 1000.0,
                    'end': (s.get('end_time') or 0) / 1000.0,
                })
            for w in (s.get('words') or []):
                if not isinstance(w, dict):
                    continue
                t = (w.get('text') or '')
                if not t.strip():
                    continue
                words.append({
                    'text': t,
                    'clean': w.get('punctuation') or '',
                    'start': (w.get('begin_time') or 0) / 1000.0,
                    'end': (w.get('end_time') or 0) / 1000.0,
                })
        full = (output.get('text') or '').strip() or ' '.join(s['text'] for s in sentences)
        return {'text': full.strip(), 'words': words, 'sentences': sentences}

    def test(self):
        try:
            self._prepare()
            from dashscope.audio.asr import Recognition  # noqa
            return {'ok': True, 'msg': 'DashScope 已就绪'}
        except Exception as e:
            return {'ok': False, 'msg': str(e)}


# ---------------------------------------------------------------- 热词表管理
def vocab_supported(model):
    """该识别模型是否支持预编译热词表。官方口径：paraformer / qwen-audio-3.x /
    fun-asr 主版本支持；qwen3-asr 兼容口未开放。"""
    m = (model or '').lower()
    return ('paraformer' in m) or ('qwen-audio' in m) or ('fun-asr' in m)


def _vocab_service(cfg):
    """构造 VocabularyService（customization 端点，model=speech-biasing）。
    base 跟随 cfg.dashscopeBaseUrl（千问AI平台/百炼通用）。"""
    key = (cfg.get('dashscopeApiKey') or '').strip()
    if not key:
        raise RuntimeError('还没填阿里云 DashScope API Key（设置 → 语音识别 里填）')
    import dashscope as _ds
    _ds.api_key = key
    host = (cfg.get('dashscopeBaseUrl') or '').strip().rstrip('/')
    if host and 'dashscope.aliyuncs.com' not in host:
        _ds.base_http_api_url = host + '/api/v1'
    from dashscope.audio.asr.vocabulary import VocabularyService
    return VocabularyService()


def vocab_sync(cfg, log=None):
    """本地词表 → 服务端热词表（免费）。已建表且模型没换 → update；
    换了识别模型/旧表失效 → 自动重建（官方坑：target_model 不一致时静默不生效）。
    返回 {'ok', vocabulary_id, count, target_model, msg}。调用方负责把
    vocabulary_id/asrVocabModel 写回配置。"""
    model = (cfg.get('asrModel') or 'paraformer-realtime-v2').strip()
    if not vocab_supported(model):
        return {'ok': False, 'msg': f'当前识别模型 {model} 不支持热词（paraformer / '
                                    'qwen-audio-3.x / fun-asr 系支持；qwen3-asr 兼容口未开放）'}
    words = []
    for w in (cfg.get('asrHotwords') or []):
        if not isinstance(w, dict):
            continue
        t = str(w.get('word') or '').strip()
        if t:
            wt = (w).get('weight')
            try:
                wt = int(wt)
            except (TypeError, ValueError):
                wt = 4
            words.append({'word': t,
                          'weight': wt,
                          'lang': 'zh' if any(ord(c) > 127 for c in t) else 'en'})
    if not words:
        return {'ok': False, 'msg': '热词表是空的（先加几个词再同步）'}
    try:
        svc = _vocab_service(cfg)
        vid = (cfg.get('asrVocabularyId') or '').strip()
        vmodel = (cfg.get('asrVocabModel') or '').strip()
        if vid and vmodel == model:
            svc.update_vocabulary(vid, words)
            if log:
                log(f'热词表已更新：{vid}（{len(words)} 词 · 绑定 {model}）')
            return {'ok': True, 'vocabulary_id': vid, 'count': len(words), 'target_model': model}
        new_vid = svc.create_vocabulary(target_model=model, prefix='tedlib',
                                        vocabulary=words)
        if vid and vid != new_vid:            # 换模型重建：旧表尽力清理
            try:
                svc.delete_vocabulary(vid)
            except Exception:
                pass
        if log:
            log(f'热词表已创建：{new_vid}（{len(words)} 词 · 绑定 {model}）')
        return {'ok': True, 'vocabulary_id': new_vid, 'count': len(words), 'target_model': model}
    except Exception as e:
        return {'ok': False, 'msg': f'热词表同步失败：{str(e)[:150]}'}


def vocab_delete(cfg, log=None):
    """删服务端热词表（本地词表保留，可再同步重建）。"""
    vid = (cfg.get('asrVocabularyId') or '').strip()
    if not vid:
        return {'ok': True, 'msg': '本来就没有云端热词表'}
    try:
        _vocab_service(cfg).delete_vocabulary(vid)
        if log:
            log(f'云端热词表已删除：{vid}')
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'msg': f'删除失败：{str(e)[:150]}'}


def vocab_panel(cfg):
    """热词面板数据：本地词表 + 云端状态（vocabulary_id / 绑定模型 / 是否失配）。"""
    model = (cfg.get('asrModel') or 'paraformer-realtime-v2').strip()
    vid = (cfg.get('asrVocabularyId') or '').strip()
    vmodel = (cfg.get('asrVocabModel') or '').strip()
    return {'ok': True,
            'words': cfg.get('asrHotwords') or [],
            'on': bool(cfg.get('asrHotwordOn')),
            'vocabularyId': vid,
            'vocabModel': vmodel,
            'model': model,
            'supported': vocab_supported(model),
            'mismatch': bool(vid and vmodel and vmodel != model)}


# ---------------------------------------------------------------- 文本比对
def normalize_word(w):
    """把词规整成可比较的形式：小写、去掉首尾标点。"""
    w = (w or '').lower().strip()
    w = re.sub(r"^[^a-z0-9']+", '', w)
    w = re.sub(r"[^a-z0-9']+$", '', w)
    return w


def tokenize(text):
    """把文本切成词（保留原始形式）。"""
    return [t for t in re.split(r"\s+", (text or '').strip()) if t]


def compare(reference, recognized):
    """逐词比对「应为」与「实说」，返回对齐结果。

    用最简单的双指针 + 前瞻匹配（对跟读场景足够，且可解释）：
      命中        -> ok
      实说词能在后面几步找到 -> 中间的原词标记为 missed（漏读）
      参考词能在实说后面找到 -> 多出来的标记为 extra（多读）
      都不匹配    -> wrong（读错）
    """
    ref = tokenize(reference)
    got = tokenize(recognized)
    rn = [normalize_word(x) for x in ref]
    gn = [normalize_word(x) for x in got]

    items = []
    i = j = 0
    LOOKAHEAD = 4
    while i < len(ref) or j < len(got):
        if i < len(ref) and j < len(got):
            if rn[i] == gn[j]:
                items.append({'type': 'ok', 'ref': ref[i], 'got': got[j],
                              'refIdx': i, 'gotIdx': j})
                i += 1
                j += 1
                continue
            # 参考词在后面几步能被匹配上 -> 当前实说词是多读的
            k = None
            for t in range(j + 1, min(j + 1 + LOOKAHEAD, len(got))):
                if gn[t] == rn[i]:
                    k = t
                    break
            if k is not None:
                for t in range(j, k):
                    items.append({'type': 'extra', 'ref': None, 'got': got[t],
                                  'refIdx': i, 'gotIdx': t})
                j = k
                continue
            # 实说词在后面几步能找到 -> 当前参考词是漏读的
            m = None
            for t in range(i + 1, min(i + 1 + LOOKAHEAD, len(ref))):
                if rn[t] == gn[j]:
                    m = t
                    break
            if m is not None:
                for t in range(i, m):
                    items.append({'type': 'missed', 'ref': ref[t], 'got': None,
                                  'refIdx': t, 'gotIdx': j})
                i = m
                continue
            # 都不匹配 -> 读错
            items.append({'type': 'wrong', 'ref': ref[i], 'got': got[j],
                          'refIdx': i, 'gotIdx': j})
            i += 1
            j += 1
        elif i < len(ref):
            items.append({'type': 'missed', 'ref': ref[i], 'got': None, 'refIdx': i, 'gotIdx': None})
            i += 1
        else:
            items.append({'type': 'extra', 'ref': None, 'got': got[j], 'refIdx': None, 'gotIdx': j})
            j += 1

    total = max(1, len(ref))
    ok = sum(1 for x in items if x['type'] == 'ok')
    return {
        'items': items,
        'refCount': len(ref),
        'gotCount': len(got),
        'ok': ok,
        'wrong': sum(1 for x in items if x['type'] == 'wrong'),
        'missed': sum(1 for x in items if x['type'] == 'missed'),
        'extra': sum(1 for x in items if x['type'] == 'extra'),
        'accuracy': round(ok / total * 100, 1),
    }
