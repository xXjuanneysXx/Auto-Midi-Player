# -*- coding: utf-8 -*-
"""
全局热键：F6 开始 / F7 暂停或继续 / F8 停止 / F10 录制 / Ctrl+F1 唤起主界面 / Ctrl+F2 开关跟奏
=================================================================================

默认键位写在 DEFAULT_BINDINGS 里，界面上可以改成别的（HotkeyMap 就是那张表）。
能绑的键由 VK_NAMES 决定：字母、数字、F1-F24、小键盘、方向键、空格这些常见键。

为什么要自己再写一套
--------------------
keyboard 库的 add_hotkey 里面是「低级钩子线程 + 队列 + 处理线程 + 热键状态机」
好几层。演奏的时候程序一边不停发按键（pydirectinput 的 SendInput 事件同样会
经过低级钩子）、一边切前台后台，这套状态机偶尔会卡住，表现就是 F7 / F8 突然
失灵 —— 在游戏里演奏到一半遇到这个最难受。

所以这里做两层保险，任意一层活着热键就还能用：

1. HotkeyHook：自己用 ctypes 装一个 WH_KEYBOARD_LL 低级键盘钩子，独立线程跑
   消息循环；回调里只做「认键名 -> 去重 -> 通知」，非常短，不依赖 keyboard 库；
   钩子被系统摘掉（回调超时）或线程挂掉会自动重装。
2. keyboard 库的 add_hotkey 继续保留一份，注册在启动时，摘除用的句柄留着，
   退出时一起清掉；它要是哪天不灵了也不影响第 1 条。

两条路都接到同一个 Dispatcher，按动作做 250 毫秒去重，所以一次按键只响应一次。
界面上隔几秒调一次 HotkeyListener.refresh()，钩子被系统摘掉时能自己装回来。
"""

import ctypes
import threading
import time

try:
    from ctypes import wintypes
except Exception:                                   # pragma: no cover
    wintypes = None


# 动作名 -> 默认组合键。界面上改成别的之后会存进 QSettings，下次启动读回来。
DEFAULT_BINDINGS = {'start': 'f6', 'pause': 'f7', 'stop': 'f8',
                    'record': 'f10', 'show': 'ctrl+f1', 'follow': 'ctrl+f2'}

# 界面上按这个顺序列出各个动作
ACTION_ORDER = ('start', 'pause', 'stop', 'record', 'show', 'follow')
ACTION_LABELS = {'start': '开始演奏', 'pause': '暂停 / 继续', 'stop': '停止演奏',
                 'record': '录制（弹一段记成谱）',
                 'show': '唤起主界面', 'follow': '开关跟奏模式'}

# 修饰键：组合键名字里按这个顺序拼
MOD_VK = {'ctrl': 0x11, 'shift': 0x10, 'alt': 0x12}
MOD_ORDER = ('ctrl', 'shift', 'alt')

# 带空格 / 容易写丑的键名，界面上换个好看的写法
_PRETTY = {'esc': 'Esc', 'page up': 'PageUp', 'page down': 'PageDown',
           'caps lock': 'CapsLock', 'num lock': 'NumLock', 'scroll lock': 'ScrollLock',
           'print screen': 'PrintScreen', 'left windows': 'Win', 'right windows': 'Win'}


def _vk_table():
    """
    虚拟键码 -> 键名。**能绑的键全在这张表里**，表外的键钩子直接放过。

    修饰键（Ctrl / Shift / Alt / Win）故意不进去：它们只能当组合键的前缀。
    """
    table = {}
    for code in range(0x41, 0x5B):                       # A-Z
        table[code] = chr(code).lower()
    for code in range(0x30, 0x3A):                       # 0-9
        table[code] = chr(code)
    for i in range(24):                                  # F1-F24
        table[0x70 + i] = 'f%d' % (i + 1)
    for code in range(0x60, 0x6A):                       # 小键盘 0-9
        table[code] = 'num %d' % (code - 0x60)
    table.update({
        0x08: 'backspace', 0x09: 'tab', 0x0D: 'enter', 0x13: 'pause', 0x14: 'caps lock',
        0x1B: 'esc', 0x20: 'space', 0x21: 'page up', 0x22: 'page down', 0x23: 'end',
        0x24: 'home', 0x25: 'left', 0x26: 'up', 0x27: 'right', 0x28: 'down',
        0x2C: 'print screen', 0x2D: 'insert', 0x2E: 'delete', 0x5D: 'menu',
        0x90: 'num lock', 0x91: 'scroll lock',
        0x6A: 'num *', 0x6B: 'num +', 0x6D: 'num -', 0x6E: 'num .', 0x6F: 'num /',
        0xBA: ';', 0xBB: '=', 0xBC: ',', 0xBD: '-', 0xBE: '.', 0xBF: '/', 0xC0: '`',
        0xDB: '[', 0xDC: '\\', 0xDD: ']', 0xDE: "'",
    })
    return table


