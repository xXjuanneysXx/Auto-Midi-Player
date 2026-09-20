# -*- coding: utf-8 -*-
"""
录制：把你弹的东西记成谱面
==========================

按 F10 开始，再按一下 F10 结束。这期间你按下的**每一个游戏键**
（z x c v b n m , 这八个）都会被记下来：什么时候按下去、什么时候松开、
按下去的那一刻鼠标的哪个键正按着（右键 = 升调 A、左键 = 降调 B、
中键 = 升半音 #，跟自动演奏时的规矩完全一样）。

录完就是一串「记号 + 时长」，直接就是程序认的谱面格式（见 jianpu.py）：
可以写成 .txt，也可以写成 .mid，还能丢进简谱编辑器继续改。

只认这八个键
------------
别的键一律放过：跑步的 WASD、程序自己的热键（F6 / F7 / F8 / F10 / Ctrl+F1…）
都不会混进录音里。所以录制期间你可以一边跑一边弹，录下来的只有你弹的音。

吃掉的那一下去哪儿了
--------------------
桌面（或者本程序自己的窗口）在前台时，这八个键送给系统毫无意义 —— 输入法会弹候选框，
系统和桌面接住按键还可能「叮」一声。所以录制时它们会被钩子吃掉；吃掉之后还会把这一下
投给一个**收键窗口**（消息专用窗口，不显示、不抢焦点）：按键有个正经去处，
但什么也不做。

顺便记一笔前台是谁
------------------
录制期间前台窗口一换（比如从桌面切到游戏），日志里记一行「前台是 xx」——
回头出了问题，一眼就能看出按键当时是被吃掉了还是送给了游戏。

单音是硬要求
------------
谱面同一时刻只能有一个音（游戏里就一双手）。要是真按出了和弦，或者上一个音
忘了松手，后面的音会把前面的截断；被截到只剩 MIN_DUR 的会记个数，录完在日志
里告诉你 —— 想修就去编辑器里拖一下。

时间轴怎么来的
--------------
第一个音按下去算 0 秒，两个音中间的空档写成休止符 `0`。所以录出来的节奏就是
**你弹的节奏**，跟原曲多快没关系；想改成原速，用编辑器里的时间轴调。
"""

import ctypes
import threading
import time

try:                                            # pragma: no cover
    from ctypes import wintypes
except Exception:                               # pragma: no cover
    wintypes = None

import jianpu

try:                                # 查音频会话 / 进程名（只有 Windows 有）
    import audiowatch
except Exception:                   # pragma: no cover
    audiowatch = None


# ============ Win32 那点东西 ============

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012

WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEMOVE = 0x0200

# 游戏里那八个琴键：字符 -> 简谱数字（正好是 jianpu.DIGIT_KEYS 反过来）
DIGIT_BY_KEY = {}
for _digit, _key in jianpu.DIGIT_KEYS.items():
    DIGIT_BY_KEY[_key] = _digit

# 字符 -> 虚拟键码。只列这八个键，表外的键钩子直接放过。
VK_BY_KEY = {'z': 0x5A, 'x': 0x58, 'c': 0x43, 'v': 0x56,
             'b': 0x42, 'n': 0x4E, 'm': 0x4D, ',': 0xBC}
KEY_BY_VK = {vk: key for key, vk in VK_BY_KEY.items()}

# 鼠标键 -> 谱面里的修饰字母（跟演奏时按的那三个键一一对应）
MOD_BY_MOUSE = {WM_LBUTTONDOWN: 'B', WM_MBUTTONDOWN: '#', WM_RBUTTONDOWN: 'A'}
MOUSE_DOWN = (WM_LBUTTONDOWN, WM_MBUTTONDOWN, WM_RBUTTONDOWN)
MOUSE_UP_BY_DOWN = {WM_LBUTTONUP: WM_LBUTTONDOWN,
                    WM_MBUTTONUP: WM_MBUTTONDOWN,
                    WM_RBUTTONUP: WM_RBUTTONDOWN}
MOUSE_UP = tuple(MOUSE_UP_BY_DOWN)

