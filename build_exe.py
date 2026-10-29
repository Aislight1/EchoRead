# -*- coding: utf-8 -*-
r"""
把 听与读打包成独立 .exe

用法（必须用系统 Python，不许 uv ephemeral）:
  py build_exe.py
  —— PyInstaller 只收构建环境里装了的模块：系统 py 的 sounddevice/edge_tts/
     dashscope/requests 等运行期依赖天然齐全。第四十五批曾改用 uv run
     --with 手拼清单，漏了 sounddevice 导致打包版录音全挂（"缺少
     sounddevice" 报障）；torch 已用 --exclude-module 挡在包外。

产物:
  dist\听与读\听与读.exe
"""
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parent
NAME = '听与读'

# PyInstaller 会清空 dist\<NAME>，这些用户数据要先搬走、打完再放回
DATA_FILES = ['config.json', 'vocab.json', 'practice.json', 'voices.json',
              'define_cache.json', 'plan.json', 'usage_log.jsonl',
              'sentence_favorites.json', 'phoneme_progress.json', 'item_stats.json', 'error.log',
              'difficulty.json', 'parse_overrides.json', 'calibration.json', 'item_summary.json',
              'listen_quiz_cache.json']
# 暂存目录放在 build/ 之外：build/ 会在每次打包开头被整目录清空
STASH = APP / '.build_stash'


def stash_user_data(out: Path) -> Path:
    stash = STASH
    # 防污染第一层：轮换备份上一轮快照（本轮 stash 被污染时上一轮还能救）
    prev = APP / '.build_stash_prev'
    if stash.exists():
        shutil.rmtree(prev, ignore_errors=True)
        try:
            stash.rename(prev)
        except OSError:
            shutil.copytree(stash, prev, dirs_exist_ok=True)
    stash.mkdir(parents=True, exist_ok=True)

    # 防污染第二层：config 哨兵——当前 exe 旁 config 一个 API key 都没有
    # （多半是数据被误删后应用自动重建的空壳），而上一轮快照里有 key，
    # 绝不能用空壳覆盖快照（2026-10-13 事故根因之一）。
    def _cfg_keys(p):
        try:
            c = json.loads(p.read_text(encoding='utf-8'))
            return {k for k in ('llmApiKey', 'dashscopeApiKey', 'mimoApiKey', 'openaiApiKey',
                                'volcApiKey', 'siliconflowApiKey', 'customSpeechApiKey')
                    if (c.get(k) or '').strip()}
        except Exception:
            return set()

    src_cfg = out / 'config.json'
    prev_cfg = prev / 'config.json'
    skip_cfg = False
    if src_cfg.exists() and prev_cfg.exists():
        if not _cfg_keys(src_cfg) and _cfg_keys(prev_cfg):
            print('  [!] 检出无任何 API key 的空壳 config（疑似误删后自动重建），跳过暂存、沿用上一轮快照')
            shutil.copy2(prev_cfg, stash / 'config.json')
            skip_cfg = True

    for name in DATA_FILES:
        src = out / name
        if name == 'config.json' and skip_cfg:
            continue
        # 只进不出：out 缺失（上一轮构建事故清掉）而上一轮快照有 → 沿用快照，防连环穿透
        if not src.exists():
            if (prev / name).exists():
                shutil.copy2(prev / name, stash / name)
                print(f'  [!] {name} 输出目录已缺失，沿用上一轮快照')
            continue
        shutil.copy2(src, stash / name)
        print(f'  已暂存 {name}')
    cache = out / 'cache'
    if cache.exists():
        shutil.copytree(cache, stash / 'cache', dirs_exist_ok=True)
        print('  已暂存 cache/')

    # 防误删第三层：语料库目录若指向输出目录内部（如 dist\<NAME>\downloads），
    # 整库一并暂存（2026-10-13 事故：库目录被构建清空、音频全丢）
    try:
        c = json.loads((out / 'config.json').read_text(encoding='utf-8')) if (out / 'config.json').exists() else {}
        lib_dir = (c.get('libraryDir') or '').strip()
        if lib_dir:
            lp = Path(lib_dir)
            try:
                if lp.resolve().is_relative_to(out.resolve()):
                    shutil.copytree(lp, stash / '__library__', dirs_exist_ok=True)
                    print(f'  已暂存语料库目录（在输出目录内）：{lp.name}/')
            except (OSError, ValueError):
                pass
    except Exception:
        pass
    return stash


