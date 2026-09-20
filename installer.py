# -*- coding: utf-8 -*-
"""
AutoPlay 安装程序
=================

一个自带运行环境的安装程序：目标机器上什么都不用装（不需要 Python、不需要 pip），
双击就能装。界面是 Qt 画的，功能就三件事：

    选安装目录  ->  把程序解压进去  ->  建快捷方式 / 写卸载信息

打包方式
--------
1. 先 PyInstaller --onefile 把本文件打成 `AutoPlay-安装程序.exe`；
2. 再把整个程序（dist\\AutoPlay\\ 一整棵目录树 + songs\\）压成 payload.zip；
3. 把 payload.zip 直接**追加**在这个 exe 的末尾，再补 16 字节尾巴
   （8 字节魔数 + 8 字节 payload 起始偏移）。

运行时就用这个偏移把内嵌的 zip 当成普通文件读 —— 不用先把几百兆解到临时目录，
装的时候是「从自己身上流式解压到目标目录」，又快又不占额外磁盘。
（PyInstaller 的单文件 exe 是从文件末尾往回找自己的数据块的，尾巴上多挂一段
不影响它启动 —— 这是实测过的。）

payload 里两棵树：
    app/     程序本体（AutoPlay.exe + _internal\\...）
    songs/   内置曲库，装到 <安装目录>\\songs\\，默认那首就是《鸟之诗》
"""

import os
import shutil
import struct
import subprocess
import sys
import threading
import winreg
import zipfile

try:                                                  # 优先 Qt 官方绑定
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QPainter, QPen,
                               QPixmap)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame,
                                   QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                                   QPushButton, QVBoxLayout, QWidget)
except ImportError:                                   # 装了 PyQt6 也行
    from PyQt6.QtCore import Qt, pyqtSignal as Signal
    from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QPainter, QPen,
                             QPixmap)
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame,
                                 QHBoxLayout, QLabel, QLineEdit, QProgressBar,
                                 QPushButton, QVBoxLayout, QWidget)


import fileassoc                          # .mproj 文件关联（跟主程序共用同一份）

APP_NAME = 'AutoPlay'
APP_TITLE = 'MIDI 简谱自动演奏'
APP_VERSION = '1.0'
APP_EXE = 'AutoPlay.exe'
PUBLISHER = 'AutoPlay'
UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPlay'
SHORTCUT_NAME = 'AutoPlay 简谱演奏'
EDITION_LABEL = '完全版'
HAS_ASSOC = True              # 这一版要不要提供「关联 .mproj」：只有完全版有编辑器
PAYLOAD_MAGIC = b'APAYLOAD1'
TRAILER_SIZE = len(PAYLOAD_MAGIC) + 8

LOCALAPPDATA = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
DEFAULT_DIR = os.path.join(LOCALAPPDATA, 'AutoPlay')

# 两个版本：名字 / 目录 / 快捷方式 / 卸载表项都不一样，装在一起也不会互相打架。
# make_installer.py 打包时往 payload 根上放一个 edition.txt 告诉安装程序这是哪一版。
EDITIONS = {
    'full': {
        'label': '完全版',
        'title': 'MIDI 简谱自动演奏',
        'folder': 'AutoPlay',
        'shortcut': 'AutoPlay 简谱演奏',
        'key': r'Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPlay',
    },
    'lite': {
        'label': '精简版',
        'title': 'MIDI 简谱自动演奏（精简版）',
        'folder': 'AutoPlayLite',
        'shortcut': 'AutoPlay 简谱演奏（精简版）',
        'key': r'Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPlayLite',
    },
}


def apply_edition(key):
    """按版本改掉界面名字 / 默认目录 / 快捷方式名 / 卸载表项。"""
    global APP_NAME, APP_TITLE, SHORTCUT_NAME, DEFAULT_DIR, UNINSTALL_KEY, EDITION_LABEL
    global HAS_ASSOC
    info = EDITIONS.get(key) or EDITIONS['full']
    APP_NAME = 'AutoPlay' if key == 'full' else 'AutoPlayLite'
    APP_TITLE = info['title']
    SHORTCUT_NAME = info['shortcut']
    DEFAULT_DIR = os.path.join(LOCALAPPDATA, info['folder'])
    UNINSTALL_KEY = info['key']
    EDITION_LABEL = info['label']
    HAS_ASSOC = (key == 'full')          # 精简版没有简谱编辑器，关联了也打不开工程
    return info

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 13px;
          color: #e6e9ef; background: #0f1219; }
