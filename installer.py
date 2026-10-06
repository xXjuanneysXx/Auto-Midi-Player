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
3. 把 payload.zip 直接**追加**在这个 exe 的末尾，再补 17 字节尾巴
   （9 字节魔数 `APAYLOAD1` + 8 字节 payload 起始偏移）。

运行时就用这个偏移把内嵌的 zip 当成普通文件读 —— 不用先把几百兆解到临时目录，
装的时候是「从自己身上流式解压到目标目录」，又快又不占额外磁盘。
（PyInstaller 的单文件 exe 是从文件末尾往回找自己的数据块的，尾巴上多挂一段
不影响它启动 —— 这是实测过的。）

payload 里两棵树：
    app/     程序本体（AutoPlay.exe + _internal\\...）
    songs/   内置曲库，装到 <安装目录>\\songs\\，默认那首就是《鸟之诗》

app/ 里还带着打包好的卸载程序 uninstall.exe，装完就躺在安装目录根上；安装程序再往
旁边写一份 uninstall.json，告诉它「装在哪、删哪些快捷方式、要不要取消 .mproj 关联」。

在线安装（同一个源码的另一种用法）
----------------------------------
不带 payload 的「在线安装程序」只有 46 MB 左右，双击后它发现身边没有程序本体，
就转去联网：

    拉 payload.json（曲库仓库根目录）
        -> 界面选完全版 / 精简版
        -> 按清单把 32 MB 一片的程序本体下到 %LOCALAPPDATA%\\AutoPlay\\下载缓存\\
        -> 用「多片拼起来的 zip 流」直接解压安装（不拼出大 zip）

