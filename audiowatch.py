# -*- coding: utf-8 -*-
"""
谁在响：看一眼是哪个程序在出声
================================

干嘛用的
--------
录制的时候要是蹦出一声「叮」，光靠耳朵是分不出谁干的：可能是系统提示音，
也可能是某个后台程序（输入法、游戏平台、录屏 / Overlay 之类）在响应按键。

这个模块干两件事：

1. **谁在响**：把系统里正在出声的会话（一个程序一条）列出来，带上进程名。
   界面上那个「谁在响」按钮就是拿它做的：听 10 秒，谁出声就写进日志。
2. **按住系统提示音**：录制时把「系统提示音」那一路按成静音，录完再放回去。
   只动系统那一路，别的程序（游戏、音乐）碰都不碰。

怎么做的
--------
WASAPI 的音频会话接口（IAudioSessionManager2 那一套）。这些都是 COM，
这里用 ctypes 直接按虚表调 —— 为了不额外拖 comtypes / pycaw 进来，
安装包本来就够大了。

注意：所有函数都不能抛异常出去。查不到会话是常事（比如设备正在被独占），
返回空列表让调用方该干嘛干嘛。
"""

import ctypes
import json
import os
import threading
import time

try:                                              # pragma: no cover
    from ctypes import wintypes
    _ole32 = ctypes.windll.ole32
    _kernel32 = ctypes.windll.kernel32
    _user32 = ctypes.windll.user32
    WINDOWS = True
except Exception:                                 # pragma: no cover
    WINDOWS = False

# 会话状态
STATE_INACTIVE, STATE_ACTIVE, STATE_EXPIRED = 0, 1, 2
STATE_TEXT = {STATE_INACTIVE: '不活动', STATE_ACTIVE: '正在响', STATE_EXPIRED: '已过期'}

# 「谁在响」默认听多久（秒）
LISTEN_SECONDS = 10.0
# 采样间隔：叮一声只有几百毫秒，太稀会漏
INTERVAL = 0.02


class GUID(ctypes.Structure):
    """COM 的 GUID。"""

    _fields_ = [('Data1', ctypes.c_ulong),
                ('Data2', ctypes.c_ushort),
                ('Data3', ctypes.c_ushort),
                ('Data4', ctypes.c_ubyte * 8)]

    def __init__(self, text):
        super().__init__()
        parts = text.strip('{}').split('-')
        self.Data1 = int(parts[0], 16)
        self.Data2 = int(parts[1], 16)
        self.Data3 = int(parts[2], 16)
        rest = parts[3] + parts[4]
        for index in range(8):
            self.Data4[index] = int(rest[index * 2:index * 2 + 2], 16)


CLSID_DEVICE_ENUMERATOR = '{BCDE0395-E52F-467C-8E3D-C4579291692E}'
IID_DEVICE_ENUMERATOR = '{A95664D2-9614-4F35-A746-DE8DB63617E6}'
IID_SESSION_MANAGER2 = '{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}'
IID_SESSION_CONTROL2 = '{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}'
IID_SIMPLE_VOLUME = '{87CE5498-68D6-44E5-9215-6DA47EF883D8}'

CLSCTX_ALL = 0x17
E_RENDER, E_CONSOLE = 0, 0


def _co_init():
    """当前线程先进 COM（进过了就算了）。"""
    if not WINDOWS:
        return
    try:
        _ole32.CoInitializeEx(None, 0x2)          # 单线程套间
    except Exception:                             # pragma: no cover
        pass


def _call(pointer, index, restype, *argtypes):
    """
    按虚表第 index 个方法调一次 —— COM 接口在 ctypes 里就是这么调的。

    先把接口指针当「虚表指针的指针」取出来，再从表里取出第 index 个函数地址，
    按签名包成一个可调用的东西。签名必须写全，不然 64 位下参数会被截断。
    """
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(table[index])


def _release(pointer):
    """Release；出错也不管（反正要退出这段作用域了）。"""
    if not pointer:
        return
    try:
        _call(pointer, 2, ctypes.c_ulong)(pointer)
    except Exception:                             # pragma: no cover
        pass