# 系统自己的窗口（桌面、任务栏、开始菜单、输入法…）的类名：
# 录制时前台是它们，或者干脆是本程序自己，游戏键就别往系统里送了
DESKTOP_CLASSES = ('Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd',
                   'SysListView32', 'TaskListThumbnailWnd', 'MultitaskingViewFrame',
                   'ForegroundStaging', 'XamlExplorerHostIslandWindow',
                   'IME', 'Default IME')
# 这几个进程的窗口全算系统自己的（桌面、任务栏、资源管理器）
SHELL_PROCESSES = ('explorer.exe',)
# 前台是谁隔这么久重新看一眼（钩子里不能磨蹭，也别每个键都查一次）
FOREGROUND_CACHE_SEC = 0.25

# 「收键窗口」：吃掉的那一下按键投给它（见 _sink_key）
HWND_MESSAGE = -3

# 一个音最短记这么久（秒），跟编辑器里那条线一致；再短就是按重叠了
MIN_DUR = 0.03
# 空档短于这个就当没有，不写休止符（免得谱面里一堆 0.001 的碎渣）
REST_MIN = 0.02

DEFAULT_BPM = 120.0


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('vkCode', ctypes.c_ulong),
        ('scanCode', ctypes.c_ulong),
        ('flags', ctypes.c_ulong),
        ('time', ctypes.c_ulong),
        ('dwExtraInfo', ctypes.c_void_p),
    ]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ('x', ctypes.c_long),
        ('y', ctypes.c_long),
        ('mouseData', ctypes.c_ulong),
        ('flags', ctypes.c_ulong),
        ('time', ctypes.c_ulong),
        ('dwExtraInfo', ctypes.c_void_p),
    ]


def token_of(digit, mods):
    """
    数字 + 按住的鼠标修饰键 -> 简谱记号。

    '1' + {'A'} -> 'A1'      '2' + {'#'} -> '#E2'      '7' + {'B', '#'} -> '#B7'

    修饰字母写在数字前面（升半音 # 排最前），跟 jianpu.parse_token 认的写法一致。
    """
    if 'A' in mods:                     # 升调（鼠标右键）
        prefix = 'A'
    elif 'B' in mods:                   # 降调（鼠标左键）
        prefix = 'B'
    else:
        prefix = 'E'
    return ('#' if '#' in mods else '') + prefix + digit


def to_pairs(spans):
    """
    [(记号, 按下秒, 松开秒)] -> ([(记号, 持续时间)], 被挤短的音数)

    谱面是「一个接一个」的，所以重叠的尾巴必须剪掉：后一个音一进来，前一个音立刻
    收。没重叠的地方原样保留 —— 你按多久就是多久。

    第一个音按下那一刻算 0 秒：按完 F10 走过去坐下、随手试两个音，这些前摇不会
    变成谱面开头的一长串休止符。
    """
    items = sorted(spans, key=lambda span: float(span[1]))
    pairs = []
    # 从第一个音按下去那一刻起算：F10 之后你走过去的那几秒静音不该写进谱面
    clock = float(items[0][1]) if items else 0.0
    squeezed = 0
    for index, (token, down, up) in enumerate(items):
        down, up = float(down), float(up)
        start = round(max(down, clock), 3)
        if start > clock + REST_MIN:
            pairs.append((jianpu.REST_TOKEN, round(start - clock, 3)))
        dur = max(up - down, MIN_DUR)
        if index + 1 < len(items):
            room = float(items[index + 1][1]) - start
            if room < dur - 1e-9:
                dur = max(room, MIN_DUR)
                squeezed += 1
        dur = round(dur, 3)
        pairs.append((token, dur))
        clock = start + dur
    return pairs, squeezed


def snap_pairs(pairs, snap_ms):
    """
    [(记号, 持续时间)] -> (吸附过的谱面, 被修过几个)

    「音长吸附」：把每个时值吸到最近的格子上 —— 按了 354 毫秒、格子上理论上该是
    350 毫秒，就记成 350。休止符（两个音之间的空档）也一起吸，不然光吸音长会把
    节奏切得七零八落。

    吸附之后时值不会变成 0：至少留一格。
    """
    step = float(snap_ms) / 1000.0
    if step <= 0:
        return list(pairs), 0
    out = []
    changed = 0
    for token, dur in pairs:
        dur = max(float(dur), 0.0)
        snapped = round(max(round(dur / step), 1) * step, 3)
        if abs(snapped - dur) > 1e-6:
            changed += 1
        out.append((token, snapped))
    return out, changed


