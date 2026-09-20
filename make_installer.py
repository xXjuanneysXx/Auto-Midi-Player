# -*- coding: utf-8 -*-
"""
把「程序 + 曲库 + 安装程序」打成一个自解压的单文件安装包。

    python make_installer.py            # 全流程（两个版本一起打）
    python make_installer.py icon       # 只生成 AutoPlay.ico
    python make_installer.py app        # 只打两个版本的主程序
    python make_installer.py payload    # 只压 payload（需要主程序已经打好）

全流程做四件事：
    1. 画一个 AutoPlay.ico（主程序和快捷方式都用它）；
    2. PyInstaller 打主程序，两个版本各打一份：
         完全版 -> dist\\AutoPlay\\      精简版 -> dist_lite\\AutoPlay\\
       精简版不给「音频转 MIDI」那一整套（basic-pitch / onnxruntime / librosa /
       numba / llvmlite / scipy…，差不多 290 MB），也不给简谱编辑器 ——
       跟奏、简谱生成、演奏、试听、内置曲库都留着。
       打精简版时会临时把 edition.LITE 改成 True，打完立刻改回来。
    3. PyInstaller 打安装程序本体（--onefile）-> dist_installer\\；
    4. 每个版本各压一个 payload.zip，追加到安装程序 exe 末尾，再补 16 字节尾巴
       （魔数 + 偏移），产物放 发布\\，两个包大小明显不一样。
"""

import io
import os
import re
import struct
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SONGS_DIR = os.path.join(HERE, 'songs')

# 两个版本：完全版 / 精简版。名字、dist 目录、工作目录都不一样，避免互相踩。
EDITIONS = (
    {'key': 'full', 'label': '完全版', 'lite': False,
     'dist': os.path.join(HERE, 'dist'), 'work': None},
    {'key': 'lite', 'label': '精简版', 'lite': True,
     'dist': os.path.join(HERE, 'dist_lite'), 'work': os.path.join(HERE, 'build_lite')},
)
APP_DIR = os.path.join(EDITIONS[0]['dist'], 'AutoPlay')
ICON = os.path.join(HERE, 'AutoPlay.ico')
INSTALLER_DIST = os.path.join(HERE, 'dist_installer')
INSTALLER_EXE = os.path.join(INSTALLER_DIST, 'AutoPlay-安装程序.exe')
RELEASE_DIR = os.path.join(HERE, '发布')
PAYLOAD_MAGIC = b'APAYLOAD1'
TRAILER_SIZE = len(PAYLOAD_MAGIC) + 8


def log(text):
    print(text, flush=True)


def run(args, env=None):
    log('$ %s' % ' '.join(args))
    result = subprocess.run(args, cwd=HERE, env=env)
    if result.returncode != 0:
        raise SystemExit('[x] 这条命令失败了：%s' % ' '.join(args))


# ---------- 1. 图标 ----------

def render_note(size):
    """图标本体在 noteicon.py 里，主程序 / 安装程序 / 快捷方式共用同一个。"""
    import noteicon
    return noteicon.note_pixmap(size)


def png_bytes(pixmap):
    from PySide6.QtCore import QBuffer, QIODevice
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, 'PNG')
    return bytes(buffer.data())


def write_ico(path, images):
    """
    写一个多尺寸 .ico。

    ICO 里每一张图允许直接放 PNG（Vista 以后都认），所以不用手写 BMP + 掩码那一套。
    """
    header = struct.pack('<HHH', 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = [], []
    for size, data in images:
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32,
                                   len(data), offset))
        blobs.append(data)
        offset += len(data)
    with open(path, 'wb') as handle:
        handle.write(header + b''.join(entries) + b''.join(blobs))


def build_icon():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    app = QApplication([sys.argv[0]])       # noqa: F841  QPixmap 需要先有 QApplication
    sizes = (16, 24, 32, 48, 64, 128, 256)
    write_ico(ICON, [(size, png_bytes(render_note(size))) for size in sizes])
    log('图标好了：%s（%d 个尺寸）' % (ICON, len(sizes)))
    return ICON


# ---------- 2/3. 两次 PyInstaller ----------

class lite_edition(object):
    """
    临时把 edition.LITE 改成 True（打包精简版用），出了这个块立刻改回来。

    源码里永远写着 False，这样只用维护一份代码；万一中途出错也不会把源码留在
    精简版状态上 —— 出了任何事都会在 finally 里写回原文。
    """

    def __init__(self):
        self.path = os.path.join(HERE, 'edition.py')

    def __enter__(self):
        self.original = io.open(self.path, encoding='utf-8').read()
        changed = re.sub(r'^LITE = False', 'LITE = True', self.original,
                         count=1, flags=re.M)
        if changed == self.original:
            raise SystemExit('[x] edition.py 里没找到 LITE = False')
        io.open(self.path, 'w', encoding='utf-8', newline='').write(changed)
        return self

    def __exit__(self, *exc):
        io.open(self.path, 'w', encoding='utf-8', newline='').write(self.original)
        return False