VK_NAMES = _vk_table()
KEY_NAMES = set(VK_NAMES.values())


def normalize_combo(combo):
    """把「Ctrl + Shift + A」统一成 'ctrl+shift+a'；认不出来就返回空串。"""
    parts = [p.strip().lower() for p in str(combo).split('+') if p.strip()]
    mods = [m for m in MOD_ORDER if m in parts]
    keys = [p for p in parts if p not in MOD_ORDER]
    if len(keys) != 1 or keys[0] not in KEY_NAMES:
        return ''
    return '+'.join(mods + keys)


def pretty_combo(combo):
    """'ctrl+f1' -> 'Ctrl+F1'，界面上显示用。"""
    out = []
    for part in str(combo).split('+'):
        if part in MOD_VK:
            out.append(part.capitalize())
        elif part in _PRETTY:
            out.append(_PRETTY[part])
        elif len(part) <= 2:
            out.append(part.upper())
        else:
            out.append(part.title())
    return '+'.join(out)


class HotkeyMap:
    """动作 <-> 组合键的对照表。运行时可以随便改，改完存起来就是新的键位。"""

    def __init__(self, bindings=None):
        self._bindings = dict(DEFAULT_BINDINGS)
        if bindings:
            self.replace(bindings)

    # ---------- 查询 ----------

    def combo_of(self, action):
        return self._bindings.get(action, '')

    def action_for(self, combo):
        """组合键 -> 动作名；没人绑这个键就返回 None。"""
        combo = normalize_combo(combo)
        if not combo:
            return None
        for action, key in self._bindings.items():
            if key == combo:
                return action
        return None

    def used_by(self, combo, skip=None):
        """这个键已经被哪个动作占了（skip 用来跳过自己那一行）。"""
        action = self.action_for(combo)
        return None if action == skip else action

    def items(self):
        """[(动作名, 组合键)]，按界面上的顺序，没绑的跳过。"""
        return [(a, self._bindings[a]) for a in ACTION_ORDER if self._bindings.get(a)]

    # ---------- 修改 ----------

    def set(self, action, combo):
        """给某个动作换键位；combo 认不出来就不动，返回 False。"""
        combo = normalize_combo(combo)
        if action not in self._bindings or not combo:
            return False
        self._bindings[action] = combo
        return True

    def replace(self, bindings):
        """整表替换：先回默认，再把 bindings 里认得的盖上去。"""
        self._bindings = dict(DEFAULT_BINDINGS)
        for action, combo in dict(bindings or {}).items():
            self.set(action, combo)

    def reset(self, action=None):
        """恢复默认；不传动作就全部恢复。"""
        if action is None:
            self._bindings = dict(DEFAULT_BINDINGS)
        elif action in DEFAULT_BINDINGS:
            self._bindings[action] = DEFAULT_BINDINGS[action]

    def copy(self):
        return HotkeyMap(self._bindings)

DEBOUNCE = 0.25          # 同一个动作多久之内只响应一次（秒）

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_TIMER = 0x0113
WM_QUIT = 0x0012
REINSTALL_MS = 20000     # 每隔这么久把钩子重新装一遍，自愈用


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('vkCode', wintypes.DWORD),
        ('scanCode', wintypes.DWORD),
        ('flags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('dwExtraInfo', ctypes.c_void_p),
    ]


def _say(log, message):
    if log:
        try:
            log(message)
        except Exception:
            pass


class Dispatcher:
    """把键名翻成动作，并保证一次按键只响应一次。"""

    def __init__(self, on_action=None, log=None, keys=None):
        self.on_action = on_action
        self.log = log
        self.keys = keys if keys is not None else HotkeyMap()
        self._lock = threading.Lock()
        self._last = {}
        self.enabled = True          # 改键位的时候先关掉，免得手指一按旧热键就触发

    def fire(self, name):
        if not self.enabled:
            return False
        # 先按当前键位表查；查不到再看传进来的本身是不是动作名
        # （界面按钮 / 托盘菜单是直接发动作名的）
        action = self.keys.action_for(name)
        if action is None and name in DEFAULT_BINDINGS:
            action = name
        if action is None:
            return False
        now = time.perf_counter()
        with self._lock:
            if now - self._last.get(action, -99.0) < DEBOUNCE:
                return False
            self._last[action] = now
        if self.on_action is not None:
            try:
                self.on_action(action)
            except Exception as exc:
                _say(self.log, '热键处理出错（%s）：%s' % (action, exc))
        return True