def pairs_to_notes(pairs, tonic):
    """
    [(记号, 持续时间)] -> [(开始秒, 持续时间, midi 音高)]，休止符只往前走时间。

    导 midi 和喂给编辑器都用这个。
    """
    notes = []
    clock = 0.0
    for token, dur in pairs:
        dur = float(dur)
        rel = jianpu.token_to_rel(token)
        if rel is not None:
            notes.append((clock, dur, int(tonic) + rel))
        clock += dur
    return notes


def total_seconds(pairs):
    """整段录音有多长（秒）。"""
    return sum(float(dur) for _token, dur in pairs)


def write_midi(pairs, path, tonic, bpm=DEFAULT_BPM, name='melody'):
    """
    写一份**单音** midi。

    用 mido 直接写，不碰 mp3midi —— 那边是「音频转 MIDI」用的，带的依赖太重，
    精简版里根本没有；录出来的谱本来就是干净的单音，写起来就这么几行。

    同一时刻不会有第二个音在响：to_pairs 已经把尾巴剪过了，这里再保证
    「先松再按」（同音重复那次才看得出来）。

    轨道名别用中文：mido 写 meta 文本按 latin-1 编码，一个汉字就抛异常。
    """
    import mido

    ticks_per_beat = 480
    tempo = mido.bpm2tempo(bpm)
    midi = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage('track_name', name=name, time=0))
    track.append(mido.MetaMessage('set_tempo', tempo=tempo, time=0))
    track.append(mido.Message('program_change', program=0, time=0))

    # 休止符也是时间，所以起点必须从谱面里带过来（不能顺着往下加时值）
    events = []                     # (绝对 tick, 先松后按的排序, 消息)
    for start, dur, pitch in pairs_to_notes(pairs, tonic):
        on = max(int(round(mido.second2tick(float(start), ticks_per_beat, tempo))), 0)
        off = max(on + int(round(mido.second2tick(float(dur), ticks_per_beat, tempo))), on + 1)
        events.append((on, 1, mido.Message('note_on', note=int(pitch), velocity=90, time=0)))
        events.append((off, 0, mido.Message('note_off', note=int(pitch), velocity=0, time=0)))
    events.sort(key=lambda item: (item[0], item[1]))
    last = 0
    for when, _order, message in events:
        message.time = max(int(when) - last, 0)
        track.append(message)
        last = int(when)
    midi.save(path)
    return path