def _query(pointer, iid_text):
    """QueryInterface：拿到了就返回新指针，没有就返回 None。"""
    iid = GUID(iid_text)
    out = ctypes.c_void_p()
    try:
        hr = _call(pointer, 0, ctypes.c_long, ctypes.POINTER(GUID),
                   ctypes.POINTER(ctypes.c_void_p))(pointer, ctypes.byref(iid),
                                                    ctypes.byref(out))
    except Exception:                             # pragma: no cover
        return None
    return out if hr >= 0 and out else None


def available():
    """这台机器能不能查音频会话（只有 Windows 有这套接口）。"""
    return WINDOWS


def process_name(pid):
    """PID -> 进程名；查不到就给个 'pid 1234' 这种写法。"""
    if not WINDOWS or not pid:
        return '未知程序'
    handle = None
    try:
        _kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = _kernel32.OpenProcess(0x1000, False, int(pid))   # 只查信息
        if not handle:
            return 'pid %d' % pid
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if not _kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0,
                                                    buffer, ctypes.byref(size)):
            return 'pid %d' % pid
        return os.path.basename(buffer.value) or ('pid %d' % pid)
    except Exception:                             # pragma: no cover
        return 'pid %d' % pid
    finally:
        if handle:
            try:
                _kernel32.CloseHandle(ctypes.c_void_p(handle))
            except Exception:                     # pragma: no cover
                pass


def foreground():
    """现在最前面那个窗口是谁：{'class': 类名, 'pid': pid, 'name': 进程名}。"""
    info = {'class': '', 'pid': 0, 'name': ''}
    if not WINDOWS:
        return info
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return info
        buffer = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(ctypes.c_void_p(hwnd), buffer, 128)
        info['class'] = buffer.value
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        info['pid'] = int(pid.value)
        info['name'] = process_name(pid.value)
    except Exception:                             # pragma: no cover
        pass
    return info


# ============ 会话枚举 ============

def _manager():
    """默认播放设备上的会话管理器；拿不到就返回 None。"""
    _co_init()
    enumerator = ctypes.c_void_p()
    device = ctypes.c_void_p()
    manager = ctypes.c_void_p()
    try:
        clsid, iid = GUID(CLSID_DEVICE_ENUMERATOR), GUID(IID_DEVICE_ENUMERATOR)
        hr = _ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_ALL,
                                     ctypes.byref(iid), ctypes.byref(enumerator))
        if hr < 0 or not enumerator:
            return None, None
        hr = _call(enumerator, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int,
                   ctypes.POINTER(ctypes.c_void_p))(enumerator, E_RENDER, E_CONSOLE,
                                                    ctypes.byref(device))
        if hr < 0 or not device:
            return None, None
        iid = GUID(IID_SESSION_MANAGER2)
        hr = _call(device, 3, ctypes.c_long, ctypes.POINTER(GUID), ctypes.c_ulong,
                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            device, ctypes.byref(iid), CLSCTX_ALL, None, ctypes.byref(manager))
        if hr < 0 or not manager:
            return None, None
        return enumerator, device, manager
    except Exception:                             # pragma: no cover
        return None, None


def _each_session(manager, visit):
    """
    把每个会话交给 visit(控制指针)。

    visit 返回 False 就提前收工（比如「按住系统提示音」找到一个就够了）。
    """
    sessions_enum = ctypes.c_void_p()
    try:
        hr = _call(manager, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            manager, ctypes.byref(sessions_enum))
        if hr < 0 or not sessions_enum:
            return
        count = ctypes.c_int()
        if _call(sessions_enum, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                sessions_enum, ctypes.byref(count)) < 0:
            return
        for index in range(max(count.value, 0)):
            control = ctypes.c_void_p()
            if _call(sessions_enum, 4, ctypes.c_long, ctypes.c_int,
                     ctypes.POINTER(ctypes.c_void_p))(sessions_enum, index,
                                                      ctypes.byref(control)) < 0:
                continue
            try:
                if visit(control) is False:
                    return
            finally:
                _release(control)
    except Exception:                             # pragma: no cover
        pass
    finally:
        _release(sessions_enum)