断点续传：下完的片校验 sha256 后跳过；下到一半的片带 HTTP Range 接着下；
片挂在 Gitee Releases 上（单个附件上限 100 MB，所以切 32 MB，完全版 6 片、精简版 2 片）。
清单和片都是 `make_parts.py` + `上传分片到Gitee.py` 产出的。
"""

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import winreg
import zipfile

try:                                                  # 优先 Qt 官方绑定
    from PySide6.QtCore import Qt, Signal
    from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QPainter, QPen,
                               QPixmap)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                                   QFrame, QHBoxLayout, QLabel, QLineEdit,
                                   QProgressBar, QPushButton, QVBoxLayout, QWidget)
except ImportError:                                   # 装了 PyQt6 也行
    from PyQt6.QtCore import Qt, pyqtSignal as Signal
    from PyQt6.QtGui import (QBrush, QColor, QFont, QIcon, QPainter, QPen,
                             QPixmap)
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                                 QFrame, QHBoxLayout, QLabel, QLineEdit,
                                 QProgressBar, QPushButton, QVBoxLayout, QWidget)


import fileassoc                          # .mproj 文件关联（跟主程序共用同一份）

APP_NAME = 'AutoPlay'
APP_TITLE = 'MIDI 简谱自动演奏'
APP_VERSION = '1.1.0'
APP_EXE = 'AutoPlay.exe'
UNINSTALL_EXE = 'uninstall.exe'       # 打包时放进 payload，装完在安装目录根上
UNINSTALL_JSON = 'uninstall.json'     # 安装时写，告诉上面那个 exe 该怎么卸
PUBLISHER = 'AutoPlay'
UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPlay'
SHORTCUT_NAME = 'AutoPlay 简谱演奏'
EDITION_LABEL = '完全版'
HAS_ASSOC = True              # 这一版要不要提供「关联 .mproj」：只有完全版有编辑器
PAYLOAD_MAGIC = b'APAYLOAD1'
TRAILER_SIZE = len(PAYLOAD_MAGIC) + 8

LOCALAPPDATA = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
DEFAULT_DIR = os.path.join(LOCALAPPDATA, 'AutoPlay')

# ---- 在线安装（这个 exe 里没有内嵌程序本体时用） ----
# 清单（payload.json）跟 version.json / notice.json 一样放在曲库仓库根目录，
# Gitee 的 raw 直链实测能读中文 json，不用走要令牌的 contents 接口。
MANIFEST_URLS = (
    'https://gitee.com/juanneys/midi-music/raw/master/payload.json',
    'https://gitee.com/juanneys/midi-music/raw/main/payload.json',
)
DOWNLOAD_DIR = os.path.join(LOCALAPPDATA, 'AutoPlay', '下载缓存')
NET_TIMEOUT = 20.0            # 单次请求超时（秒）—— 分片只有 32 MB，别等太久
NET_RETRIES = 3               # 一片最多试几次（每次换一个地址 / 重试）
USER_AGENT = 'AutoPlay-Installer/%s' % APP_VERSION

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


def has_embedded_payload():
    """这个 exe 里带没带程序本体。没带 = 在线安装程序，得联网下分片。"""
    return payload_source()[0] is not None


# ============ 在线安装：拉清单 + 下分片 ============

def _no_cache(url):
    """给地址加个每次都变的参数 —— Gitee 的 raw 走 CDN，裸链接可能吐旧内容。"""
    return '%s%s_=%d' % (url, '&' if '?' in url else '?', time.time())


def _http_open(url, timeout=NET_TIMEOUT, start=None):
    """
    打开一个下载地址。start 不是 None 时带 `Range: bytes=<start>-`，
    实现断点续传；服务器不认 Range（回 200）时调用方自己把多出来的那截丢掉。
    """
    headers = {'User-Agent': USER_AGENT}
    if start:
        headers['Range'] = 'bytes=%d-' % start
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                  timeout=timeout)


def _http_bytes(url, timeout=NET_TIMEOUT):
    with _http_open(url, timeout=timeout) as response:
        return response.read()


def _duration(seconds):
    """秒 -> `3 分 12 秒` 这种给人看的写法（剩余时间用）。"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return '%d 秒' % seconds
    if seconds < 3600:
        return '%d 分 %d 秒' % (seconds // 60, seconds % 60)
    return '%d 小时 %d 分' % (seconds // 3600, (seconds % 3600) // 60)


def fetch_manifest(timeout=NET_TIMEOUT):
    """
    拉在线安装清单（payload.json），返回 dict。

    清单里写着每个版本的程序压成几片、每片多大、sha256 是多少、去哪儿下。
    哪个地址都拉不到就报错，让用户换回百度网盘那份完整安装包。
    """
    problems = []
    for url in manifest_urls():
        try:
            data = json.loads(_http_bytes(_no_cache(url), timeout).decode('utf-8', 'replace'))
            if isinstance(data, dict) and data.get('editions'):
                return data
            problems.append('%s：内容不对（没有 editions）' % url)
        except Exception as exc:
            problems.append('%s：%s' % (url, exc))
    raise RuntimeError('拉不到在线安装清单（payload.json）：\n\n%s\n\n'
                       '请检查网络连接；实在连不上就改用百度网盘那份完整安装包。'
                       % '\n'.join(problems))


def manifest_urls():
    """
    清单地址：环境变量 `AUTOPLAY_PAYLOAD_INDEX` 指哪个就用哪个（调试、或以后换镜像
    地址时不用重新打包），没设就用代码里内置的那两个。
    """
    override = str(os.environ.get('AUTOPLAY_PAYLOAD_INDEX') or '').strip()
    return (override,) if override else MANIFEST_URLS


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            blob = handle.read(1 << 20)
            if not blob:
                break
            digest.update(blob)
    return digest.hexdigest()


class PartsReader:
    """
    把下载好的多片 zip 拼成一个能随机读的只读文件对象。

    zipfile 要 seek 到末尾读「中央目录」，所以这个壳子必须支持 seek ——
    有了它就不用先把 175 MB 的片拼成一个大 zip（省磁盘、省时间）。
    """

    def __init__(self, paths):
        self.handles = [open(path, 'rb') for path in paths]
        self.sizes = [os.fstat(handle.fileno()).st_size for handle in self.handles]
        self.offsets = []
        length = 0
        for size in self.sizes:
            self.offsets.append(length)
            length += size
        self.length = length
        self.pos = 0

    def _which(self, pos):
        index = 0
        for i, offset in enumerate(self.offsets):
            if pos >= offset:
                index = i
        return index

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.length - self.pos
        out = bytearray()
        while size > 0 and self.pos < self.length:
            index = self._which(self.pos)
            handle = self.handles[index]
            handle.seek(self.pos - self.offsets[index])
            blob = handle.read(size)
            if not blob:
                break
            out += blob
            self.pos += len(blob)
            size -= len(blob)
        return bytes(out)

    def seek(self, pos, whence=0):
        if whence == 0:
            target = pos
        elif whence == 1:
            target = self.pos + pos
        else:
            target = self.length + pos
        self.pos = max(0, min(self.length, target))
        return self.pos

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def readable(self):
        return True

    def close(self):
        for handle in self.handles:
            try:
                handle.close()
            except OSError:
                pass


class PartsDownloader:
    """
    把清单里某一版的切片下到本地缓存目录，返回一个拼好的 PartsReader。

    断点续传做两层：
      · 整片已经下完（大小 + sha256 都对）—— 直接跳过，程序关掉再开接着用；
      · 下到一半（文件在、大小不够）—— 带 HTTP Range 接着下，不从头再来。
    下完的片**留着不删**（在 %LOCALAPPDATA%\\AutoPlay\\下载缓存\\<版本> 里），
    重装 / 换版本都能接着用。哪片失败最多试 NET_RETRIES 次，每次换一个镜像地址。
    """

    def __init__(self, manifest, edition_key, report):
        self.manifest = manifest
        self.key = edition_key
        self.info = (manifest.get('editions') or {}).get(edition_key)
        if not self.info:
            raise RuntimeError('这份在线安装清单里没有「%s」这个版本，'
                               '可能清单刚更新过，请重新下载在线安装程序。' % edition_key)
        self.report = report
        self.cache = os.path.join(DOWNLOAD_DIR, edition_key,
                                  str(manifest.get('app_version') or 'unknown'))
        self.started = time.time()

    def total_download(self):
        return sum(int(part['size']) for part in self.info.get('parts') or [])

    def run(self):
        os.makedirs(self.cache, exist_ok=True)
        parts = self.info.get('parts') or []
        if not parts:
            raise RuntimeError('这份清单里「%s」没有切片，装不了' % self.key)
        total = self.total_download() or 1
        done = 0
        paths = []
        for index, part in enumerate(parts, 1):
            path = os.path.join(self.cache, part['name'])
            self._ensure(index, len(parts), part, path, done, total)
            paths.append(path)
            done += int(part['size'])
        self.report('下载完成，开始安装…', done, done)
        return PartsReader(paths)

    def _urls_for(self, name):
        urls = ['%s/%s' % (str(base).rstrip('/'), name)
                for base in (self.info.get('base_urls') or [])]
        for mirror in (self.manifest.get('mirrors') or []):
            urls.append('%s/%s' % (str(mirror).rstrip('/'), name))
        if not urls:
            raise RuntimeError('这份清单里没有下载地址（base_urls 是空的）')
        return urls

    def _ensure(self, index, count, part, path, done, total):
        name = part['name']
        size = int(part['size'])
        digest = str(part.get('sha256') or '')
        if os.path.isfile(path):
            have = os.path.getsize(path)
            if have == size and (not digest or sha256_file(path) == digest):
                self.report('第 %d/%d 片已经下过了，跳过' % (index, count),
                            done + size, total)
                return
            if have > size:
                try:
                    os.remove(path)
                except OSError:
                    pass
        urls = self._urls_for(name)
        last = ''
        for attempt in range(1, NET_RETRIES + 1):
            url = urls[(attempt - 1) % len(urls)]
            try:
                self._fetch(index, count, part, path, url, done, total)
                return
            except Exception as exc:
                last = str(exc)
                if attempt < NET_RETRIES:
                    self.report('第 %d/%d 片没下成（%s），换源重试…' % (index, count, last),
                                done, total)
        raise RuntimeError('下载第 %d/%d 片失败：%s\n\n%s\n\n'
                           '可以重开这个安装程序接着下（已经下好的片会跳过），'
                           '或者改用百度网盘那份完整安装包。' % (index, count, last, name))

    def _fetch(self, index, count, part, path, url, done, total):
        name = part['name']
        size = int(part['size'])
        digest = str(part.get('sha256') or '')
        received = os.path.getsize(path) if os.path.isfile(path) else 0
        if received >= size:
            received = 0
        with open(path, 'ab' if received else 'wb') as out:
            while received < size:
                before = received
                response = _http_open(url, NET_TIMEOUT, start=received or None)
                with response:
                    skip = 0
                    if received and getattr(response, 'status', 200) != 206:
                        skip = received          # 服务器不认 Range：把重给的前半截丢掉
                    while True:
                        blob = response.read(1 << 18)
                        if not blob:
                            break
                        if skip:
                            if len(blob) <= skip:
                                skip -= len(blob)
                                continue
                            blob = blob[skip:]
                            skip = 0
                        out.write(blob)
                        received += len(blob)
                        self._tick(index, count, name, received, size, done, total)
                out.flush()
                if received == before:
                    raise RuntimeError('对方提前断开了（下到 %d 字节）' % received)
        if received != size:
            raise RuntimeError('大小不对：下到 %d 字节，应该是 %d' % (received, size))
        if digest and sha256_file(path) != digest:
            try:
                os.remove(path)
            except OSError:
                pass
            raise RuntimeError('这一片下坏了（sha256 对不上），已经删掉，下次重下')

    def _tick(self, index, count, name, received, size, done, total):
        downloaded = done + received
        elapsed = max(0.001, time.time() - self.started)
        speed = downloaded / elapsed
        eta = (total - downloaded) / speed if speed > 0 else 0
        self.report('正在下载：第 %d/%d 片 %s · %.1f MB/s · 还剩约 %s'
                    % (index, count, name, speed / 1048576.0, _duration(eta)),
                    downloaded, total)


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

    两处兜底：
      - powershell 不在 PATH 上（精简系统 / 特殊环境）就按 %SystemRoot% 拼绝对路径再试；
      - 建完喊一声 SHChangeNotify。光把 .lnk 写进文件系统，资源管理器不一定会马上重画
        桌面，用户装完往桌面上一看「怎么没有快捷方式」，其实只是没刷新。

    建完还会回头看一眼文件到底在不在 —— 宁可报一句错，也别让用户对着空桌面猜。
    """
    def quote(text):
        return "'" + str(text).replace("'", "''") + "'"

    # 先让 PowerShell 用 UTF-8 吐结果：不然报错信息是本地代码页，解码出来全是乱码
    script = ['[Console]::OutputEncoding=[Text.Encoding]::UTF8;',
              '$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s);' % quote(link),
              '$s.TargetPath=%s;' % quote(target),
              '$s.WorkingDirectory=%s;' % quote(workdir)]
    if icon:
        script.append('$s.IconLocation=%s;' % quote(icon))
    if description:
        script.append('$s.Description=%s;' % quote(description))
    script.append('$s.Save();')
    command = ''.join(script)
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    problems = []
    for exe in powershell_paths():
        try:
            result = subprocess.run([exe, '-NoProfile', '-NonInteractive',
                                     '-Command', command],
                                    capture_output=True, encoding='utf-8', errors='replace',
                                    creationflags=flags)
        except OSError as exc:
            problems.append('%s：%s' % (exe, exc))
            continue
        if result.returncode == 0 and os.path.isfile(link):
            fileassoc.notify_shell()      # 让资源管理器/桌面立刻显示出来
            return link
        why = (result.stderr or result.stdout or '').strip() or ('退出码 %s' % result.returncode)
        if result.returncode == 0:
            why = '命令跑完了但没生成 %s' % os.path.basename(link)
        problems.append('%s：%s' % (exe, why))
    raise RuntimeError('创建快捷方式失败：%s' % '；'.join(problems))


def powershell_paths():
    """先按 PATH 上的 powershell，再按 %SystemRoot% 拼绝对路径（PATH 不认时兜底）。"""
    paths = ['powershell']
    root = os.environ.get('SystemRoot') or os.environ.get('WINDIR')
    if root:
        full = os.path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        if os.path.isfile(full):
            paths.append(full)
    return paths


def make_uninstaller(install_dir, shortcuts, assoc=False):
    """
    把「怎么卸」写进安装目录里的 uninstall.json，紧挨着打包时就放进来的 uninstall.exe。

    exe 里是死逻辑（哪台机器上都是同一份），「装在哪儿 / 删哪些快捷方式 / 要不要
    取消 .mproj 关联」随安装变，所以由安装程序写到它旁边的 json 里；那两个版本各装
    各的目录、各有各的 json，同时装着也不会互相认错。
    assoc=True 表示装的时候关联过 .mproj，卸载时把注册表里那几项一起删掉 —— 那份
    删除逻辑就是 fileassoc.unregister()，跟程序里那个勾选框共用一套。
    """
    exe = os.path.join(install_dir, UNINSTALL_EXE)
    if not os.path.isfile(exe):
        raise RuntimeError('安装目录里没有 %s（卸载程序），卸载入口装不上' % UNINSTALL_EXE)
    config = {'title': APP_TITLE, 'dir': install_dir, 'exe': APP_EXE,
              'key': UNINSTALL_KEY, 'shortcuts': list(shortcuts), 'assoc': bool(assoc)}
    with open(os.path.join(install_dir, UNINSTALL_JSON), 'w', encoding='utf-8') as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    return exe


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
        self.options = options          # {'dir':…, 'desktop':bool, 'startmenu':bool,
                                        #  'online':bool, 'edition':…, 'manifest':…}
        self.report = report            # report(在干嘛, 已完成字节, 总字节)
        self.total_bytes = 0            # 在线安装：下载 + 解压的总字节（进度条按它走）
        self.offset_bytes = 0           # 当前阶段开始前已经完成的字节
        self.last_progress = None       # 最近一次报的 (已完成, 总字节)
        self.reader = None              # 在线安装：多片拼起来的那个文件对象

    def run(self):
        target = self.options['dir']
        self.report('准备安装目录…', 0, 1)
        os.makedirs(target, exist_ok=True)
        self._check_writable(target)

        if self.options.get('online'):
            payload = self._online_payload()          # 联网下分片，拼成 zip 流
        else:
            payload, _path = open_payload()           # 内嵌的 payload.zip
        try:
            with payload:
                self._check_payload(payload)
                self._extract(payload, target)
        finally:                          # zipfile 不会替我们关掉传进去的文件对象
            if self.reader is not None:
                self.reader.close()
                self.reader = None
        self._clean_old(target)

        self._stage('写说明文件…')
        self._write_readme(target)
        shortcuts = self._shortcuts(target)
        assoc = self._register_assoc(target) if self.options.get('assoc') else ''
        self._stage('写入卸载信息…')
        size_kb = self._dir_size(target) / 1024.0
        uninstaller = make_uninstaller(target, shortcuts, assoc=bool(assoc))
        register_uninstall(target, uninstaller, size_kb)
        self._stage('装好了')
        return target

    def _online_payload(self):
        """
        在线安装：先下分片，再把「多片拼起来的 zip」交给解压那一步。

        不拼出完整的大 zip 文件 —— PartsReader 直接从几个片里按需读，
        省磁盘也省一次全量拷贝。
        """
        manifest = self.options.get('manifest') or fetch_manifest()
        key = self.options.get('edition') or 'full'
        info = (manifest.get('editions') or {}).get(key) or {}
        download_total = sum(int(part['size']) for part in info.get('parts') or [])
        install_total = int(info.get('install_size') or 0) or int(info.get('size') or 0) * 3
        self.total_bytes = max(1, download_total + install_total)
        self.offset_bytes = 0
        downloader = PartsDownloader(manifest, key, self._download_report)
        reader = downloader.run()
        self.reader = reader
        self.offset_bytes = download_total
        self.report('下载完成，开始解压安装…', self.offset_bytes, self.total_bytes)
        return zipfile.ZipFile(reader)

    def _download_report(self, text, done, total):
        self.report(text, min(int(done), self.total_bytes), self.total_bytes)

    def _stage(self, text):
        """
        收尾阶段的提示（写说明 / 建快捷方式 / 写卸载信息 / 装好了）。

        让进度条停在刚才的位置 —— 在线安装就是 100%。以前这里固定报 (1, 1)，
        进度条走到头又缩回 0.1%，看着像出了错。
        """
        if self.total_bytes:
            self.report(text, self.total_bytes, self.total_bytes)
        else:
            self.report(text, *(self.last_progress or (1, 1)))

    def _tick(self, text, done, total):
        """解压阶段的进度：在线安装时接到下载阶段后面；离线安装就用原来的总数。"""
        if self.total_bytes:
            self.last_progress = (self.offset_bytes + done, self.total_bytes)
        else:
            self.last_progress = (done, total)
        self.report(text, *self.last_progress)

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
            self._stage('关联 .mproj 工程文件：双击就直接打开编辑器')
            return exe
        except Exception as exc:
            self._stage('关联 .mproj 没成功（不影响使用）：%s' % exc)
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
   或者点「曲库」—— 自带的 songs\\ 和联网曲库下载过的曲子都在那个列表里
   （上面有「内置曲库」「已下载」两个快捷入口）。
3. 点「开始演奏」，然后切回游戏。开始之前先点一下游戏窗口让它拿到焦点。

顶部那几个按钮
--------------
公告 / 更新 / B站主页 / GitHub。公告和更新是程序启动后自己去曲库仓库
（Gitee / GitHub）拉回来的，会各开一个独立窗口（能最小化、不置顶）。
有新版本的时候：这一版（v1.0.2 起）能**增量更新** —— 只下改动过的文件
（几十 MB，不用重下两百多 MB 的完整包），点「立即更新」就行；下好程序会自己
退出、由 AutoPlayUpdater.exe 换好文件再自己开回来，换到一半出问题会自动还原。
装的是更老的版本（没有更新器）就只能点「去下载」下完整安装包（会打开 B站主页，
下载链接发在那儿）。
B站主页和 GitHub 用系统默认浏览器打开，不在程序里嵌浏览器。
拉不到网络就安静地用上次那份（更新清单除外，它不缓存）。

窗口右上角那几个键
------------------
已就绪（状态胶囊）+ 主题下拉是圆角矩形；键位 / 最小化 / 最大化 / 关闭是直角方块。
· 最小化：正常最小化，任务栏里能找到；
· 关闭（×）：不退出程序，是收进**托盘**后台（任务栏不占位，演奏和热键照常）；
  要真退出就在托盘图标上右键 →「退出」；
· 勾掉界面上的「显示演奏状态」可以把右上角那颗状态胶囊藏起来（嫌它挡事时用）。

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

简谱编辑器（完全版才有）
------------------------
点「简谱编辑器…」会另开一个编辑器窗口（跟主界面分开的普通窗口：能最小化、不置顶），
把音轨铺成钢琴卷帘手动改：双击空白加音、右键音块删音、拖着改长短，拖右边缘改时值，
点一下两个音之间的空白就选中那段间隔、按 Delete 删掉（后面的音自动接上、不留新缝），
时间尺上方那个「方块 + 倒三角」按住拖动就能挪光标；空格 = 播放 / 暂停，
Ctrl+Z 撤销、Ctrl+S 存工程。改完点「导出 MIDI…」，
导出的文件会自动载回主程序。
没改完也能存成工程文件（.mproj）：装的时候勾了「关联文件类型」的话，以后在
资源管理器里双击 .mproj 就直接开程序并弹出编辑器窗口。程序已经开着的时候双击
工程文件，**不会**再起一个程序 —— 那个新进程会把文件交给已经在跑的这个，自己退出；
这边另开一个编辑器窗口打开它。

文件夹
------
songs\\        内置曲库，往里面扔 .mid 就会出现在「内置曲库」里
_internal\\    运行库，别删别改
AutoPlayUpdater.exe  增量更新时换文件用的小程序（平时不用管它）

谱面文件
--------
程序会在 midi 旁边生成 TONIC<主音> <曲名>.txt（简谱谱面）；
那个目录写不进去时会放到 %%LOCALAPPDATA%%\\AutoPlay\\scores\\。

出问题看日志
------------
%%LOCALAPPDATA%%\\AutoPlay\\AutoPlay.log —— 闪退、按键没反应，看它的最后几行。

卸载
----
设置 →「应用」里搜 AutoPlay，或者双击安装目录里的 uninstall.exe。
卸载只删程序本体和快捷方式，%%LOCALAPPDATA%%\\AutoPlay 里的设置 / 日志 / 录制 /
谱面都留着 —— 重新装回来还认得你。
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

    @staticmethod
    def _check_payload(payload):
        """先看一眼卸载程序在不在包里 —— 装到一半才发现缺东西最难受。"""
        if 'app/' + UNINSTALL_EXE not in set(payload.namelist()):
            raise RuntimeError('这个安装包里没有 %s（卸载程序），'
                               '请重新下载完整的安装包。' % UNINSTALL_EXE)

    @staticmethod
    def _clean_old(target):
        """
        老版本在安装目录里留过一份「卸载.bat」（当场生成的），现在换成打包带的
        uninstall.exe 了，升级安装时把那份旧的删掉 —— 留着会让人以为没换过来。
        删不掉（比如被占用）不是事，卸载的时候整个目录都会走。
        """
        stale = os.path.join(target, '卸载.bat')
        if os.path.isfile(stale):
            try:
                os.remove(stale)
            except OSError:
                pass

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
        # 注意：这里必须用循环变量 item，不能用外面那个 info（循环结束后它停在最后
        # 一个条目上，算出来的总量会小得离谱 —— 进度条就会乱跳）
        total = sum(item.file_size for item, _subs in entries) or 1
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
                    self._tick('正在安装：%s' % subs, done, total)
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
            self._stage('创建快捷方式：%s' % link)
            try:
                os.makedirs(os.path.dirname(link), exist_ok=True)
                make_shortcut(link, exe, target, icon, APP_TITLE)
                made.append(link)
                self._stage('快捷方式好了：%s' % link)
            except Exception as exc:      # 快捷方式建不上不该让整个安装失败
                self._stage('快捷方式没建成（%s）：%s' % (os.path.basename(link), exc))
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
    on_manifest = Signal(object)            # 在线安装清单（dict，或者 Exception）

    def __init__(self, online=None):
        super().__init__()
        # 没有内嵌程序本体 = 在线安装程序；也可以在命令行加 --online 强制试这一套
        self.online = (not has_embedded_payload()) if online is None else bool(online)
        self.manifest = None
        self.setWindowTitle('%s 安装程序 v%s' % (APP_TITLE, APP_VERSION))
        self.setMinimumWidth(620)
        self.setStyleSheet(STYLE)
        self.setWindowIcon(make_icon())
        self.installer = None
        self.target = ''
        self._build()
        self.on_progress.connect(self._show_progress)
        self.on_done.connect(self._show_result)
        self.on_manifest.connect(self._show_manifest)
        self.on_progress.emit('', 0, 1)
        self._load_payload_info()

    def _subtitle_text(self):
        tail = ('程序本体联网下载，支持断点续传' if self.online
                else '自带运行环境，目标电脑不需要装 Python，装完就能用')
        return 'v%s · %s · %s' % (APP_VERSION, EDITION_LABEL, tail)

    # ---- 界面 ----

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        self.title = QLabel('安装 %s' % APP_TITLE)
        self.title.setObjectName('title')
        root.addWidget(self.title)
        self.subtitle = QLabel(self._subtitle_text())
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

        self.edition_combo = None
        if self.online:                # 在线安装：装哪一版由用户在这儿选
            box.addWidget(self._label('安装版本'))
            self.edition_combo = QComboBox()
            self.edition_combo.addItem('完全版 · 全部功能（含音频转 MIDI、简谱编辑器）', 'full')
            self.edition_combo.addItem('精简版 · 不含音频转 MIDI 和编辑器，体积小', 'lite')
            self.edition_combo.currentIndexChanged.connect(self._edition_changed)
            box.addWidget(self.edition_combo)

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
                                      '双击它就直接开程序、弹出简谱编辑器窗口。\n'
                                      '只写在当前用户（HKCU）里，不需要管理员权限，卸载时会一起清掉。')
            box.addWidget(self.assoc_box)
        box.addWidget(self.run_box)

        self.space_label = QLabel('')
        self.space_label.setObjectName('value')
        self.space_label.setWordWrap(True)
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

    def _edition_changed(self, _index=None):
        """在线安装：换版本时界面上的名字 / 默认目录 / 快捷方式名都跟着换。"""
        key = self.edition_combo.currentData() if self.edition_combo else 'full'
        current = os.path.normpath(self.dir_edit.text().strip() or '')
        was_default = (not current) or current == os.path.normpath(DEFAULT_DIR)
        apply_edition(key)
        if was_default:                # 没手动改过目录就跟着换成那一版的默认目录
            self.dir_edit.setText(DEFAULT_DIR)
        self.title.setText('安装 %s' % APP_TITLE)
        self.subtitle.setText(self._subtitle_text())
        self.setWindowTitle('%s 安装程序 v%s' % (APP_TITLE, APP_VERSION))
        if self.assoc_box is not None:
            self.assoc_box.setVisible(HAS_ASSOC)    # 精简版没有编辑器，藏起来
        self._refresh_space()

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
        """看一眼程序本体有多大，顺便把「要多少空间 / 要下多少」显示出来。"""
        if self.online:
            self.payload_bytes = 0
            self.space_label.setText('正在拉在线安装清单…')
            threading.Thread(target=self._load_manifest, daemon=True).start()
            return
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

    def _load_manifest(self):
        """后台线程：拉在线安装清单（payload.json）。"""
        try:
            data = fetch_manifest()
        except Exception as exc:
            self.on_manifest.emit(exc)
            return
        self.on_manifest.emit(data)

    def _show_manifest(self, data):
        if isinstance(data, Exception):
            self.space_label.setText(
                '暂时拉不到在线安装清单，点「开始安装」会再试一次；'
                '实在连不上就改用百度网盘那份完整安装包。\n\n%s' % data)
            return
        self.manifest = data
        self._refresh_space()

    def _refresh_space(self):
        if self.online:
            self._refresh_online_space()
            return
        if not self.payload_bytes:
            return
        text = '需要 %s' % format_size(self.payload_bytes)
        free = free_space(self.dir_edit.text().strip() or DEFAULT_DIR)
        if free:
            text += ' · 这个盘还剩 %s' % format_size(free)
            if free < self.payload_bytes * 1.05:
                text += ' · 空间可能不够'
        self.space_label.setText(text)

    def _refresh_online_space(self):
        """在线安装：列出「要下多少、几片、安装后多大」，清单还没拉到时什么都不说。"""
        key = self.edition_combo.currentData() if self.edition_combo else 'full'
        info = ((self.manifest or {}).get('editions') or {}).get(key) or {}
        if not info:
            if self.manifest is not None:
                self.space_label.setText('这份在线清单里没有「%s」，换一个版本试试' % EDITION_LABEL)
            return
        size = int(info.get('size') or 0)
        install = int(info.get('install_size') or 0)
        text = '要下载 %s（%d 片）' % (format_size(size), len(info.get('parts') or []))
        if install:
            text += ' · 安装后约 %s' % format_size(install)
        free = free_space(self.dir_edit.text().strip() or DEFAULT_DIR)
        if free:
            text += ' · 这个盘还剩 %s' % format_size(free)
            if install and free < install * 1.05:
                text += ' · 空间可能不够'
        version = (self.manifest or {}).get('app_version')
        if version and version != APP_VERSION:
            text += ' · 清单里是最新的 v%s' % version
        text += '\n下好的分片会留着，断了重开接着下，不会重下。'
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
        options = {'dir': target,
                   'desktop': self.desktop_box.isChecked(),
                   'startmenu': self.startmenu_box.isChecked(),
                   'assoc': bool(self.assoc_box is not None and self.assoc_box.isChecked())}
        if self.online:
            options['online'] = True
            options['edition'] = (self.edition_combo.currentData()
                                  if self.edition_combo else 'full')
            options['manifest'] = self.manifest      # 可能还没拉到，后台线程会再试
            self.title.setText('正在下载并安装')
            self.progress_title.setText('正在下载程序本体…')
            self.subtitle.setText('中途关掉也没事，下次重开会接着下（断点续传）')
        else:
            self.title.setText('正在安装')
            self.subtitle.setText('别关这个窗口，装完会自己跳转')
        threading.Thread(target=self._install, args=(options,), daemon=True).start()

    def _install(self, options):
        try:
            if options.get('online') and not options.get('manifest'):
                options['manifest'] = fetch_manifest()   # 界面上没拉到，这儿再试一次
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
        link = os.path.join(desktop_dir(), SHORTCUT_NAME + '.lnk')
        if os.path.isfile(link):
            shortcut_line = '· 桌面上的「%s」已经放好了（%s）；\n' % (SHORTCUT_NAME, link)
        else:
            shortcut_line = ('· 桌面上没找到快捷方式。想手动放一个：右键 %s，'
                             '「发送到」→「桌面快捷方式」；\n' % os.path.join(self.target, APP_EXE))
        self.done_text.setText(
            '装到：%s\n\n'
            '%s'
            '· 双击它就能用（第一次运行会问一次管理员权限，因为要往游戏里发按键）；\n'
            '· 内置曲库在同目录的 songs 文件夹，默认那首是《鸟之诗》；\n'
            '· 想卸载：设置 →「应用」里搜 AutoPlay，或者双击目录里的 uninstall.exe。'
            % (self.target, shortcut_line))
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
    forced = '--online' in argv          # 调试用：源码直接跑 / 想看下载那套界面
    argv = [item for item in argv if item != '--online']
    online = forced or not has_embedded_payload()
    if online:
        apply_edition('full')            # 在线版：装哪一版由界面上的下拉框选
    else:
        apply_edition(read_edition())    # 离线版：完全版还是精简版，看内嵌的 payload
    app = QApplication(argv)
    app.setApplicationName(APP_TITLE + ' 安装程序')
    app.setStyle('Fusion')
    window = InstallerWindow(online=online)
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