class Recorder:
    """
    录制器：start() 开始，stop() 收工，spans() 拿原始记录。

    钩子跑在自己的线程里（低级键盘 + 低级鼠标钩子各一个），回调里只做「记一笔」，
    绝不碰界面 —— 跟热键钩子一个路子。

    想跟着按键出声（桌面上录制时给自己一个「琴键反馈」），传一个 on_note 进来：
    on_note(记号, 按下没按下) 会在钩子线程里被叫到，里面别做慢活儿。
    """

    def __init__(self, log=None, on_note=None):
        self.log = log
        self.on_note = on_note      # 音符按下 / 松开时叫一声（桌面上录制出声用）
        self.active = False
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._spans = []            # [[记号, 按下秒, 松开秒]]
        self._open = {}             # 还按着的游戏键 -> 它那条 span
        self._mods = set()          # 现在按着哪些鼠标修饰键
        self._down = set()          # 现在按着哪些游戏键（挡住系统的自动重复）
        self._thread = None
        self._stop = threading.Event()
        self._thread_id = 0
        self._kb_hook = None
        self._ms_hook = None
        self._pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
        self._eat_cache = None      # 这一下要不要吃掉按键（见 _eat_keys）
        self._eat_cache_at = 0.0
        self._sink = 0              # 收键窗口（吃掉的那一下有正经去处，见 _sink_key）
        self._fg_log = None         # 上次记进日志的前台窗口：只在「变天」时记一笔
        self._kb_fp = None          # WINFUNCTYPE 代理，一定要留着引用
        self._ms_fp = None
        self._proc_type = None

    # ---------- 对外 ----------

    def count(self):
        """已经录到几个音（界面上的计数器用）。"""
        with self._lock:
            return len(self._spans)

    def elapsed(self):
        """录了多久（秒）。"""
        return 0.0 if not self._t0 else time.perf_counter() - self._t0

    def spans(self):
        """录到的原始记录（按下时间的顺序）。"""
        with self._lock:
            return [tuple(span) for span in self._spans]

    def start(self):
        """开始录；已经在录就直接返回 False。"""
        if self.active:
            return False
        with self._lock:
            self._spans, self._open, self._mods, self._down = [], {}, set(), set()
        self._stop.clear()
        self._t0 = time.perf_counter()
        self._seed_mods()
        self.active = True
        self._thread = threading.Thread(target=self._loop, name='录制钩子', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """收工，返回录到几个音。还按着的键按「松到现在」算。"""
        if not self.active:
            return 0
        self.active = False
        self._stop.set()
        self._join()
        now = self._now()
        with self._lock:
            for span in self._open.values():
                span[2] = max(now, span[1] + MIN_DUR)
            self._open.clear()
            self._down.clear()
            self._mods.clear()
            return len(self._spans)

    def cancel(self):
        """扔掉这次录制（退出程序时用）。"""
        if self.active:
            self.active = False
            self._stop.set()
            self._join()
        with self._lock:
            self._spans, self._open, self._down, self._mods = [], {}, set(), set()

    def _join(self):
        thread, self._thread = self._thread, None
        if thread is None or not thread.is_alive():
            return
        try:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
        thread.join(timeout=1.0)

    # ---------- 记账 ----------

    def _say(self, message):
        if self.log:
            try:
                self.log(message)
            except Exception:
                pass

    def _now(self):
        return time.perf_counter() - self._t0

    def _seed_mods(self):
        """
        开始那一刻鼠标本来就按着的键也要算数。

        不管的话，是按着右键再按 F10 开始录的，第一个音就会漏掉「升调」——
        游戏那边可是看得见的，谱面记错了人还以为是程序坏了。
        """
        try:
            user32 = ctypes.windll.user32
            for vk, mod in ((0x01, 'B'), (0x02, 'A'), (0x04, '#')):
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    self._mods.add(mod)
        except Exception:                               # pragma: no cover
            pass

    def _key(self, key, down):
        """
        一个游戏键按下 / 松开。

        顺便把「哪个音按下了 / 松开了」交给 on_note：桌面上录制时拿它出声，游戏里
        不出声（那儿的音游戏自己会放）。回调放在锁外面调 —— 出声那一下不该卡着
        别的按键记账。
        """
        token = None
        with self._lock:
            if down:
                if key in self._down:           # 系统的自动重复：同一个音不算第二遍
                    return
                self._down.add(key)
                token = token_of(DIGIT_BY_KEY[key], self._mods)
                span = [token, self._now(), None]
                self._spans.append(span)
                self._open[key] = span
            else:
                if key not in self._down:
                    return
                self._down.discard(key)
                span = self._open.pop(key, None)
                if span is not None and span[2] is None:
                    # 极短的点击也给它 MIN_DUR，免得变成 0 长度
                    span[2] = max(self._now(), span[1] + MIN_DUR)
                    token = span[0]
        if token is not None:
            self._emit(token, down)

    def _emit(self, token, down):
        """告诉外面「这个音按下了 / 松开了」。钩子回调里绝不能把异常抛出去。"""
        if self.on_note is None:
            return
        try:
            self.on_note(token, down)
        except Exception:
            pass


    def _mouse(self, message):
        """鼠标按下 / 松开：只更新「现在按着哪些修饰键」。"""
        with self._lock:
            if message in MOD_BY_MOUSE:
                self._mods.add(MOD_BY_MOUSE[message])
            elif message in MOUSE_UP_BY_DOWN:
                self._mods.discard(MOD_BY_MOUSE[MOUSE_UP_BY_DOWN[message]])

    # ---------- 钩子线程 ----------

    def _loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            # HOOKPROC 必须包成 WINFUNCTYPE 再把 argtypes 声明死：不声明的话
            # ctypes 会把 Python 方法当普通对象传进去，SetWindowsHookExW 直接返回
            # 空句柄（热键钩子那边也是这么写的，踩过一次了）。
            self._proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                                 ctypes.c_size_t, ctypes.c_ssize_t)
            self._kb_fp = self._proc_type(self._kb_proc)
            self._ms_fp = self._proc_type(self._ms_proc)
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self._proc_type,
                                                 ctypes.c_void_p, ctypes.c_uint]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_size_t, ctypes.c_ssize_t]
            user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            self._sink = self._make_sink(user32)
            self._install(user32)
            if self._kb_hook is None and self._ms_hook is None:
                self._say('录制装不上钩子，试试用管理员身份运行')
                return
            if self._kb_hook is None:
                self._say('键盘钩子没装上，录不了音（大概没权限）')
            if self._ms_hook is None:
                self._say('鼠标钩子没装上，升降调 / 升半音可能录不准')
            message = wintypes.MSG()
            while not self._stop.is_set():
                got = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if got <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:                    # pragma: no cover
            self._say('录制的钩子线程出错了：%r' % exc)
        finally:
            self._uninstall(user32)
            self._destroy_sink(user32)
            self._thread_id = 0

    def _install(self, user32):
        self._uninstall(user32)
        for what, hook_id, func in (('键盘', WH_KEYBOARD_LL, self._kb_fp),
                                    ('鼠标', WH_MOUSE_LL, self._ms_fp)):
            try:
                hook = user32.SetWindowsHookExW(hook_id, func, None, 0)
            except Exception as exc:                    # pragma: no cover
                hook = None
                self._say('%s钩子装的时候报错：%r' % (what, exc))
            if hook_id == WH_KEYBOARD_LL:
                self._kb_hook = hook
            else:
                self._ms_hook = hook
            if not hook:
                self._say('%s钩子没装上（错误码 %s）'
                          % (what, ctypes.windll.kernel32.GetLastError()))

    def _uninstall(self, user32):
        for name in ('_kb_hook', '_ms_hook'):
            hook = getattr(self, name)
            if hook:
                try:
                    user32.UnhookWindowsHookEx(ctypes.c_void_p(hook))
                except Exception:
                    pass
                setattr(self, name, None)

    @staticmethod
    def _foreground(user32):
        """前台窗口是谁：{'class': 类名, 'pid': 进程号}。钩子里不能磨蹭，只问这两样。"""
        info = {'class': '', 'pid': 0}
        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 64)
                info['class'] = buf.value
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
                info['pid'] = int(pid.value)
        except Exception:
            pass
        return info

    def _eat_keys(self, user32):
        """
        录制时这一下按键要不要「吃掉」（不往系统里传）。

        为什么：桌面（或者本程序自己的窗口）在前台时，z x c v b n m , 送给系统毫无意义，
        只会惹麻烦 —— 输入法弹候选框、桌面/系统接住按键「叮」一声。这时候干脆吃掉，
        谁也响不了；吃掉的那一下还会投给收键窗口（见 _sink_key），让它有个正经去处。

        什么时候吃：前台是本程序自己 / 前台是系统的窗口（桌面、任务栏、开始菜单、
        输入法自己的窗口）/ 前台那家伙是 explorer.exe / 连前台窗口都没有。
        游戏在前台时一律放过：那个音得靠游戏自己发出来，吃掉就把游戏按哑了。

        结果会缓存 FOREGROUND_CACHE_SEC（0.25 秒）：钩子里每按一个键都去问一遍
        前台是谁太浪费，前台也不会在一瞬间变来变去。
        """
        now = time.perf_counter()
        cached = self._eat_cache
        if cached is not None and now - self._eat_cache_at < FOREGROUND_CACHE_SEC:
            return cached
        info = self._foreground(user32)
        name = ''
        if audiowatch is not None and info['pid']:
            try:
                name = audiowatch.process_name(info['pid']) or ''
            except Exception:                       # pragma: no cover
                name = ''
        eat = (not info['class']                          # 没有前台窗口：按键只会掉到桌面
               or info['pid'] == self._pid                # 前台是本程序自己
               or info['class'] in DESKTOP_CLASSES        # 桌面 / 任务栏 / 输入法
               or name.lower() in SHELL_PROCESSES)        # 资源管理器（桌面也在它名下）
        self._eat_cache = eat
        self._eat_cache_at = now
        # 前台「变天」时记一笔：出问题好对照（真正的决定数 + 窗口类名 + 进程）
        state = (eat, info['class'], info['pid'])
        if state != self._fg_log:
            self._fg_log = state
            self._say('录制中前台窗口：%s（%s，PID %d）—— %s'
                      % (info['class'] or '（没有前台窗口）', name or '未知程序',
                         info['pid'],
                         '这八个键被吃掉' if eat else '按键原样送给它（游戏自己发声音）'))
        return eat

    # ---------- 收键窗口 ----------

    def _make_sink(self, user32):
        """
        开一个「收键窗口」：消息专用窗口（HWND_MESSAGE）——不会显示、也抢不到焦点。

        低级钩子要跑在有消息循环的线程上，这个窗口就用钩子线程现成的消息循环，
        不用另起线程；窗口的消息没人处理也没关系，它只是给吃掉的那一下当个接收人。
        """
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            user32.CreateWindowExW.restype = ctypes.c_void_p
            user32.CreateWindowExW.argtypes = [
                ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            hwnd = user32.CreateWindowExW(0, 'STATIC', 'AutoPlay 收键窗口', 0,
                                          0, 0, 0, 0, ctypes.c_void_p(HWND_MESSAGE),
                                          None, kernel32.GetModuleHandleW(None), None)
            return int(hwnd or 0)
        except Exception:                           # pragma: no cover
            return 0

    def _destroy_sink(self, user32):
        """关掉收键窗口（收工 / 退出时）。"""
        if not self._sink:
            return
        try:
            user32.DestroyWindow(ctypes.c_void_p(self._sink))
        except Exception:                           # pragma: no cover
            pass
        self._sink = 0

    def _sink_key(self, data, wparam):
        """
        把吃掉的那一下按键投给收键窗口：收下了，但不显示、不处理。

        按键被钩子吃掉之后，系统里没有任何窗口「收」过它 —— 有的机器会因此放一声
        提示音（跟不开程序、在桌面上敲字母一个道理）。投给我们自己的收键窗口，
        让它有个正经去处。

        只投 WM_KEYDOWN / WM_KEYUP，**不投 WM_CHAR**：带 Alt 的字符消息会让系统的
        默认窗口过程「叮」一声，那就白忙了。
        """
        if not self._sink:
            return
        try:
            lparam = (int(data.scanCode) << 16) | 1
            message = WM_KEYUP if wparam in (WM_KEYUP, WM_SYSKEYUP) else WM_KEYDOWN
            ctypes.windll.user32.PostMessageW(ctypes.c_void_p(self._sink), message,
                                              ctypes.c_size_t(int(data.vkCode)),
                                              ctypes.c_ssize_t(lparam))
        except Exception:                           # pragma: no cover
            pass

    def _kb_proc(self, code, wparam, lparam):
        if code == 0 and self.active:
            try:
                data = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                key = KEY_BY_VK.get(int(data.vkCode))
                if key:
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self._key(key, True)
                    elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                        self._key(key, False)
                    # 桌面 / 本程序在前台：账照样记，但这一下别送给系统（免得它响一声），
                    # 投给收键窗口让它有个正经去处
                    if self._eat_keys(ctypes.windll.user32):
                        self._sink_key(data, wparam)
                        return 1
            except BaseException:
                pass
        return ctypes.windll.user32.CallNextHookEx(None, code, wparam, lparam)

    def _ms_proc(self, code, wparam, lparam):
        if code == 0 and self.active:
            try:
                if wparam != WM_MOUSEMOVE:
                    self._mouse(int(wparam))
            except BaseException:
                pass
        return ctypes.windll.user32.CallNextHookEx(None, code, wparam, lparam)