def _info_of(control):
    """一个会话：状态、属于哪个进程、是不是「系统提示音」那一路。"""
    state = ctypes.c_int(STATE_INACTIVE)
    try:
        _call(control, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
            control, ctypes.byref(state))
    except Exception:                             # pragma: no cover
        pass
    pid, system = 0, False
    control2 = _query(control, IID_SESSION_CONTROL2)
    if control2:
        try:
            value = ctypes.c_ulong()
            if _call(control2, 14, ctypes.c_long, ctypes.POINTER(ctypes.c_ulong))(
                    control2, ctypes.byref(value)) >= 0:
                pid = int(value.value)
            # IsSystemSoundsSession：返回 S_OK(0) 就是系统提示音那一路
            system = _call(control2, 15, ctypes.c_long)(control2) == 0
        except Exception:                         # pragma: no cover
            pass
        finally:
            _release(control2)
    return {'state': int(state.value), 'pid': pid, 'system': system,
            'name': '(系统提示音)' if system else process_name(pid)}


def sessions():
    """现在有多少路会话在：列表，每项 {name, pid, state, system}。"""
    found = []
    if not WINDOWS:
        return found
    enumerator, device, manager = _manager()
    if not manager:
        return found
    try:
        _each_session(manager, lambda control: found.append(_info_of(control)))
    finally:
        _release(manager)
        _release(device)
        _release(enumerator)
    return found


def _set_system_mute(muted):
    """
    「系统提示音」那一路静音 / 放开。返回 (成不成, 之前是不是静音)。

    只认 IsSystemSoundsSession() 那一路 —— 游戏、音乐都有各自的会话，动不着。
    """
    if not WINDOWS:
        return False, None
    result = {'ok': False, 'before': None}

    def visit(control):
        control2 = _query(control, IID_SESSION_CONTROL2)
        if not control2:
            return None
        try:
            if _call(control2, 15, ctypes.c_long)(control2) != 0:
                return None                       # 不是系统那一路
            volume = _query(control2, IID_SIMPLE_VOLUME)
            if not volume:
                return None
            try:
                current = ctypes.c_int()
                if _call(volume, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                        volume, ctypes.byref(current)) < 0:
                    return None
                result['before'] = bool(current.value)
                hr = _call(volume, 5, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)(
                    volume, 1 if muted else 0, None)
                result['ok'] = hr >= 0
            finally:
                _release(volume)
            return False                          # 找到了，不用再翻别的会话
        finally:
            _release(control2)

    enumerator, device, manager = _manager()
    if not manager:
        return False, None
    try:
        _each_session(manager, visit)
    finally:
        _release(manager)
        _release(device)
        _release(enumerator)
    return result['ok'], result['before']


def system_muted():
    """「系统提示音」那一路现在是静音吗（查不到给 None）。"""
    if not WINDOWS:
        return None
    state = {'value': None}

    def visit(control):
        control2 = _query(control, IID_SESSION_CONTROL2)
        if not control2:
            return None
        try:
            if _call(control2, 15, ctypes.c_long)(control2) != 0:
                return None
            volume = _query(control2, IID_SIMPLE_VOLUME)
            if not volume:
                return None
            try:
                current = ctypes.c_int()
                if _call(volume, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                        volume, ctypes.byref(current)) >= 0:
                    state['value'] = bool(current.value)
            finally:
                _release(volume)
            return False
        finally:
            _release(control2)

    enumerator, device, manager = _manager()
    if not manager:
        return None
    try:
        _each_session(manager, visit)
    finally:
        _release(manager)
        _release(device)
        _release(enumerator)
    return state['value']


# ============ 录制时按住系统提示音 ============

_MARKER = None          # 记着「我们把它按住了」：上次要是崩了，下次开机放回去


