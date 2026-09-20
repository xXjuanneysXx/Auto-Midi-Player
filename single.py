# -*- coding: utf-8 -*-
"""
单实例：双击工程文件时把它交给已经在跑的那个程序
================================================

双击 `.mproj` 的时候，Windows 照注册表里的命令起一个**新进程**。要是程序本来就开着，
再起一个就变成两个一模一样的窗口，托盘图标、全局热键、设置文件全在打架。所以：

* 新进程先找有没有「同一个 exe」已经在跑；
* 找到了就把「要打开哪个文件」用 `WM_COPYDATA` 塞给它，自己立刻退出；
* 收到消息的那个进程**新开一个编辑器窗口**打开这个工程（不是替换当前的，也不是把
  主窗口顶到前面 —— 用户双击的是那个工程，就给他一个属于那个工程的窗口）。

怎么找「同一个程序」
--------------------
不看窗口标题 —— 标题会随载入的曲子变（`MIDI 简谱自动演奏 - 鸟之诗.mid`），覆盖模式下
窗口还会被整个重建。也不在窗口上挂标记属性（重建就没了）。这里认的是**进程的可执行
文件路径**：枚举顶层窗口 -> 问出它属于哪个进程 -> `QueryFullProcessImageNameW` 拿到那个
进程的 exe 全路径 -> 跟自己的比。完全版和精简版是两个 exe，正好各认各的。

光找到还不够：对面可能是个不认这个消息的窗口（Qt 自己也会建一些隐藏的顶层窗口）。
所以发的时候挨个试，谁回了「我处理了」（`WM_COPYDATA` 的返回值）才算交接成功；
全都没反应就当作「没有别的实例」，这个进程照常自己开窗口。

只有打包成 exe 之后才走这一套（`enabled()`）：源码运行时大家的 exe 都是 python.exe，
按可执行文件认会把两个不相干的程序认成同一个。
"""

import ctypes
import os
import sys

try:
    from ctypes import wintypes
except Exception:                                 # pragma: no cover
    wintypes = None


WM_COPYDATA = 0x004A
SMTO_ABORTIFHUNG = 0x0002
MESSAGE_TIMEOUT = 1500        # 毫秒：对面卡住就别等了
PAYLOAD_MAGIC = 0x41504C59    # 'APLY'：万一收消息的窗口根本不认识，也好认出不是我们的


# Windows 原生消息结构：nativeEvent 递过来的那块内存要按它解释（拿 lParam）
MSG = getattr(wintypes, 'MSG', None)


class COPYDATASTRUCT(ctypes.Structure):
    _fields_ = [('dwData', ctypes.c_void_p),
                ('cbData', ctypes.c_ulong),
                ('lpData', ctypes.c_void_p)]


def enabled():
    """
    这一套要不要生效。

    默认只有「打包成 exe」才生效；源码运行时两个进程的 exe 都是 python.exe，
    按可执行文件认人一定会认错。测试会把这里换成 True（那会儿只认当前这个 python）。
    """
    return bool(getattr(sys, 'frozen', False))


def own_image():
    """本进程的可执行文件全路径（拿不到就返回空字符串）。"""
    try:
        return os.path.normcase(os.path.abspath(sys.executable))
    except Exception:                             # pragma: no cover
        return ''


def make_payload(path):
    """要说的话：magic + utf-16 的文件路径。"""
    return ctypes.c_ulong(PAYLOAD_MAGIC).value.to_bytes(4, 'little') + str(path or '').encode('utf-16-le')


def message_of(address):
    """nativeEvent 给的那个地址 -> MSG；不是 Windows 消息（或者系统没给）就返回 None。"""
    if MSG is None or not address:
        return None
    try:
        return ctypes.cast(ctypes.c_void_p(address), ctypes.POINTER(MSG)).contents
    except Exception:                             # pragma: no cover
        return None


def read_payload(address):
    """
    收到的那块内存 -> 文件路径。

    address 是 `COPYDATASTRUCT` 的地址（WM_COPYDATA 的 lParam）；不是我们的
    magic、或者一块空消息，就返回空字符串。
    """
    if not address:
        return ''
    info = ctypes.cast(ctypes.c_void_p(address), ctypes.POINTER(COPYDATASTRUCT)).contents
    if not info.lpData or int(info.cbData) < 4:
        return ''
    blob = ctypes.string_at(ctypes.c_void_p(info.lpData), int(info.cbData))
    if int.from_bytes(blob[:4], 'little') != PAYLOAD_MAGIC:
        return ''
    return blob[4:].decode('utf-16-le', 'replace')


def _image_of(hwnd, user32, kernel32):
    """这个窗口是哪个 exe 开出来的（同一个进程、或问不出来就返回空字符串）。"""
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    if not pid.value or pid.value == os.getpid():
        return ''
    handle = kernel32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return ''
    try:
        size = ctypes.c_ulong(1024)
        name = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return os.path.normcase(name.value)
        return ''
    except Exception:                             # pragma: no cover
        return ''
    finally:
        kernel32.CloseHandle(handle)


def candidates():
    """所有「跟我们同一个 exe」的顶层窗口句柄（别人的、自己的都不要）。"""
    ours = own_image()
    if not ours:
        return []
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _param):
        if user32.GetWindow(ctypes.c_void_p(hwnd), 4):        # GW_OWNER：有爹的不算主窗口
            return True
        if _image_of(hwnd, user32, kernel32) == ours:
            found.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(visit, None)
    except Exception:                             # pragma: no cover
        return found
    return found


def send(path=''):
    """
    把「打开这个文件」交给已经在跑的那个程序。

    返回 True 表示**有人接手了**，这个进程该退出了；False 就是没人在跑（或者那边
    不理我们），照常开自己的窗口。
    """
    if not enabled():
        return False
    payload = make_payload(path)
    buffer = ctypes.create_string_buffer(payload, len(payload))
    info = COPYDATASTRUCT(ctypes.c_void_p(PAYLOAD_MAGIC), len(payload),
                          ctypes.cast(buffer, ctypes.c_void_p))
    user32 = ctypes.windll.user32
    user32.SendMessageTimeoutW.restype = ctypes.c_void_p
    user32.SendMessageTimeoutW.argtypes = (ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_uint, ctypes.c_uint,
                                           ctypes.POINTER(ctypes.c_void_p))
    result = ctypes.c_void_p()
    for hwnd in candidates():
        result.value = 0
        try:
            user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), WM_COPYDATA, None,
                                       ctypes.byref(info), SMTO_ABORTIFHUNG,
                                       MESSAGE_TIMEOUT, ctypes.byref(result))
        except Exception:                         # pragma: no cover
            continue
        if result.value:                          # 对面说「我处理了」
            return True
    return False


def handoff(path=''):
    """有别的实例在跑就交给它（没在跑就返回 False，自己接着启动）。"""
    return send(path)