def build_app(edition):
    """打一个版本的主程序。精简版顺手把 edition.LITE 改掉再改回来。"""
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
            '--distpath', edition['dist'], 'AutoPlay.spec']
    if edition['work']:
        args += ['--workpath', edition['work']]
    env = dict(os.environ)
    if edition['lite']:
        env['AUTOPLAY_LITE'] = '1'
        with lite_edition():
            run(args, env=env)
    else:
        run(args, env=env)
    app_dir = os.path.join(edition['dist'], 'AutoPlay')
    if not os.path.isfile(os.path.join(app_dir, 'AutoPlay.exe')):
        raise SystemExit('[x] 没看到 %s，%s 主程序没打出来' % (app_dir, edition['label']))
    size = directory_size(app_dir) / 1048576.0
    log('%s 主程序好了：%s（%.1f MB）' % (edition['label'], app_dir, size))
    return app_dir


def directory_size(path):
    """目录里所有文件加起来多大（拿来对比两个版本的体积）。"""
    total = 0
    for folder, _dirs, files in os.walk(path):
        for item in files:
            try:
                total += os.path.getsize(os.path.join(folder, item))
            except OSError:
                pass
    return total


def build_installer():
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
         '--windowed', '--name', 'AutoPlay-安装程序', '--distpath', INSTALLER_DIST,
         '--workpath', os.path.join(HERE, 'build_installer'),
         '--specpath', os.path.join(HERE, 'build_installer'), 'installer.py'])
    if not os.path.isfile(INSTALLER_EXE):
        raise SystemExit('[x] 没看到 %s，安装程序本体没打出来' % INSTALLER_EXE)
    log('安装程序本体好了：%s' % INSTALLER_EXE)


# ---------- 4. payload + 追加 ----------

def build_payload(path, edition):
    """
    把一个版本的程序 + 内置曲库压成 zip：app\\ 是程序，songs\\ 是曲库。

    另外在根上放一个 edition.txt，安装程序靠它认得自己装的是哪一版
    （名字、默认目录、快捷方式都要跟着变）。
    """
    app_dir = os.path.join(edition['dist'], 'AutoPlay')
    count = 0
    total = 0
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr('edition.txt', edition['key'])
        for root, name in ((app_dir, 'app'), (SONGS_DIR, 'songs')):
            if not os.path.isdir(root):
                raise SystemExit('[x] 没有这个目录：%s' % root)
            for folder, _dirs, files in os.walk(root):
                for item in sorted(files):
                    full = os.path.join(folder, item)
                    arc = '%s/%s' % (name, os.path.relpath(full, root).replace('\\', '/'))
                    zf.write(full, arc)
                    count += 1
                    total += os.path.getsize(full)
        if os.path.isfile(ICON):            # 快捷方式要用它当图标
            zf.write(ICON, 'app/AutoPlay.ico')
            count += 1
    size = os.path.getsize(path)
    log('%s payload 好了：%s（%d 个文件，压缩前 %.1f MB，压缩后 %.1f MB）'
        % (edition['label'], path, count, total / 1048576.0, size / 1048576.0))
    return size


def attach(payload, out):
    """安装程序本体 + payload.zip + 16 字节尾巴 = 单文件安装包。"""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    offset = os.path.getsize(INSTALLER_EXE)
    with open(out, 'wb') as out_handle:
        for path in (INSTALLER_EXE, payload):
            with open(path, 'rb') as src:
                while True:
                    chunk = src.read(4 << 20)
                    if not chunk:
                        break
                    out_handle.write(chunk)
        out_handle.write(PAYLOAD_MAGIC)
        out_handle.write(struct.pack('<Q', offset))
    log('安装包好了：%s（%.1f MB）' % (out, os.path.getsize(out) / 1048576.0))
    return out


def app_version():
    """版本号只写在 installer.py 里，这儿读出来，免得两处对不上。"""
    import re
    text = open(os.path.join(HERE, 'installer.py'), encoding='utf-8').read()
    match = re.search(r"^APP_VERSION = '([^']+)'", text, re.M)
    return match.group(1) if match else '0.0'


def main(argv):
    steps = argv[1:] or ['all']
    if 'icon' in steps:
        build_icon()
        return 0
    if 'all' in steps or 'app' in steps:
        build_icon()
        for edition in EDITIONS:
            build_app(edition)
    if 'all' in steps or 'installer' in steps:
        build_installer()
    os.makedirs(os.path.join(HERE, 'build_installer'), exist_ok=True)
    made = []
    for edition in EDITIONS:
        payload = os.path.join(HERE, 'build_installer', 'payload-%s.zip' % edition['key'])
        build_payload(payload, edition)
        out = os.path.join(RELEASE_DIR, 'AutoPlay %s 安装程序 v%s.exe'
                           % (edition['label'], app_version()))
        attach(payload, out)
        made.append(out)
    log('')
    log('搞定。两个包都在 发布\\ 里，拷到别的电脑上双击就能装（那边不用装 Python）：')
    for path in made:
        log('    %s（%.1f MB）' % (os.path.basename(path),
                                 os.path.getsize(path) / 1048576.0))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))