def restore_user_data(out: Path, stash: Path):
    for name in DATA_FILES:
        src = stash / name
        if src.exists():
            shutil.copy2(src, out / name)
            print(f'  已恢复 {name}')
    if (stash / 'cache').exists():
        shutil.copytree(stash / 'cache', out / 'cache', dirs_exist_ok=True)
        print('  已恢复 cache/')
    cfg_now = out / 'config.json'
    cfg_prev = APP / '.build_stash_prev' / 'config.json'
    if cfg_now.exists() and cfg_prev.exists():
        def _kk(pp):
            try:
                c = json.loads(pp.read_text(encoding='utf-8'))
                return any((c.get(k) or '').strip() for k in
                           ('llmApiKey', 'dashscopeApiKey', 'mimoApiKey', 'openaiApiKey'))
            except Exception:
                return False
        if not _kk(cfg_now) and _kk(cfg_prev):
            print('  ⚠[!] 恢复后 config 是空壳！已用上一轮快照兜底')
            shutil.copy2(cfg_prev, cfg_now)
    libbak = stash / '__library__'
    if libbak.exists():
        # 库目录回原位（位置取 stash 时的 config 记录；恢复顺序在 config 之后）
        try:
            c = json.loads((out / 'config.json').read_text(encoding='utf-8'))
            lp = Path(c.get('libraryDir') or '')
            if str(lp) and lp.is_relative_to(out):
                shutil.copytree(libbak, lp, dirs_exist_ok=True)
                print(f'  已恢复语料库目录：{lp.name}/')
        except Exception as e:
            print(f'  语料库目录恢复失败：{e}')