class HotkeyHook:
    """自己装的低级键盘钩子，跑在独立线程的消息循环里。"""

    def __init__(self, on_key, log=None):
        self.on_key = on_key
        self.log = log
        self._thread = None
        self._thread_id = 0
        self._hook = None
        self._proc = None           # 一定要留着引用，不然会被 GC 掉
        self._stop = threading.Event()

    # ---------- 生命周期 ----------

    def start(self):
        if self.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='hotkey-hook', daemon=True)
        self._thread.start()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def ensure_alive(self):
        """线程死了就重新拉一个（自愈）。"""
        if not self.is_alive():
            _say(self.log, '热键钩子线程不在了，重新装一个…')
            self.start()

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        thread_id, self._thread_id = self._thread_id, 0
        if thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    # ---------- 线程内部 ----------

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                       ctypes.c_size_t, ctypes.c_ssize_t)
        self._proc = proc_type(self._callback)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, proc_type, ctypes.c_void_p, ctypes.c_uint]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_size_t, ctypes.c_ssize_t]
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        self._install()
        try:
            user32.SetTimer(None, 1, REINSTALL_MS, None)      # 到点自动重装
            message = wintypes.MSG()
            while not self._stop.is_set():
                got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if got <= 0:
                    break
                if message.message == WM_TIMER:
                    self._install()
                    continue
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:                            # pragma: no cover
            _say(self.log, '热键钩子的消息循环出错了：%r' % exc)
        finally:
            self._uninstall()
            self._thread_id = 0

    def _install(self):
        user32 = ctypes.windll.user32
        self._uninstall()
        try:
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        except Exception as exc:                                # pragma: no cover
            self._hook = None
            _say(self.log, '装热键钩子失败：%r' % exc)
        if not self._hook:
            code = ctypes.windll.kernel32.GetLastError()
            _say(self.log, '装热键钩子失败（错误码 %s），试试用管理员身份运行' % code)

    def _uninstall(self):
        if self._hook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None

    def _callback(self, code, wparam, lparam):
        if code == 0:
            try:
                if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    data = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                    name = VK_NAMES.get(data.vkCode)
                    if name:
                        # 绑没绑这个键交给 Dispatcher 判断，钩子里只认键名
                        self.on_key(self._combo(name))
            except BaseException:                               # 回调里绝不能抛异常
                pass
        return ctypes.windll.user32.CallNextHookEx(None, code, wparam, lparam)

    @staticmethod
    def _combo(name):
        """按当前按住的修饰键拼出组合键名，例如 Ctrl+F1 -> 'ctrl+f1'。"""
        user32 = ctypes.windll.user32
        mods = [mod for mod in MOD_ORDER if user32.GetAsyncKeyState(MOD_VK[mod]) & 0x8000]
        return '+'.join(mods + [name]) if mods else name


class HotkeyListener:
    """对外的门面：自己装的钩子 + keyboard 库的热键，一起去重。"""

    def __init__(self, on_action=None, log=None, keys=None):
        self.log = log
        self.keys = keys if keys is not None else HotkeyMap()
        self.dispatcher = Dispatcher(on_action, log, self.keys)
        self.hook = HotkeyHook(self.dispatcher.fire, log)
        self._keyboard = None
        self._removers = []

    def start(self):
        self.hook.start()
        self.install_keyboard_hotkeys()

    def install_keyboard_hotkeys(self):
        """用 keyboard 库也注册一份热键（第二层保险），退出时按句柄摘掉。"""
        try:
            import keyboard
        except Exception as exc:
            _say(self.log, '没能加载 keyboard 模块，只用自带的钩子：%s' % exc)
            return
        self._keyboard = keyboard
        for action, key in self.keys.items():
            try:
                remover = keyboard.add_hotkey(key, lambda a=action: self.dispatcher.fire(a))
                self._removers.append(remover)
            except Exception as exc:
                _say(self.log, '注册 %s 热键失败：%s' % (pretty_combo(key), exc))

    def rebind(self):
        """
        换了键位：把 keyboard 库那份重新注册一遍。

        自己装的钩子不用动 —— 它每次都拿当前表现查，改了就立刻生效。
        """
        self._remove_keyboard_hotkeys()
        self.install_keyboard_hotkeys()

    def _remove_keyboard_hotkeys(self):
        for remover in self._removers:
            try:
                self._keyboard.remove_hotkey(remover)
            except Exception:
                try:
                    remover()
                except Exception:
                    pass
        self._removers = []

    def refresh(self):
        """界面定时器里隔几秒调一次，只做自愈：线程死了 / 钩子掉了就重装。"""
        self.hook.ensure_alive()

    def stop(self):
        self.hook.stop()
        self._remove_keyboard_hotkeys()
