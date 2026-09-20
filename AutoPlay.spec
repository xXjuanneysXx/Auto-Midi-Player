# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置。

- 窗口模式（console=False）：运行时不出现黑框
- uac_admin：游戏要是以管理员身份运行，我们发过去的按键才不会被拦掉
- onedir：启动快，改完代码重新打包也快
- 顺手删掉用不到的 Qt 组件（Qt 只用到 Core / Gui / Widgets），
  否则 PyInstaller 会把 Quick / Qml / Pdf / Network / 语言包全塞进来

同一个 spec 打两个版本：环境变量 AUTOPLAY_LITE=1 时是**精简版** ——
不带「音频转 MIDI」那一整套（basic-pitch / onnxruntime / librosa / numba /
llvmlite / scipy…，差不多 290 MB），也不带简谱编辑器（editor.py）。
跟奏、简谱生成、演奏、试听、内置曲库都留着。
"""

import glob
import os

from PyInstaller.utils.hooks import collect_all

# 精简版：不带音频转 MIDI
LITE = bool(os.environ.get('AUTOPLAY_LITE'))


datas, binaries, hiddenimports = [], [], []
for package in ('keyboard', 'pydirectinput'):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# 「音频转 MIDI」用的那套后端（basic-pitch -> onnxruntime / librosa / numba / scipy…）。
# audio2midi.py 里这些是 importlib 动态导入的，PyInstaller 的静态分析看不见，
# 不显式收集的话，打包出来的 exe 会悄悄退回内置的 YIN —— 转换质量差一大截。
# 代价是体积会大很多（llvmlite 那个 DLL 就 120 MB），精简版整个不要。
BP_PACKAGES = (
    'basic_pitch', 'onnxruntime', 'librosa', 'pretty_midi', 'mir_eval', 'resampy',
    'numba', 'llvmlite', 'soxr', 'soundfile', 'pooch',
)

# 精简版要排掉的东西：转谱那一整套（含本地 mp3midi 包）+ 简谱编辑器
AUDIO_MODULES = ('basic_pitch', 'onnxruntime', 'librosa', 'pretty_midi', 'mir_eval',
                 'resampy', 'numba', 'llvmlite', 'soxr', 'soundfile', 'pooch',
                 'scipy', 'sklearn', 'joblib', 'threadpoolctl', 'mp3midi', 'editor')

if not LITE:
    for package in BP_PACKAGES:
        package_datas, package_binaries, package_imports = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_imports

    # scipy._cyutility 是 C 层隐式引用的私有扩展（scipy.sparse 的 Cython 代码里
    # import 它），PyInstaller 的静态分析看不见。漏了它，冻结后只要一 import
    # scipy.stats 就会 ModuleNotFoundError: No module named 'scipy._cyutility'，
    # 而 basic-pitch 正好要经 mir_eval -> scipy.stats 走进来 —— v1.3 头一版就是
    # 栽在这儿：界面说「basic-pitch 可以用」，真转的时候才失败。
    import scipy as _scipy

    for _path in glob.glob(os.path.join(os.path.dirname(_scipy.__file__),
                                        '_cyutility*.pyd')):
        binaries.append((_path, 'scipy'))

# 名字里带这些的一律不要
UNUSED = (
    'Qt6Quick', 'Qt6Qml', 'Qt6QmlModels', 'Qt6QmlWorkerScript', 'Qt6QmlMeta',
    'Qt6Pdf', 'Qt6Network', 'Qt6OpenGL', 'Qt6VirtualKeyboard', 'Qt6Sql',
    'Qt6Svg', 'Qt6Xml', 'Qt6Test', 'Qt6Designer', 'Qt6PrintSupport',
    'Qt6Concurrent', 'Qt6DBus', 'Qt6Multimedia', 'Qt6WebEngine',
    'opengl32sw',            # 纯控件程序用不到软件 OpenGL，想更保险就删掉这行
    'translations/',         # Qt 自带语言包，界面文案都是我们自己的
)


def keep(entry):
    name = entry[0].replace('\\', '/')
    return not any(part in name for part in UNUSED)


# librosa 自带的画图 / 示例数据那些东西会顺手把 matplotlib、pandas 拖进来（一共四十多 MB），
# 我们只用它算基频和读音频，一个都用不到 —— 直接排除掉。
EXCLUDES = ['matplotlib', 'pandas', 'PIL', 'tkinter', 'IPython', 'pytest', 'sympy',
            'notebook', 'nbformat', 'tornado']
if LITE:
    EXCLUDES += list(AUDIO_MODULES)


# 内置曲库：装到 _internal\songs\。安装程序会在安装目录下再放一份 songs\，
# 那份会优先被找到（见 main.py 的 app_subdir），这份是「只拷 dist 目录也能用」的兜底。
datas += [('songs', 'songs')]

# 图标：make_installer.py 生成，没有就先不打（不影响功能）
ICON = 'AutoPlay.ico' if os.path.exists('AutoPlay.ico') else None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
a.binaries = [entry for entry in a.binaries if keep(entry)]
a.datas = [entry for entry in a.datas if keep(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AutoPlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AutoPlay',
)
