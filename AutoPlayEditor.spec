# -*- mode: python ; coding: utf-8 -*-
"""
「简谱编辑器」的打包配置，产出 AutoPlayEditor.exe。

和主程序（AutoPlay.spec）/ 跟奏版（AutoPlayFollow.spec）的区别：

* 入口是 editor.py，只干「读 midi → 手动改 → 导出 midi」这一件事；
* 不往游戏里发按键，所以**没有** uac_admin，双击不会弹 UAC，也不需要管理员；
* 也就不需要 keyboard / pydirectinput，更用不上 basic-pitch 那一大套，
  只要 Qt + numpy + mido，包体积比主程序小得多；
* 产物放哪儿由 build_editor.bat 决定：默认进 EditorPlay\AutoPlayEditor\。

别的（去黑框、裁掉用不到的 Qt 组件）跟 AutoPlay.spec 一个路子。
"""

# mido 是动态导入的（写在 write_midi 里面），静态分析看不见，得显式列出来。
# 这里故意用白名单而不是 collect_all('mido')：后者会把 mido.backends.pygame
# 一起拉进来，于是 pygame（7 MB）、yaml 和一堆用不上的端口后端全进包。
# 我们只读写 .mid 文件，midifiles / messages / parser 这一串就够了。
MIDO_MODULES = (
    'mido',
    'mido.backends', 'mido.backends.backend',
    'mido.messages', 'mido.messages.checks', 'mido.messages.decode',
    'mido.messages.encode', 'mido.messages.messages', 'mido.messages.specs',
    'mido.messages.strings',
    'mido.midifiles', 'mido.midifiles.meta', 'mido.midifiles.midifiles',
    'mido.midifiles.tracks', 'mido.midifiles.units',
    'mido.parser', 'mido.tokenizer', 'mido.frozen', 'mido.syx', 'mido.version',
)

datas, binaries, hiddenimports = [], [], list(MIDO_MODULES)

# 名字里带这些的一律不要（Qt 只用到 Core / Gui / Widgets）
UNUSED = (
    'Qt6Quick', 'Qt6Qml', 'Qt6QmlModels', 'Qt6QmlWorkerScript', 'Qt6QmlMeta',
    'Qt6Pdf', 'Qt6Network', 'Qt6OpenGL', 'Qt6VirtualKeyboard', 'Qt6Sql',
    'Qt6Svg', 'Qt6Xml', 'Qt6Test', 'Qt6Designer', 'Qt6PrintSupport',
    'Qt6Concurrent', 'Qt6DBus', 'Qt6Multimedia', 'Qt6WebEngine',
    'opengl32sw',
    'translations/',
)


def keep(entry):
    name = entry[0].replace('\\', '/')
    return not any(part in name for part in UNUSED)


# 编辑器用不到的重家伙：基本全是主程序里「音频转 MIDI」的依赖
EXCLUDES = ('matplotlib', 'pandas', 'PIL', 'tkinter', 'IPython', 'pytest', 'sympy',
            'notebook', 'nbformat', 'tornado',
            'librosa', 'basic_pitch', 'onnxruntime', 'numba', 'llvmlite',
            'soundfile', 'pretty_midi', 'mir_eval', 'resampy', 'scipy',
            # mido 的端口后端才用得到这些，我们只读写文件
            'pygame', 'yaml', 'rtmidi', 'portmidi')


a = Analysis(
    ['editor.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(EXCLUDES),
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
    name='AutoPlayEditor',
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AutoPlayEditor',
)