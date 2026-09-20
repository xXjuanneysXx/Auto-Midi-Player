# -*- mode: python ; coding: utf-8 -*-
"""
「含跟奏版本」的打包配置，和 AutoPlay.spec 唯一的区别：

* COLLECT / EXE 的名字叫 AutoPlayFollow —— main.py 靠 exe 名字里有没有 follow
  来决定要不要把跟奏窗口露出来，所以 AutoPlay.exe 依然是那个干净的老版本；
* 产物放哪儿由命令行决定，见 build_follow.bat：默认进 FollowPlay\\AutoPlayFollow\\。

别的（去黑框、要管理员权限、裁掉用不到的 Qt 组件）都跟 AutoPlay.spec 一样。
"""

from PyInstaller.utils.hooks import collect_all


datas, binaries, hiddenimports = [], [], []
for package in ('keyboard', 'pydirectinput'):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# 同 AutoPlay.spec：把「音频转 MIDI」用的后端一起收进来（否则打包后会退回 YIN）
BP_PACKAGES = (
    'basic_pitch', 'onnxruntime', 'librosa', 'pretty_midi', 'mir_eval', 'resampy',
    'numba', 'llvmlite', 'soxr', 'soundfile', 'pooch',
)
for package in BP_PACKAGES:
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

# 同 AutoPlay.spec：scipy 的私有扩展 _cyutility 静态分析看不见，必须手工带上，
# 否则冻结后 basic-pitch 一 import 就 ModuleNotFoundError。
import glob
import os

import scipy as _scipy

for _path in glob.glob(os.path.join(os.path.dirname(_scipy.__file__), '_cyutility*.pyd')):
    binaries.append((_path, 'scipy'))

# 名字里带这些的一律不要
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


# 同 AutoPlay.spec：librosa 会顺手拖进 matplotlib / pandas，我们一个都用不到
EXCLUDES = ('matplotlib', 'pandas', 'PIL', 'tkinter', 'IPython', 'pytest', 'sympy',
            'notebook', 'nbformat', 'tornado')


a = Analysis(
    ['main.py'],
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
    name='AutoPlayFollow',
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AutoPlayFollow',
)
