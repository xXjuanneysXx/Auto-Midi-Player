# -*- coding: utf-8 -*-
"""
跟奏窗口：给眼睛看的「下落式」演奏提示
======================================

一个无边框、半透明、磨砂质感的窗口，盖在游戏上（置顶、不抢焦点、鼠标能穿透）：

* 上半部分是**下落的长条音符**，长条的长度就是这个音要按多久（跟 midi 的时值走）；
  长条够长就写上这个音的升降调 / 升半音记号（↑ 升调、↓ 降调、# 升半音）；
  太短写不下（「等长演奏」一个音才 90 毫秒）就看颜色 —— 窗口最上面一直挂着一条图例，
  细色块 + 它代表的操作，颜色和长条是同一张表；
* 音符落到下方那个圆角判定框就消失 —— 超出判定框的部分不画出来，看起来像「按进去了」；
* 最下面是 z x c v b n m , 八个琴键，正好对应简谱的 1 2 3 4 5 6 7 i；
* 颜色按**要按的操作**区分（不按鼠标 / 升半音 / 升调 / 升调+半音 / 降调 / 降调+半音），
  一眼看出这个音要不要按住鼠标、按住哪个键；
* 哪个键正被按住（程序自己弹的也算），对应那一列会发光，提醒就是按这一列。

两种节奏：

* **原速跟奏**：跟着演奏器走，时间轴读 player.elapsed()，和真正发按键的演奏器共用
  同一个钟，不会错位；
* **练习（等你按对）**：程序不发按键，音符落到判定线就停住，你按对那个键才继续往下走。
  新手不用被原曲速度拖着跑。
"""

import ctypes
import theme
import time
from bisect import bisect_left, bisect_right
from collections import namedtuple

try:                                                      # 优先 Qt 官方绑定
    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
    from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPen)
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError:                                       # 装了 PyQt6 也行
    from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
    from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPen)
    from PyQt6.QtWidgets import QApplication, QWidget

import jianpu
import player as player_mod


# 一个音在界面上需要的全部信息
Note = namedtuple('Note', 'lane start dur color')

# 修饰键组合 -> (长条尾部颜色, 长条头部颜色)。
# 尾深头亮，看起来有方向感；六种颜色在深色背景上互相分得开。
# 颜色代表「要按住哪个鼠标键」，六种操作色由主题统一提供（theme.NOTE_COLORS
# 是个就地改的字典，换主题时自动更新，见 theme.py）。
NOTE_COLORS = theme.NOTE_COLORS

# 没在弹的时候，某一列发光的颜色
IDLE_GLOW = theme.c('#9fc4ff')

# 色块上写的记号（跟谱面文件里的写法一致：# 写在最前面）
NOTE_SYMBOL = {'': '', '#': '#', 'A': '↑', 'B': '↓', '#A': '#↑', '#B': '#↓'}

# 按对 / 按错的反馈色
CORRECT_FLASH = theme.c('#5fd18b')
WRONG_FLASH = theme.c('#f08a8a')

@theme.subscribe
def _reload_theme_colors():
    """主题换了：把上面这几个模块级颜色重新取一遍（画的时候现取的那些不用管）。"""
    global IDLE_GLOW, CORRECT_FLASH, WRONG_FLASH
    IDLE_GLOW = theme.c('#9fc4ff')
    CORRECT_FLASH = theme.c('#5fd18b')
    WRONG_FLASH = theme.c('#f08a8a')


# 跟奏的两种节奏：默认「等我按对」（练习模式），另一个是按原曲速度自动走
PACE_MODES = ('等我按对', '原速跟奏')
PRACTICE_PACE = PACE_MODES[0]

# 顶上那条操作说明（图例）的每一行：色块用 NOTE_COLORS 里的颜色，文字说明它代表什么。
# 「等长演奏」时一个音只有 90 毫秒那么长，色块短得写不下 ↑ / # 记号，颜色就是唯一的
# 线索，所以这条说明一直挂在窗口最上面。
LEGEND_ITEMS = (('', '正常'), ('#', '升半音'), ('A', '升调'),
                ('#A', '升调+半音'), ('B', '降调'), ('#B', '降调+半音'))