def main():
    # 自用版开关：py build_exe.py --with-muse 时并入 Muse 连接件（存档 _legacy_muse/；
    # 源码净化后 Muse 只以独立 exe 形态保留给自用版，开源构建不带）
    with_muse = '--with-muse' in sys.argv
    # --outdir dist/<名字>：构建到独立目录（开源纯净版用；默认 dist/<NAME>）
    _outdir = None
    if '--outdir' in sys.argv:
        _outdir = sys.argv[sys.argv.index('--outdir') + 1] if sys.argv.index('--outdir') + 1 < len(sys.argv) else None
    build = APP / 'build'
    if build.exists():
        shutil.rmtree(build, ignore_errors=True)

    out = APP / 'dist' / NAME
    if _outdir:
        out = APP / _outdir
    stash = None
    if out.exists():
        print('保留 dist 里的用户数据…')
        stash = stash_user_data(out)

    # 锁目录对策：PyInstaller 默认要整目录重建 dist\<NAME>；偶尔被残留句柄
    # （孤儿 shell / 安全软件 / 系统扫描）占住删不掉。改成：构建到临时目录
    # build_dist，再**文件级同步**进去——不动 dist\<NAME> 目录本身，天然免疫目录句柄。
    def sync_into(src, dst):
        dst.mkdir(parents=True, exist_ok=True)
        for p in dst.iterdir():
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
                if p.exists():
                    p.rename(dst / (p.name + f'_locked_{int(__import__("time").time())}'))
            else:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
        for p in src.iterdir():
            if p.is_dir():
                shutil.copytree(p, dst / p.name)
            else:
                shutil.copy2(p, dst / p.name)

    args = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean',
        '--icon', str(APP / 'app.ico'),
        '--name', NAME,
        '--windowed',
        '--onedir',
        '--distpath', str(APP / 'build_dist'),
        '--add-data', f'{APP / "ui"};ui',
        '--add-binary', f'{APP / "yt-dlp.exe"};.',
        '--hidden-import', 'webview.platforms.edgechromium',
        '--hidden-import', 'webview.platforms.winforms',
        '--hidden-import', 'clr_loader',
        '--hidden-import', 'pythonnet',
        '--hidden-import', 'json_repair',
        '--collect-all', 'webview',
        '--exclude-module', 'tkinter',
        '--exclude-module', 'matplotlib',
        '--exclude-module', 'numpy',
        # 本地强制对齐（MMS_FA）延迟加载可选组件：开发机装了 torch 也不能进包（+500MB）；
        # dist 里 local_align.available()=False 自动回落 paraformer 管线
        '--exclude-module', 'torch',
        '--exclude-module', 'torchaudio',
        '--exclude-module', 'soundfile',
        '--exclude-module', 'sympy',
        '--exclude-module', 'networkx',
        '--exclude-module', 'pandas',
        '--exclude-module', 'PIL',
        '--exclude-module', 'PyQt5',
        '--exclude-module', 'PySide6',
        '--exclude-module', 'PyInstaller',
        str(APP / 'app.py'),
    ]
    print('运行 PyInstaller…')
    r = subprocess.run(args, cwd=str(APP))
    build_src = APP / 'build_dist' / NAME
    # 顺序铁律：先同步新程序（清空 out 内容），再恢复用户数据——
    # 反过来的话 sync 会把 restore 放回去的数据清掉（2026-10-13 事故根因之二）
    if r.returncode == 0 and (build_src / f'{NAME}.exe').exists():
        print('文件级同步进 dist（目录句柄锁免疫）…')
        try:
            sync_into(build_src, out)
        except Exception as e:
            print(f'  [!] 同步中断（{e}）——数据恢复优先执行')
        shutil.rmtree(APP / 'build_dist', ignore_errors=True)
    if stash is not None:
        print('恢复用户数据…')
        restore_user_data(out, stash)
    if r.returncode != 0:
        print('打包失败')
        return r.returncode

    exe = out / f'{NAME}.exe'
    if not exe.exists():
        print('打包失败：未生成 exe')
        return 1

    # ① 硬校验：dist 里的 ui/index.html 必须与源码逐字节相同。
    # 2026-10-18 复盘：用户报「LLM 纠音没有、并上句还是错的」，实际跑的是 10/15 的
    # dist，源码 10/17 早修好了——打包时 ui 没同步/没重打，界面上无法自证版本。
    # 这里直接比对字节，不一致就让构建失败（返回 3），不给"以为改好了"留余地。
    try:
        src_ui = (APP / 'ui' / 'index.html').read_bytes()
        for cand in (out / '_internal' / 'ui' / 'index.html', out / 'ui' / 'index.html'):
            if cand.exists():
                if cand.read_bytes() == src_ui:
                    print(f'  [OK] ui/index.html 与源码一致（{len(src_ui)} 字节）')
                else:
                    print('  [X] dist 里的 ui/index.html 与源码不一致 → 构建作废')
                    print(f'    源码: {APP / "ui" / "index.html"}')
                    print(f'    包内: {cand}')
                    return 3
                break
    except Exception as e:
        print(f'  [!] ui 校验跳过（{e}）')

    # ② 构建指纹：把时间 + 源码内容哈希写进 dist，界面「关于/设置」直接显示。
    # 以后任何「我明明改了却没生效」的疑问，先看这行字即可定位是不是跑了旧包。
    try:
        h = hashlib.md5()
        for rel in ('app.py', 'ui/index.html', 'videosrc.py', 'speech_asr.py', 'speech_tts.py'):
            p = APP / rel
            if p.exists():
                h.update(rel.encode('utf-8'))
                h.update(p.read_bytes())
        stamp = f'{datetime.now().strftime("%Y-%m-%d %H:%M")} · {h.hexdigest()[:8]}'
        # source 只写目录名，不写绝对路径——release/dist 会被打进安装包，
        # 绝对路径会带出本机用户名/项目目录（发布隐私红线）
        info = {'builtAt': stamp, 'stamp': stamp, 'source': APP.name, 'exe': f'{NAME}.exe'}
        blob = json.dumps(info, ensure_ascii=False, indent=1)
        (out / 'build_info.json').write_text(blob, encoding='utf-8')
        (out / '_internal').mkdir(parents=True, exist_ok=True)
        (out / '_internal' / 'build_info.json').write_text(blob, encoding='utf-8')
        print(f'  [OK] 构建指纹：{stamp}')
    except Exception as e:
        print(f'  [!] 构建指纹写入失败（{e}）')

    print()

    if with_muse:
        legacy = APP / '_legacy_muse'
        if legacy.exists():
            for item in ['TED语料库MCP.exe', '_internal_mcp', 'TED语料库Muse连接.exe', '_internal_muse']:
                src = legacy / item
                if not src.exists():
                    continue
                if src.is_dir():
                    shutil.copytree(src, out / item, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, out / item)
            print('Muse 连接件已并入（自用版）: TED语料库Muse连接.exe')
    print('打包完成')
    print('  目录:', out)
    print('  exe :', exe)
    print(f'  大小: {exe.stat().st_size / 1024 / 1024:.1f} MB（exe 本体）')
    total = sum(f.stat().st_size for f in out.rglob('*') if f.is_file())
    print(f'  整包: {total / 1024 / 1024:.1f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