QLabel#title { font-size: 19px; font-weight: 600; }
QLabel#subtitle { color: #8b93a7; font-size: 12px; }
QLabel#value { color: #dfe4ee; }
QLabel#hint { color: #6f7787; font-size: 12px; }
QFrame#card { background: #161a23; border: 1px solid #232937; border-radius: 12px; }
QLineEdit { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
            padding: 7px 10px; color: #dfe4ee; }
QLineEdit:focus { border-color: #3b82f6; }
QPushButton { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
              padding: 8px 18px; color: #dfe4ee; }
QPushButton:hover { background: #242c3c; }
QPushButton:pressed { background: #1a2029; }
QPushButton:disabled { background: #171b24; border-color: #222836; color: #5c6478; }
QPushButton#primary { background: #3b82f6; border-color: #3b82f6; color: #ffffff;
                      font-weight: 600; }
QPushButton#primary:hover { background: #4b8ef8; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 4px;
                       border: 1px solid #2b3345; background: #1d2330; }
QCheckBox::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QProgressBar { background: #1d2330; border: none; border-radius: 6px; height: 12px;
               text-align: center; color: #8b93a7; }
QProgressBar::chunk { background: #3b82f6; border-radius: 6px; }
"""


# ============ 内嵌数据 ============

def payload_source():
    """
    内嵌的 payload.zip 在哪儿。

    返回 (路径, 起始偏移)。打包好的安装程序是「自己 exe + 尾巴上的 zip」；
    开发时直接跑源码，就找同目录下的 payload.zip。
    """
    exe = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
    try:
        with open(exe, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size > TRAILER_SIZE:
                handle.seek(size - TRAILER_SIZE)
                trailer = handle.read(TRAILER_SIZE)
                if trailer[:len(PAYLOAD_MAGIC)] == PAYLOAD_MAGIC:
                    return exe, struct.unpack('<Q', trailer[len(PAYLOAD_MAGIC):])[0]
    except OSError:
        pass
    for folder in (os.path.dirname(exe), os.getcwd()):
        path = os.path.join(folder, 'payload.zip')
        if os.path.isfile(path):
            return path, 0
    return None, None


def read_edition():
    """
    这个安装包里装的是哪一版。

    打包时 make_installer.py 会往 payload.zip 的根上放一个 edition.txt；
    读不到就当成完全版（自己手动压的 payload 基本都是完全版）。
    """
    try:
        payload, _path = open_payload()
        with payload:
            info = payload.getinfo('edition.txt')
            return payload.read(info).decode('utf-8').strip() or 'full'
    except Exception:
        return 'full'


class OffsetReader:
    """
    把「从第 offset 字节开始的内嵌 zip」包装成一个普通只读文件对象。

    zipfile 只会用 read / seek / tell，所以把它骗过去就行 ——
    这样就不用先把几百兆复制出来再解压。
    """

    def __init__(self, handle, offset):
        self.handle = handle
        self.offset = offset
        handle.seek(0, os.SEEK_END)
        self.length = handle.tell() - offset

    def read(self, size=-1):
        if size is None or size < 0:
            return self.handle.read()
        return self.handle.read(size)

    def seek(self, pos, whence=0):
        if whence == 0:
            target = pos
        elif whence == 1:
            target = self.tell() + pos
        else:
            target = self.length + pos
        self.handle.seek(self.offset + max(0, target))
        return self.tell()

    def tell(self):
        return self.handle.tell() - self.offset

    def seekable(self):
        return True

    def readable(self):
        return True

    def close(self):
        self.handle.close()


def open_payload():
    """打开内嵌的 payload.zip，返回 (zipfile, 描述用的路径)。"""
    path, offset = payload_source()
    if path is None:
        raise RuntimeError('这个安装程序里没有内嵌的程序本体（payload.zip 丢了）。\n'
                           '请重新下载完整的安装包。')
    return zipfile.ZipFile(OffsetReader(open(path, 'rb'), offset)), path
def format_size(size):
    """字节 -> `123 MB` 这种给人看的写法。"""
    size = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return '%.1f %s' % (size, unit) if unit != 'B' else '%d B' % size
        size /= 1024


def shell_folder(name, fallback):
    """
    问注册表「桌面 / 开始菜单」到底在哪个目录。

    不能想当然用 %USERPROFILE%\\Desktop —— OneDrive 会把桌面整个搬走，
    还有不少人改过路径，问注册表最稳。
    """
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            value, _kind = winreg.QueryValueEx(key, name)
        path = os.path.expandvars(value)
        if path:
            return path
    except OSError:
        pass
    return os.path.expandvars(fallback)


def desktop_dir():
    return shell_folder('Desktop', r'%USERPROFILE%\Desktop')


def start_menu_dir():
    return shell_folder('Programs', r'%APPDATA%\Microsoft\Windows\Start Menu\Programs')


def make_shortcut(link, target, workdir, icon=None, description=''):
    """
    建一个 .lnk。

    没装 pywin32，所以借系统自带的 PowerShell 调 WScript.Shell —— 这台机器上
    只要有 Windows 就有它，不用额外带任何东西。
    """
    def quote(text):
        return "'" + str(text).replace("'", "''") + "'"

    script = ['$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);' % quote(link),
              '$s.TargetPath=%s;' % quote(target),
              '$s.WorkingDirectory=%s;' % quote(workdir)]
    if icon:
        script.append('$s.IconLocation=%s;' % quote(icon))
    if description:
        script.append('$s.Description=%s;' % quote(description))
    script.append('$s.Save();')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive',
                             '-Command', ''.join(script)],
                            capture_output=True, text=True, creationflags=flags)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or '创建快捷方式失败')
    return link


def make_uninstaller(install_dir, shortcuts, assoc=False):
    """
    在安装目录里写一个卸载用的 bat（路径都写死，双击就能卸干净）。

    assoc=True 表示装的时候关联过 .mproj，卸载时把注册表里那几项一起删掉。
    删除的写法跟 fileassoc.UNINSTALL_BAT_LINES 是同一份 —— 装和卸用的是同一套路径，
    免得一边改了一边忘了。
    """
    path = os.path.join(install_dir, '卸载.bat')
    lines = ['@echo off', 'chcp 65001 >nul',
             'rem AutoPlay 卸载程序（安装时自动生成）',
             'rem 自己正跑在被删的目录里，所以先复制到 TEMP 再换个进程跑',
             'if not "%~1"=="go" (',
             '  copy /y "%~f0" "%TEMP%\\AutoPlay-uninstall.bat" >nul',
             '  start "" /min "%TEMP%\\AutoPlay-uninstall.bat" go',
             '  exit /b',
             ')',
             'timeout /t 2 /nobreak >nul',
             'taskkill /f /im %s >nul 2>&1' % APP_EXE,
             'rd /s /q "%s"' % install_dir]
    for link in shortcuts:
        lines.append('del "%s" >nul 2>&1' % link)
    if assoc:
        lines.append('rem 取消 .mproj 工程文件关联（只删本程序写的那几项）')
        lines += list(fileassoc.UNINSTALL_BAT_LINES)
    lines += ['reg delete "%s" /f >nul 2>&1' % UNINSTALL_KEY,
              'del "%~f0" >nul 2>&1',
              '']
    with open(path, 'w', encoding='utf-8', newline='\r\n') as handle:
        handle.write('\n'.join(lines))
    return path


def register_uninstall(install_dir, uninstaller, size_kb):
    """写进「应用和功能」，让用户能像卸正常软件一样卸掉它。"""
    values = (('DisplayName', APP_TITLE, winreg.REG_SZ),
              ('DisplayVersion', APP_VERSION, winreg.REG_SZ),
              ('Publisher', PUBLISHER, winreg.REG_SZ),
              ('InstallLocation', install_dir, winreg.REG_SZ),
              ('UninstallString', '"%s"' % uninstaller, winreg.REG_SZ),
              ('DisplayIcon', os.path.join(install_dir, APP_EXE), winreg.REG_SZ),
              ('EstimatedSize', int(size_kb), winreg.REG_DWORD),
              ('NoModify', 1, winreg.REG_DWORD),
              ('NoRepair', 1, winreg.REG_DWORD))
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_WRITE) as key:
        for name, value, kind in values:
            winreg.SetValueEx(key, name, 0, kind, value)


def free_space(path):
    """这个目录所在的盘还剩多少字节（目录还不存在就往上找）。"""
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        return shutil.disk_usage(path or os.getcwd()).free
    except OSError:
        return 0

# ============ 装的过程 ============

class Installer:
    """真正干活的：解压 -> 建快捷方式 -> 写卸载信息。全在后台线程里跑。"""

    def __init__(self, options, report):
        self.options = options          # {'dir':…, 'desktop':bool, 'startmenu':bool}
        self.report = report            # report(在干嘛, 已完成字节, 总字节)

    def run(self):
        target = self.options['dir']
        self.report('准备安装目录…', 0, 1)
        os.makedirs(target, exist_ok=True)
        self._check_writable(target)

        payload, _path = open_payload()
        with payload:
            self._extract(payload, target)

        self.report('写说明文件…', 1, 1)
        self._write_readme(target)
        shortcuts = self._shortcuts(target)
        assoc = self._register_assoc(target) if self.options.get('assoc') else ''
        self.report('写入卸载信息…', 1, 1)
        size_kb = self._dir_size(target) / 1024.0
        uninstaller = make_uninstaller(target, shortcuts, assoc=bool(assoc))
        register_uninstall(target, uninstaller, size_kb)
        self.report('装好了', 1, 1)
        return target

    def _register_assoc(self, target):
        """
        把 .mproj 关联到刚装好的这个 exe。

        写的是 HKCU\\Software\\Classes，不需要管理员权限。关联不上（比如别的软件
        把根键锁了）不该让整个安装失败，报一声就接着往下装。
        """
        exe = os.path.join(target, APP_EXE)
        try:
            fileassoc.register(exe, notify=False)
            fileassoc.notify_shell()
            self.report('关联 .mproj 工程文件：双击就直接打开编辑器', 1, 1)
            return exe
        except Exception as exc:
            self.report('关联 .mproj 没成功（不影响使用）：%s' % exc, 1, 1)
            return ''

    @staticmethod
    def _write_readme(target):
        """放一份说明书在安装目录里 —— 装完就忘了怎么用是最常见的。"""
        text = """%s v%s（%s）
%s

怎么用
------
1. 双击 AutoPlay.exe。第一次会问一次管理员权限 —— 这是必须的：游戏要用管理员
   身份运行，本程序也必须提权，否则 Windows 会把发过去的按键全拦掉。
2. 启动后会自己载入内置曲库里的《鸟之诗》。想换曲子点「选择 MIDI 文件…」，
   或者点「内置曲库」。
3. 点「开始演奏」，然后切回游戏。开始之前先点一下游戏窗口让它拿到焦点。

快捷键（界面右下角「快捷键设置…」里可以改）
--------------------------------------------
F6        开始演奏
F7        暂停 / 继续
F8        停止
F10       开始 / 结束录制（把你弹的一段记成谱面 + midi）
Ctrl+F1   在游戏里唤起主界面（再按一下收起来，不会把游戏顶回桌面）
Ctrl+F2   开关跟奏模式

录制（完全版才有）
------------------
按 F10 开始录，在游戏里弹 z x c v b n m ,（升降调 / 升半音照常用鼠标左中右键），
再按一下 F10 收工。程序会把你弹的每个音和按住多长都记下来，写成谱面 + midi，
并且直接摆进「简谱编辑器」——录的时候手滑了，当场拖一下就行。
文件放在 %%LOCALAPPDATA%%\\AutoPlay\\recordings\\。录制期间别的键（跑步的 WASD、
热键）一律不录。
界面上那个「录制时发声」是个纯开关：勾着，录制时按一个琴键就响一声（听得出自己
弹得对不对）；不勾就全程不出声。嫌吵就关掉。

两个标签页
----------
演奏        选曲子、调音长、试听、开始弹。
简谱编辑器  把音轨铺成钢琴卷帘手动改：双击空白加音、右键音块删音、拖着改长短，
            拖右边缘改时值，Ctrl+Z 撤销。改完点「导出 MIDI…」，导出的文件会
            自动载回主程序。那一页里：空格 = 播放/停止，F11 = 最大化 / 还原。
            没改完也能存成工程文件（.mproj）：装的时候勾了「关联文件类型」的话，
            以后在资源管理器里双击 .mproj 就直接开程序并进这一页。
            程序已经开着的时候双击工程文件，**不会**再起一个程序 —— 那个新进程会把
            文件交给已经在跑的这个，自己退出；这边另开一个编辑器窗口打开它。

文件夹
------
songs\\        内置曲库，往里面扔 .mid 就会出现在「内置曲库」里
_internal\\    运行库，别删别改

谱面文件
--------
程序会在 midi 旁边生成 TONIC<主音> <曲名>.txt（简谱谱面）；
那个目录写不进去时会放到 %%LOCALAPPDATA%%\\AutoPlay\\scores\\。

出问题看日志
------------
%%LOCALAPPDATA%%\\AutoPlay\\AutoPlay.log —— 闪退、按键没反应，看它的最后几行。

卸载
----
设置 →「应用」里搜 AutoPlay；或者直接删掉整个安装目录。
""" % (APP_TITLE, APP_VERSION, EDITION_LABEL,
                '=' * (len(APP_TITLE) + len(APP_VERSION) + len(EDITION_LABEL) + 5))
        with open(os.path.join(target, '说明.txt'), 'w', encoding='utf-8',
                  newline='\r\n') as handle:
            handle.write(text)
        return text

    @staticmethod
    def _check_writable(target):
        probe = os.path.join(target, '.autoplay-write-test')
        try:
            with open(probe, 'w') as handle:
                handle.write('ok')
            os.remove(probe)
        except OSError as exc:
            raise RuntimeError(
                '这个目录写不进去：%s\n\n%s\n\n'
                '换一个目录（比如「恢复默认」那个），或者右键安装程序\n'
                '选「以管理员身份运行」。' % (target, exc))

    def _extract(self, payload, target):
        """把 payload 流式解压到目标目录，顺便报进度。"""
        entries = []
        for info in payload.infolist():
            name = info.filename.replace('\\', '/')
            if info.is_dir():
                continue
            if name.startswith('app/'):
                subs = name[4:]
            elif name.startswith('songs/'):
                subs = name
            else:
                continue
            if not subs or subs.startswith('/') or '..' in subs.split('/'):
                continue
            entries.append((info, subs))
        total = sum(info.file_size for _info, _subs in entries) or 1
        done = 0
        for info, subs in entries:
            dest = os.path.join(target, *subs.split('/'))
            folder = os.path.dirname(dest)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with payload.open(info) as source, open(dest, 'wb') as out:
                while True:
                    chunk = source.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    self.report('正在安装：%s' % subs, done, total)
        return done

    def _shortcuts(self, target):
        """按用户勾的选择建快捷方式，返回建了哪些（卸载时要删）。"""
        exe = os.path.join(target, APP_EXE)
        icon = os.path.join(target, 'AutoPlay.ico')
        if not os.path.isfile(icon):
            icon = exe
        made = []
        wanted = []
        if self.options.get('desktop'):
            wanted.append(os.path.join(desktop_dir(), SHORTCUT_NAME + '.lnk'))
        if self.options.get('startmenu'):
            wanted.append(os.path.join(start_menu_dir(), SHORTCUT_NAME + '.lnk'))
        for link in wanted:
            self.report('创建快捷方式：%s' % link, 1, 1)
            try:
                os.makedirs(os.path.dirname(link), exist_ok=True)
                make_shortcut(link, exe, target, icon, APP_TITLE)
                made.append(link)
            except Exception as exc:      # 快捷方式建不上不该让整个安装失败
                self.report('快捷方式没建成（%s）：%s' % (os.path.basename(link), exc), 1, 1)
        return made

    @staticmethod
    def _dir_size(path):
        total = 0
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total


# ============ 界面 ============

class InstallerWindow(QWidget):
    """安装界面：选目录 -> 进度 -> 完成，三页来回切。"""

    on_progress = Signal(str, int, int)     # 后台线程 -> 界面
    on_done = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('%s 安装程序 v%s' % (APP_TITLE, APP_VERSION))
        self.setMinimumWidth(620)
        self.setStyleSheet(STYLE)
        self.setWindowIcon(make_icon())
        self.installer = None
        self.target = ''
        self._build()
        self.on_progress.connect(self._show_progress)
        self.on_done.connect(self._show_result)
        self.on_progress.emit('', 0, 1)
        self._load_payload_info()

    # ---- 界面 ----

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        self.title = QLabel('安装 %s' % APP_TITLE)
        self.title.setObjectName('title')
        root.addWidget(self.title)
        self.subtitle = QLabel('v%s · %s · 自带运行环境，目标电脑不需要装 Python，装完就能用'
                               % (APP_VERSION, EDITION_LABEL))
        self.subtitle.setObjectName('subtitle')
        root.addWidget(self.subtitle)

        root.addWidget(self._build_options(), 1)
        root.addWidget(self._build_progress(), 1)
        root.addWidget(self._build_done(), 1)

        self.options_page.setVisible(True)
        self.progress_page.setVisible(False)
        self.done_page.setVisible(False)

    def _card(self):
        card = QFrame()
        card.setObjectName('card')
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        return card, box

    def _build_options(self):
        self.options_page, box = self._card()

        box.addWidget(self._label('安装位置'))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.dir_edit = QLineEdit(DEFAULT_DIR)
        self.dir_edit.textChanged.connect(lambda *_: self._refresh_space())
        row.addWidget(self.dir_edit, 1)
        browse = QPushButton('浏览…')
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        default = QPushButton('恢复默认')
        default.clicked.connect(lambda: self.dir_edit.setText(DEFAULT_DIR))
        row.addWidget(default)
        box.addLayout(row)
        hint = QLabel('装到当前用户目录下不需要管理员权限；想要全机共用可以改成 '
                      'C:\\Program Files\\AutoPlay（那种目录要管理员权限）。')
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        box.addWidget(hint)

        self.desktop_box = QCheckBox('创建桌面快捷方式')
        self.desktop_box.setChecked(True)
        self.startmenu_box = QCheckBox('创建开始菜单快捷方式')
        self.startmenu_box.setChecked(True)
        self.run_box = QCheckBox('安装完成后立刻运行')
        self.run_box.setChecked(True)
        box.addWidget(self.desktop_box)
        box.addWidget(self.startmenu_box)
        self.assoc_box = None
        if HAS_ASSOC:                 # 精简版没有编辑器，不提供这个选项
            self.assoc_box = QCheckBox('双击 .mproj 工程文件用本程序打开（关联文件类型）')
            self.assoc_box.setChecked(True)
            self.assoc_box.setToolTip('把简谱工程的工程文件（.mproj）关联到本程序：以后在资源管理器里\n'
                                      '双击它就直接开程序并进「简谱编辑器」。\n'
                                      '只写在当前用户（HKCU）里，不需要管理员权限，卸载时会一起清掉。')
            box.addWidget(self.assoc_box)
        box.addWidget(self.run_box)

        self.space_label = QLabel('')
        self.space_label.setObjectName('value')
        box.addWidget(self.space_label)
        box.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        quit_button = QPushButton('退出')
        quit_button.clicked.connect(self.close)
        buttons.addWidget(quit_button)
        self.install_button = QPushButton('开始安装')
        self.install_button.setObjectName('primary')
        self.install_button.setDefault(True)
        self.install_button.clicked.connect(self.start_install)
        buttons.addWidget(self.install_button)
        box.addLayout(buttons)
        return self.options_page

    def _build_progress(self):
        self.progress_page, box = self._card()
        self.progress_title = QLabel('正在安装…')
        self.progress_title.setObjectName('title')
        box.addWidget(self.progress_title)
        self.progress_file = QLabel('')
        self.progress_file.setObjectName('hint')
        self.progress_file.setWordWrap(True)
        box.addWidget(self.progress_file)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(12)
        box.addWidget(self.bar)
        self.progress_bytes = QLabel('')
        self.progress_bytes.setObjectName('value')
        box.addWidget(self.progress_bytes)
        box.addStretch(1)
        return self.progress_page

    def _build_done(self):
        self.done_page, box = self._card()
        self.done_title = QLabel('装好了')
        self.done_title.setObjectName('title')
        box.addWidget(self.done_title)
        self.done_text = QLabel('')
        self.done_text.setObjectName('value')
        self.done_text.setWordWrap(True)
        box.addWidget(self.done_text)
        box.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.open_dir_button = QPushButton('打开安装目录')
        self.open_dir_button.clicked.connect(self._open_dir)
        buttons.addWidget(self.open_dir_button)
        self.run_button = QPushButton('运行 AutoPlay')
        self.run_button.setObjectName('primary')
        self.run_button.clicked.connect(self._run_app)
        buttons.addWidget(self.run_button)
        self.close_button = QPushButton('完成')
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        box.addLayout(buttons)
        return self.done_page

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setObjectName('value')
        return label

    # ---- 干活 ----

    def _load_payload_info(self):
        """看一眼内嵌的程序本体有多大，顺便把「要多少空间」显示出来。"""
        try:
            payload, _path = open_payload()
        except Exception as exc:
            self.payload_bytes = 0
            self.space_label.setText(str(exc))
            self.install_button.setEnabled(False)
            return
        with payload:
            self.payload_bytes = sum(info.file_size for info in payload.infolist())
        self._refresh_space()

    def _refresh_space(self):
        if not self.payload_bytes:
            return
        text = '需要 %s' % format_size(self.payload_bytes)
        free = free_space(self.dir_edit.text().strip() or DEFAULT_DIR)
        if free:
            text += ' · 这个盘还剩 %s' % format_size(free)
            if free < self.payload_bytes * 1.05:
                text += ' · 空间可能不够'
        self.space_label.setText(text)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, '选择安装位置',
                                                  self.dir_edit.text().strip() or DEFAULT_DIR)
        if folder:
            self.dir_edit.setText(os.path.join(folder, APP_NAME))

    def start_install(self):
        target = os.path.normpath(os.path.expandvars(self.dir_edit.text().strip() or DEFAULT_DIR))
        if len(target) < 4 or not os.path.splitdrive(target)[0]:
            self.space_label.setText('这个安装位置看着不对劲：%s' % target)
            return
        self.target = target
        self.options_page.setVisible(False)
        self.progress_page.setVisible(True)
        self.title.setText('正在安装')
        self.subtitle.setText('别关这个窗口，装完会自己跳转')
        options = {'dir': target,
                   'desktop': self.desktop_box.isChecked(),
                   'startmenu': self.startmenu_box.isChecked(),
                   'assoc': bool(self.assoc_box is not None and self.assoc_box.isChecked())}
        threading.Thread(target=self._install, args=(options,), daemon=True).start()

    def _install(self, options):
        try:
            target = Installer(options, self._emit).run()
        except Exception as exc:
            self.on_done.emit(False, str(exc))
            return
        self.on_done.emit(True, target)

    def _emit(self, text, done, total):
        """后台线程里调 —— 发信号回主线程刷界面。"""
        self.on_progress.emit(text, int(done), int(total))

    def _show_progress(self, text, done, total):
        if text:
            self.progress_file.setText(text)
        if total > 0:
            self.bar.setValue(max(0, min(1000, int(1000.0 * done / total))))
        self.progress_bytes.setText('%s / %s' % (format_size(done), format_size(total)))

    def _show_result(self, ok, message):
        self.progress_page.setVisible(False)
        self.done_page.setVisible(True)
        if not ok:
            self.title.setText('安装失败')
            self.done_title.setText('没装成')
            self.done_text.setText(message)
            self.run_button.setEnabled(False)
            self.open_dir_button.setEnabled(False)
            return
        self.target = message or self.target
        self.title.setText('装好了')
        self.done_title.setText('安装完成')
        self.done_text.setText(
            '装到：%s\n\n'
            '· 双击桌面上的「%s」就能用（第一次运行会问一次管理员权限，'
            '因为要往游戏里发按键）；\n'
            '· 内置曲库在同目录的 songs 文件夹，默认那首是《鸟之诗》；\n'
            '· 想卸载：设置 →「应用」里搜 AutoPlay，或者直接删掉这个目录。'
            % (self.target, SHORTCUT_NAME))
        if self.run_box.isChecked():
            self._run_app()

    def _open_dir(self):
        if self.target and os.path.isdir(self.target):
            os.startfile(self.target)

    def _run_app(self):
        exe = os.path.join(self.target, APP_EXE)
        if not os.path.isfile(exe):
            self.done_text.setText('没找到 %s' % exe)
            return
        try:
            os.startfile(exe)             # 走 shell：该弹 UAC 就弹
        except OSError as exc:
            self.done_text.setText('启动失败：%s' % exc)
            return
        self.close()


from noteicon import make_icon            # 跟主程序同一个图标


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    apply_edition(read_edition())        # 完全版还是精简版，看内嵌的 payload
    app = QApplication(argv)
    app.setApplicationName(APP_TITLE + ' 安装程序')
    app.setStyle('Fusion')
    window = InstallerWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