# 练习模式：音符离判定线还有这么近（秒）时，抢拍按下去也算数
EARLY_WINDOW = 0.25


class FollowWindow(QWidget):
    """盖在游戏上的跟奏面板。"""

    # (琴键, 简谱数字, 虚拟键码)
    KEYS = (('z', '1', 0x5A), ('x', '2', 0x58), ('c', '3', 0x43), ('v', '4', 0x56),
            ('b', '5', 0x42), ('n', '6', 0x4E), ('m', '7', 0x4D), (',', 'i', 0xBC))

    # 摆在屏幕的哪个位置。上下不要贴边，所以「底部 / 顶部」都留一段空白。
    POSITIONS = ('底部偏上', '屏幕正中', '顶部偏下', '右上角', '底部靠左', '底部靠右')
    MARGIN = 18            # 左右和角落里留多少
    EDGE_RATIO = 0.07      # 上下留白取屏幕高度的百分之几
    EDGE_MIN = 40          # 上下留白至少这么多像素
    # 老版本存过的位置名，读设置的时候换过来，免得用户升级后位置被重置
    LEGACY = {'底部居中': '底部偏上'}

    LANE_W = 58            # 每列宽度
    PAD = 12               # 面板左右留白
    LEGEND_H = 30          # 顶上那条操作说明（图例）的高度
    LEGEND_BAR_W = 16      # 图例里那条细色块多宽
    LEGEND_BAR_H = 7       # 多高（细得像一条线，跟音符长条一个颜色）
    HEADER_H = 26          # 顶上那行状态字
    KEYS_H = 50            # 琴键高度
    FRAME_H = 22           # 判定框高度
    FRAME_GAP = 8          # 判定框和琴键之间的缝
    LOOKAHEAD = 3.0        # 音符提前几秒出现在面板顶上
    MIN_NOTE_PX = 9        # 再短的音也画这么长，不然看不见
    FPS = 60

    STATE_TEXT = {
        'idle':  ('还没读谱面', theme.c('#8b93a7')),
        'ready': ('已就绪，按 F6 开始', theme.c('#7fb0ff')),
        'lead':  ('准备中…', theme.c('#7fb0ff')),
        'play':  ('演奏中', theme.c('#5fd18b')),
        'practice': ('练习中', theme.c('#5fd18b')),
        'waiting':  ('该你按了', theme.c('#ffc247')),
        'pause': ('已暂停', theme.c('#e0b341')),
        'stop':  ('已停止', theme.c('#9aa6ba')),
        'done':  ('演奏完成', theme.c('#7fb0ff')),
    }

    # 这些状态下音符是「活的」（要画出来）
    MOVING_STATES = ('lead', 'play', 'pause', 'practice', 'waiting')

    def __init__(self, player=None, parent=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle('跟奏模式')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # 鼠标必须能穿过去：升降调 / 升半音按的就是鼠标键，被这一层挡住就发不进游戏了
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(self.LANE_W * len(self.KEYS) + self.PAD * 2, 470 + self.LEGEND_H)

        self.player = player
        self.notes = []                       # 全部音符，按开始时间排好
        self._lane_notes = [[] for _ in self.KEYS]
        self._lane_starts = [[] for _ in self.KEYS]
        self._starts = []
        self.state = 'idle'
        self._t = 0.0
        self._held = [False] * len(self.KEYS)
        self._prev_held = [False] * len(self.KEYS)
        self._anchor = self.POSITIONS[0]
        self._panel_alpha = 205               # 拿不到磨砂就画厚一点，保证看得清
        self._blur_tried = False

        # 练习模式（等你按对）
        self.paced = False
        self._wait_index = 0                  # 下一个等你按的音
        self._paused = False
        self._last_wall = 0.0
        self._flash = {}                      # 列号 -> (过期时刻, 颜色)
        self.on_progress = None               # 练习进度回调 (已完成, 总数)
        self.on_finish = None                 # 练习结束回调 ('done' / 'stop')

        self._symbol_font = QFont('Microsoft YaHei UI', 9)
        self._symbol_font.setBold(True)

        try:
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_bool
        except Exception:
            pass

        self._timer = QTimer(self)
        self._timer.setInterval(max(int(1000 / self.FPS), 8))
        self._timer.timeout.connect(self._tick)

    # ---------- 对外接口 ----------

    def set_score(self, events, speed=1.0):
        """把谱面交给跟奏窗口。events = [(音, 开始秒, 持续时间)]，speed 是界面上的倍速。"""
        self.notes = []
        self._lane_notes = [[] for _ in self.KEYS]
        lane_of = {key: index for index, (key, _, _) in enumerate(self.KEYS)}
        for token, start, dur in player_mod.Player.speed_up(events, speed):
            if jianpu.is_rest(token):
                continue
            mods, digit = jianpu.parse_token(token)
            lane = lane_of.get(jianpu.DIGIT_KEYS.get(digit))
            if lane is None:
                continue
            color = ''.join(mods)
            if color not in NOTE_COLORS:
                color = ''
            note = Note(lane, start, dur, color)
            self.notes.append(note)
            self._lane_notes[lane].append(note)
        self.notes.sort(key=lambda note: note.start)
        for lane in self._lane_notes:
            lane.sort(key=lambda note: note.start)
        self._starts = [note.start for note in self.notes]
        self._lane_starts = [[note.start for note in lane] for lane in self._lane_notes]
        self._t = self._starts[0] if self._starts else 0.0
        self._wait_index = 0
        self._flash.clear()
        self.update()

    def set_player(self, player):
        self.player = player

    def set_ready(self):
        """读好谱面、等开始。"""
        self.paced = False
        self._paused = False
        self._wait_index = 0
        self.state = 'ready' if self.notes else 'idle'
        self._t = 0.0
        self.show_panel()

    def begin(self, wait=False):
        """
        开始跟奏。

        wait=False：原速跟奏，时间轴跟着演奏器走；
        wait=True ：练习模式，时间由你的手推着走（程序不发按键）。
        """
        self.paced = bool(wait)
        self._paused = False
        self._wait_index = 0
        self._flash.clear()
        self._last_wall = time.perf_counter()
        if self.paced:
            # 从第一个音的前 LOOKAHEAD 秒开始，让第一个音好好落下来
            self._t = (self.notes[0].start - self.LOOKAHEAD) if self.notes else 0.0
            self.state = 'practice'
        else:
            self.state = 'lead'
        self.show_panel()
        if self.paced:
            self._fire_progress()

    def toggle_pause(self):
        """练习模式：暂停 / 继续，返回现在是不是暂停。"""
        self._paused = not self._paused
        self._last_wall = time.perf_counter()      # 暂停这段别算进时间里
        self.state = 'pause' if self._paused else 'practice'
        return self._paused

    def cancel(self):
        """练习中途停止。"""
        self.paced = False
        self._paused = False
        self.state = 'stop'
        self._fire_finish('stop')

    def _clamped(self):
        """练习模式：下一个音已经停在判定线上等你了。"""
        if self._wait_index >= len(self.notes):
            return False
        return self._t >= self.notes[self._wait_index].start - 0.001

    def _fire_progress(self):
        if callable(self.on_progress):
            try:
                self.on_progress(self._wait_index, len(self.notes))
            except Exception:
                pass

    def _fire_finish(self, reason):
        if callable(self.on_finish):
            try:
                self.on_finish(reason)
            except Exception:
                pass

    def set_state(self, state):
        """play / pause / stop / done，演奏器那边状态变了就喊一声。"""
        self.state = state
        self.update()

    def finish(self, state='stop'):
        self.state = state
        self.update()

    def set_anchor(self, name):
        """放在屏幕的哪个位置（POSITIONS 里的名字）。"""
        name = self.LEGACY.get(name, name)
        if name in self.POSITIONS:
            self._anchor = name
            self.apply_position()

    @classmethod
    def resolve_position(cls, name):
        """把（可能是老版本的）位置名换成当前有效的位置名。"""
        name = cls.LEGACY.get(str(name), str(name))
        return name if name in cls.POSITIONS else cls.POSITIONS[0]

    def show_panel(self):
        self.apply_position()
        if not self.isVisible():
            self.show()
        self.keep_on_top()
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def shutdown(self):
        """收起来，不再占用资源。"""
        self._timer.stop()
        self.hide()

    def apply_position(self):
        """按选定的位置贴到屏幕上（跟着鼠标所在的那块屏幕走）。"""
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        # 上下的留白按屏幕高度算：屏幕越大留得越多，但最少 EDGE_MIN，保证不贴边
        gap = max(self.EDGE_MIN, int(area.height() * self.EDGE_RATIO))
        mid_x = area.left() + (area.width() - self.width()) // 2
        left_x = area.left() + self.MARGIN
        right_x = area.right() - self.width() - self.MARGIN + 1
        bottom_y = area.bottom() - self.height() - gap + 1
        if self._anchor == '底部靠左':
            x, y = left_x, bottom_y
        elif self._anchor == '底部靠右':
            x, y = right_x, bottom_y
        elif self._anchor == '右上角':
            x, y = right_x, area.top() + self.MARGIN
        elif self._anchor == '屏幕正中':
            x = mid_x
            y = area.top() + (area.height() - self.height()) // 2
        elif self._anchor == '顶部偏下':
            x, y = mid_x, area.top() + gap
        else:                                              # 底部偏上（默认）
            x, y = mid_x, bottom_y
        self.move(int(x), int(y))

    # ---------- 窗口事件 ----------

    def showEvent(self, event):
        super().showEvent(event)
        self.apply_position()
        QTimer.singleShot(0, self.apply_blur)

    def keep_on_top(self):
        """再顶一次（SWP_NOACTIVATE：顶上去但不抢焦点）。"""
        if not self.isVisible():
            return
        try:
            ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(int(self.winId())), ctypes.c_void_p(-1),
                0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
        except Exception:
            pass

    def apply_blur(self):
        """
        磨砂玻璃：调 Windows 的亚克力模糊（Win10 1803 以后都有）。

        拿不到也没关系，自己画的深色半透明面板照样能用，只是不那么「磨砂」。
        """
        if self._blur_tried:
            return
        self._blur_tried = True
        try:
            class AccentPolicy(ctypes.Structure):
                _fields_ = [('AccentState', ctypes.c_int), ('AccentFlags', ctypes.c_int),
                            ('GradientColor', ctypes.c_uint), ('AnimationId', ctypes.c_int)]

            class CompositionData(ctypes.Structure):
                _fields_ = [('Attribute', ctypes.c_int),
                            ('Data', ctypes.POINTER(AccentPolicy)),
                            ('SizeOfData', ctypes.c_size_t)]

            accent = AccentPolicy()
            accent.AccentState = 4                 # ACCENT_ENABLE_ACRYLICBLURBEHIND
            accent.AccentFlags = 2
            accent.GradientColor = 0x4C20160F      # ABGR：淡淡的深蓝 tint
            data = CompositionData()
            data.Attribute = 19                    # WCA_ACCENT_POLICY
            data.Data = ctypes.pointer(accent)
            data.SizeOfData = ctypes.sizeof(accent)
            ok = ctypes.windll.user32.SetWindowCompositionAttribute(
                ctypes.c_void_p(int(self.winId())), ctypes.byref(data))
            if ok:
                self._panel_alpha = 148            # 有磨砂了，底色可以淡一点
            try:                                   # Win11：窗口圆角也交给系统裁
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(int(self.winId())), ctypes.c_uint(33),
                    ctypes.byref(ctypes.c_int(2)), ctypes.sizeof(ctypes.c_int))
            except Exception:
                pass
        except Exception:
            self._panel_alpha = 205
        self.update()

    # ---------- 每帧 ----------

    def _tick(self):
        """对时间、看一眼哪几个键被按住，有变化才重画。"""
        held = self._read_keys()
        changed = held != self._held
        edges = [index for index, down in enumerate(held) if down and not self._prev_held[index]]
        self._prev_held = held
        self._held = held
        if self.paced:
            self._tick_practice(edges)
            self.update()
            return
        elapsed = None
        if self.player is not None:
            try:
                elapsed = self.player.elapsed()
            except Exception:
                elapsed = None
        if elapsed is not None:
            self._t = elapsed
            if self.state != 'pause':
                self.state = 'lead' if elapsed < 0 else 'play'
        if changed or elapsed is not None:
            self.update()

    def _tick_practice(self, edges):
        """
        练习模式：时间由你的手推着走。

        音符照常往下落，但落到判定线就停住 —— 按对那个键才继续。按错了不往前走，
        只把那一列闪一下红，免得越按越乱。
        """
        now = time.perf_counter()
        delta = max(now - self._last_wall, 0.0)
        self._last_wall = now
        for lane, (expire, _) in list(self._flash.items()):
            if expire < now:
                del self._flash[lane]
        if self._paused or not self.notes:
            return
        if self._wait_index >= len(self.notes):
            if self.state != 'done':
                self.state = 'done'
                self._fire_finish('done')
            return
        target = self.notes[self._wait_index]
        early = target.start - self._t <= EARLY_WINDOW
        if target.lane in edges and (self._clamped() or early):
            self._wait_index += 1
            self._flash[target.lane] = (now + 0.22, CORRECT_FLASH)
            if self._wait_index >= len(self.notes):
                self.state = 'done'
                self._fire_progress()
                self._fire_finish('done')
                return
            target = self.notes[self._wait_index]
            self._fire_progress()
        elif edges:
            for lane in edges:
                if lane != target.lane:
                    self._flash[lane] = (now + 0.22, WRONG_FLASH)
        if self._t < target.start:
            self._t = min(self._t + delta, target.start)
            self.state = 'practice'
        else:
            self._t = target.start                  # 压在线上了，等你按
            self.state = 'waiting'

    def _read_keys(self):
        """哪几个键现在被按住（程序用 SendInput 弹的也算，按键状态一样会变）。"""
        user32 = ctypes.windll.user32
        return [bool(user32.GetAsyncKeyState(vk) & 0x8000) for _, _, vk in self.KEYS]

    # ---------- 几何 ----------

    def _content_top(self):
        """音符和琴键那块从哪儿开始：图例 + 状态行下面。"""
        return self.LEGEND_H + self.HEADER_H

    def _keys_top(self):
        return self.height() - self.KEYS_H - 4

    def _hit_line(self):
        """判定框底边：音符的头部走到这里就正好该按下去。"""
        return self._keys_top() - self.FRAME_GAP

    def _pps(self):
        """每秒下落多少像素。"""
        return (self._hit_line() - self._content_top()) / self.LOOKAHEAD

    def _lane_x(self, lane):
        return self.PAD + lane * self.LANE_W

    def _lane_rect(self, lane):
        return QRectF(self._lane_x(lane), self._content_top(),
                      self.LANE_W, self._hit_line() - self._content_top())

    # ---------- 画 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._paint_panel(painter)
        self._paint_lanes(painter)
        if self.state in self.MOVING_STATES:
            painter.save()
            # 从状态行下面开始画：音符别压到图例 / 状态字上
            top = self._content_top() - 8
            painter.setClipRect(QRectF(0, top, self.width(), self._hit_line() - top))
            self._paint_notes(painter)
            painter.restore()
        self._paint_frame(painter)
        self._paint_keys(painter)
        self._paint_header(painter)
        self._paint_legend(painter)
        painter.end()

    def _paint_panel(self, painter):
        painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
        painter.setBrush(QBrush(QColor(10, 13, 20, self._panel_alpha)))
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 14, 14)

    def _paint_lanes(self, painter):
        painter.setPen(QPen(QColor(255, 255, 255, 14), 1))
        for lane in range(1, len(self.KEYS)):
            x = self._lane_x(lane)
            painter.drawLine(int(x), self._content_top(), int(x), int(self._hit_line()))
        # 被按住 / 刚按对按错的列：整列透出一层它自己的颜色，越靠近判定框越亮
        for lane in range(len(self.KEYS)):
            color = self._active_color(lane)
            if color is None:
                continue
            rect = self._lane_rect(lane).adjusted(2, 0, -2, 0)
            gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
            gradient.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), 0))
            gradient.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 132))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(gradient))
            painter.drawRoundedRect(rect, 9, 9)

    def _paint_notes(self, painter):
        pps = self._pps()
        hit = self._hit_line()
        waiting = (self.notes[self._wait_index]
                   if self.paced and self._wait_index < len(self.notes) else None)
        for note in self.notes:
            head = hit - (note.start - self._t) * pps
            length = max(note.dur * pps, self.MIN_NOTE_PX)
            tail = head - length
            if head < 0 or tail > hit:                 # 还没进来 / 已经按过去了
                continue
            rect = QRectF(self._lane_x(note.lane) + 4, tail, self.LANE_W - 8, length)
            dark, light = (QColor(c) for c in NOTE_COLORS[note.color])
            gradient = QLinearGradient(0, rect.top(), 0, rect.bottom())
            gradient.setColorAt(0.0, dark)
            gradient.setColorAt(1.0, light)
            radius = min(6.0, length / 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(gradient))
            painter.drawRoundedRect(rect, radius, radius)
            self._paint_symbol(painter, rect, note)
            if note is waiting:                      # 停在判定线上等你的那个音，描个白边
                painter.setPen(QPen(QColor(255, 255, 255, 215), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, radius, radius)

    def _paint_symbol(self, painter, rect, note):
        """把升降调 / 升半音的记号直接写在色块上。"""
        text = NOTE_SYMBOL.get(note.color, '')
        if not text or rect.height() < 13:
            return
        painter.setFont(self._symbol_font)
        if rect.height() > 34:                       # 长条就把记号写在头部（靠下边）
            area = QRectF(rect.left(), rect.bottom() - 20, rect.width(), 20)
        else:
            area = rect
        # 先描一层深色再写白的，深色块浅色块上都看得清
        painter.setPen(QColor(8, 11, 18, 210))
        painter.drawText(area.adjusted(1, 1, 1, 1), Qt.AlignmentFlag.AlignCenter, text)
        painter.setPen(QColor(255, 255, 255, 245))
        painter.drawText(area, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_frame(self, painter):
        """按键正上方那个判定框：音符走到这里就消失。"""
        top = self._hit_line() - self.FRAME_H
        rect = QRectF(self.PAD - 3, top, self.width() - (self.PAD - 3) * 2, self.FRAME_H)
        painter.setPen(QPen(QColor(255, 255, 255, 46), 1))
        painter.setBrush(QBrush(QColor(255, 255, 255, 12)))
        painter.drawRoundedRect(rect, 9, 9)
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
        for lane in range(1, len(self.KEYS)):
            x = self._lane_x(lane)
            painter.drawLine(int(x), int(top + 4), int(x), int(top + self.FRAME_H - 4))
        # 有键按下去的那一列，判定线上也亮一条：看一眼就知道现在按的是哪列
        for lane in range(len(self.KEYS)):
            color = self._active_color(lane)
            if color is None:
                continue
            color.setAlpha(215)
            painter.setPen(QPen(color, 2))
            left = self._lane_x(lane) + 5
            right = self._lane_x(lane) + self.LANE_W - 5
            painter.drawLine(QPointF(left, self._hit_line() - 0.5),
                             QPointF(right, self._hit_line() - 0.5))

    def _paint_keys(self, painter):
        top = self._keys_top()
        letter_font = QFont('Microsoft YaHei UI', 13)
        letter_font.setBold(True)
        digit_font = QFont('Microsoft YaHei UI', 8)
        for lane, (key, digit, _) in enumerate(self.KEYS):
            rect = QRectF(self._lane_x(lane) + 4, top, self.LANE_W - 8, self.KEYS_H - 4)
            active = self._active_color(lane)
            color = active if active is not None else QColor(theme.c('#5a6478'))
            if active is not None:
                fill = QColor(color.red(), color.green(), color.blue(), 96)
                border = QColor(color.red(), color.green(), color.blue(), 220)
            else:
                fill = QColor(26, 33, 48, 210)
                border = QColor(255, 255, 255, 34)
            painter.setPen(QPen(border, 2 if active is not None else 1))
            painter.setBrush(QBrush(fill))
            painter.drawRoundedRect(rect, 10, 10)
            painter.setFont(letter_font)
            painter.setPen(QColor(theme.c('#ffffff')) if active is not None else QColor(theme.c('#c8d0de')))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, key)
            painter.setFont(digit_font)
            painter.setPen(QColor(theme.c('#8b93a7')))
            painter.drawText(rect.adjusted(6, 4, 0, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, digit)

    def _paint_legend(self, painter):
        """
        最上面那条：一条细圆角色块 + 它代表的操作。

        颜色和音符长条用的是同一张表（NOTE_COLORS），所以「等长演奏」把长条压得很短、
        写不下 ↑ / # 记号的时候，看一眼颜色就知道这个音要不要按住鼠标。
        """
        font = QFont('Microsoft YaHei UI', 9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        inner, gap = 5, 10                     # 色块到文字、两组之间的间距
        widths = [self.LEGEND_BAR_W + inner + metrics.horizontalAdvance(label)
                  for _key, label in LEGEND_ITEMS]
        total = sum(widths) + gap * (len(widths) - 1)
        x = self.PAD + max((self.width() - self.PAD * 2 - total) / 2.0, 0.0)
        middle = self.LEGEND_H / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        for (key, label), width in zip(LEGEND_ITEMS, widths):
            dark, light = NOTE_COLORS[key]
            bar = QRectF(x, middle - self.LEGEND_BAR_H / 2.0,
                         self.LEGEND_BAR_W, self.LEGEND_BAR_H)
            gradient = QLinearGradient(bar.left(), 0, bar.right(), 0)
            gradient.setColorAt(0.0, QColor(dark))
            gradient.setColorAt(1.0, QColor(light))
            painter.setBrush(QBrush(gradient))
            painter.drawRoundedRect(bar, self.LEGEND_BAR_H / 2.0, self.LEGEND_BAR_H / 2.0)
            painter.setPen(QColor(theme.c('#9aa6ba')))
            painter.drawText(QRectF(x + self.LEGEND_BAR_W + inner, 0,
                                    width - self.LEGEND_BAR_W - inner, self.LEGEND_H),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            painter.setPen(Qt.PenStyle.NoPen)
            x += width + gap

    def _paint_header(self, painter):
        top = self.LEGEND_H + 4
        height = self.HEADER_H - 6
        text, color = self.STATE_TEXT.get(self.state, self.STATE_TEXT['stop'])
        painter.setFont(QFont('Microsoft YaHei UI', 10))
        painter.setPen(QColor(color))
        painter.drawText(QRectF(self.PAD, top, self.width() - self.PAD * 2, height),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         '跟奏 · ' + text)
        if self.notes:
            done = (self._wait_index if self.paced
                    else bisect_right(self._starts, max(self._t, 0.0)))
            painter.setFont(QFont('Microsoft YaHei UI', 9))
            painter.setPen(QColor(theme.c('#8b93a7')))
            painter.drawText(QRectF(self.PAD, top, self.width() - self.PAD * 2, height),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             '%d / %d' % (done, len(self.notes)))

    def _glow_color(self, lane):
        """这一列现在（或刚刚）在弹的音是什么颜色，没有就用中性蓝。"""
        lane_notes = self._lane_notes[lane]
        if not lane_notes:
            return IDLE_GLOW
        starts = self._lane_starts[lane]
        index = min(bisect_left(starts, self._t), len(lane_notes) - 1)
        for candidate in (index, index - 1):
            if 0 <= candidate < len(lane_notes):
                if abs(starts[candidate] - self._t) <= 0.8:
                    return NOTE_COLORS[lane_notes[candidate].color][1]
        return IDLE_GLOW

    def _active_color(self, lane):
        """这一列现在该亮成什么颜色：按住的音色，或者按对 / 按错的反馈色。"""
        flash = self._flash.get(lane)
        if flash is not None:
            if flash[0] >= time.perf_counter():
                return QColor(flash[1])
            self._flash.pop(lane, None)
        if self._held[lane]:
            return QColor(self._glow_color(lane))
        return None