def marker_path():
    """小纸条放哪儿：跟自动保存一个目录（见 autosave.py）。"""
    global _MARKER
    if _MARKER is None:
        try:
            import autosave
            _MARKER = os.path.join(autosave.DIR, 'system-sound.hold')
        except Exception:                         # pragma: no cover
            _MARKER = os.path.join(os.path.expanduser('~'), '.autoplay-system-sound.hold')
    return _MARKER


def _write_marker(before):
    try:
        path = marker_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'before': bool(before)}, handle)
    except Exception:                             # pragma: no cover
        pass


def _drop_marker():
    try:
        os.remove(marker_path())
    except Exception:
        pass


def hold_system_sounds():
    """
    录制开始：把系统提示音按住。返回「凭据」（之前是不是静音的），供 release 用。

    按不动（查不到那一路，或者本来就被人为静音了）就返回 None。
    """
    ok, before = _set_system_mute(True)
    if not ok:
        return None
    if before:
        # 本来就是静音的，等于什么也没做 —— 别写小纸条，免得回头给它放开了
        return {'already': True}
    _write_marker(False)
    return {'already': False}


def release_system_sounds(token):
    """录制结束：把系统提示音放回去（本来静音的就不动）。"""
    if not token:
        return
    _drop_marker()
    if token.get('already'):
        return
    _set_system_mute(False)


def recover_hold():
    """
    开机第一件事：上次是不是崩在录制里、把系统提示音按住了？

    是的话放回去，让小纸条别过夜。返回「放回去了吗」。
    """
    try:
        path = marker_path()
        if not os.path.isfile(path):
            return False
        with open(path, 'r', encoding='utf-8') as handle:
            before = bool(json.load(handle).get('before', False))
    except Exception:
        _drop_marker()
        return False
    _set_system_mute(bool(before))
    _drop_marker()
    return True


# ============ 听一会儿：谁在响 ============

class Watcher:
    """
    听 seconds 秒：哪一路会话从「不活动」变成「正在响」就报一次。

    跑在后台线程里（on_sound 会在那个线程被调用，界面那边自己转信号）。
    """

    def __init__(self, on_sound=None, on_done=None, log=None,
                 seconds=LISTEN_SECONDS, interval=INTERVAL):
        self.on_sound = on_sound
        self.on_done = on_done
        self.log = log
        self.seconds = float(seconds)
        self.interval = float(interval)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._heard = []            # 听出来的：[(进程名, 是不是系统提示音, 几秒)]

    # ---------- 对外 ----------

    @property
    def running(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def heard(self):
        """这次听出来的东西（给界面收尾时汇报用）。"""
        with self._lock:
            return list(self._heard)

    def start(self):
        """开听；已经在听就返回 False。"""
        if self.running:
            return False
        self._stop.clear()
        with self._lock:
            self._heard = []
        self._thread = threading.Thread(target=self._run, name='谁在响', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """别听了。"""
        self._stop.set()

    # ---------- 线程内部 ----------

    def _say(self, message):
        if self.log:
            try:
                self.log(message)
            except Exception:
                pass

    def _report(self, info):
        with self._lock:
            self._heard.append((info['name'], info['system'], time.perf_counter()))
        if self.on_sound is not None:
            try:
                self.on_sound(info)
            except Exception:
                pass

    def _run(self):
        try:
            known = {}
            for row in sessions():
                known[(row['pid'], row['system'])] = row['state']
            deadline = time.perf_counter() + self.seconds
            while not self._stop.is_set() and time.perf_counter() < deadline:
                for row in sessions():
                    key = (row['pid'], row['system'])
                    was = known.get(key, STATE_INACTIVE)
                    known[key] = row['state']
                    if row['state'] == STATE_ACTIVE and was != STATE_ACTIVE:
                        self._report(row)
                time.sleep(self.interval)
        except BaseException as exc:              # pragma: no cover
            self._say('「谁在响」听的时候出错了：%r' % exc)
        finally:
            if self.on_done is not None:
                try:
                    self.on_done(self.heard())
                except Exception:
                    pass