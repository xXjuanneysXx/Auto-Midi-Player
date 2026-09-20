# -*- coding: utf-8 -*-
"""
简谱编辑器：把音轨画成钢琴卷帘，手动加减音符
============================================

给谁用
------
midi / mp3 转出来的那条主旋律（`melody lead`）总有不合心意的地方：多冒出来一个装饰音、
少了一个音、某个音拖得太长。这个窗口把它铺开画成一排方块 —— 横着是时间、竖着是音高、
方块长短就是时值，每个方块上写着它在简谱里的记号（`E2` / `#A1` / `B5`…），拖一拖就能改，
改完直接听。

能干什么
--------
* **看**：钢琴卷帘 + 左边键盘 + 上边时间尺；方块颜色按「要按住哪个鼠标键」分，跟跟奏
  窗口是同一套配色；
* **改**：双击空白加音、右键音块删音、按住拖动、拖右边缘改长短、方向键微调、Ctrl+Z 撤销；
* **听**：整首实时试听（改过就自动重合成），拖时间尺定位，双击音块单独听这一个音；
* **存**：没改完也能存成工程文件（`.mproj`，其实就是个 json），改完导出成 `.mid`。

导出的时候会把叠在一起的音裁开（游戏只认单音），裁掉几个会写在日志里。

界面
----
这个窗口**不往游戏里发按键**，所以不需要管理员权限，也不会弹 UAC。
"""

import json
import math
import os
import sys
import theme
import threading
import time
from dataclasses import dataclass

try:                                                  # 优先 Qt 官方绑定
    from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
    from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                               QPainterPath, QPen)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox, QFileDialog,
                                   QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                                   QPushButton, QScrollBar, QSpinBox, QTextEdit,
                                   QVBoxLayout, QWidget)
except ImportError:                                   # 装了 PyQt6 也行
    from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, pyqtSignal as Signal
    from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                             QPainterPath, QPen)
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox, QFileDialog,
                                 QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                                 QPushButton, QScrollBar, QSpinBox, QTextEdit,
                                 QVBoxLayout, QWidget)

import autosave
import jianpu
import midi_analyze
import preview
from mp3midi import audio2midi


APP_TITLE = 'MIDI 简谱编辑器'
APP_VERSION = '1.0'

RULER_H = 24            # 顶上时间尺的高度
KEYS_W = 68             # 左边键盘的宽度
SB_W = 12               # 滚动条粗细
EDGE_PX = 6             # 离右边缘这么近，就算「拖长度」而不是「移动」
MIN_DUR = 0.02          # 再短的音也不让存
DEFAULT_DUR = 0.30      # 「新音时长」默认值（秒）
UNDO_LIMIT = 200        # 撤销栈最多记这么多步
HIGH_PITCH = 108        # 卷帘最高画到哪儿（C8）
LOW_PITCH = 21          # 最低（A0，钢琴最左边那个键）
AUDITION_MAX = 0.80     # 双击试听单个音，最长放这么久

PROJECT_SUFFIX = '.mproj'
PROJECT_FORMAT = 'autoplay-editor'
PROJECT_VERSION = 1
PROJECT_FILTER = '简谱工程 (*.mproj);;所有文件 (*.*)'

# 自动保存：改过没存的谱面隔这么久看一眼（内容没变就不重复写盘，见 autosave.py）
AUTOSAVE_TICK_MS = 2500
MIDI_FILTER = 'MIDI 文件 (*.mid *.midi);;所有文件 (*.*)'

# 跟跟奏窗口同一套配色：颜色代表「要按住哪个鼠标键」
# 颜色代表「要按住哪个鼠标键」，六种操作色由主题统一提供（theme.NOTE_COLORS
# 是个就地改的字典，换主题时自动更新，见 theme.py）。
NOTE_COLORS = theme.NOTE_COLORS

PICK_HINT = ('双击空白＝加音　右键音块＝删音　拖动＝移动　拖右边缘＝改长短　'
             '方向键＝微调　Shift+方向键＝改长短　Del＝删掉选中的　Ctrl+滚轮＝缩放')

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 13px; color: #e6e9ef; }
QWidget#root { background: #0f1219; }
QLabel { color: #e6e9ef; }
QLabel#title { font-size: 17px; font-weight: 600; }
QLabel#hint { color: #6f7787; font-size: 12px; }
QLabel#value { color: #8b93a7; font-size: 12px; }
QPushButton { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
              padding: 6px 12px; color: #dfe4ee; }
QPushButton:hover { background: #242c3c; }
QPushButton:pressed { background: #1a2029; }
QPushButton:disabled { background: #171b24; border-color: #222836; color: #5c6478; }
QPushButton#primary { background: #3b82f6; border-color: #3b82f6; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #4b8ef8; }
QPushButton#playing { background: #3a2226; border-color: #6b2f33; color: #f0a0a0; }
QPushButton#playing:hover { background: #46282d; }
QPushButton#mini { padding: 4px 10px; }
QPushButton#maxButton { padding: 0; border-radius: 8px; background: #24405f;
                        border: 1px solid #3b82f6; color: #bcd8ff;
                        font-size: 15px; font-weight: 600; }
QPushButton#maxButton:hover { background: #3b82f6; border-color: #7fb0ff; color: #ffffff; }
QPushButton#maxButton:pressed { background: #2f6fd0; border-color: #7fb0ff; }
QPushButton#maxButton[maxed="true"] { background: #2f6fd0; border-color: #9ec9ff;
                                          color: #ffffff; }
QSpinBox, QDoubleSpinBox { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
                           padding: 4px 6px; color: #dfe4ee; }
QSpinBox:focus, QDoubleSpinBox:focus { border-color: #3b82f6; }
QCheckBox { color: #dfe4ee; spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px;
                       border: 1px solid #2b3345; background: #1d2330; }
QCheckBox::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle { background: #2b3345; border-radius: 5px; }
QScrollBar::handle:horizontal { min-width: 30px; }
QScrollBar::handle:vertical { min-height: 30px; }
QScrollBar::handle:hover { background: #39435a; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QTextEdit { background: #0c0f15; border: 1px solid #232937; border-radius: 10px;
            color: #b9c1d1; font-family: Consolas, 'Cascadia Mono', monospace;
            font-size: 12px; padding: 6px; }
"""
# 后补的控件 / 快捷键：上面的导入块不动，缺的在这儿补
try:
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QSlider
except ImportError:
    from PyQt6.QtGui import QKeySequence, QShortcut
    from PyQt6.QtWidgets import QSlider

try:
    import winsound                      # 只有 Windows 有，双击试听单个音要用
except ImportError:                      # pragma: no cover
    winsound = None


# ============ 数据：音符 & 谱面 ============

@dataclass
class Note:
    """一个音：起点（秒）、时值（秒）、midi 音高。"""

    start: float
    dur: float
    pitch: int

    @property
    def end(self):
        return self.start + self.dur


def token_of(pitch, tonic):
    """midi 音高 -> 简谱记号（音块上写的那个 `E2` / `#A1`）。"""
    return jianpu.pitch_to_token(int(pitch), int(tonic))


def color_key(token):
    """
    记号 -> 配色键（和跟奏窗口同一套：颜色代表要按住哪个鼠标键）。

    返回 '' / '#' / 'A' / '#A' / 'B' / '#B'，正好是 NOTE_COLORS 的键。
    """
    text = token.text
    sharp = text.startswith('#')
    body = text[1:] if sharp else text
    prefix = body[:1]
    return ('#' if sharp else '') + ('' if prefix == 'E' else prefix)


def format_time(seconds, decimals=False):
    """秒 -> `1:23` / `1:23.4`。"""
    seconds = max(float(seconds), 0.0)
    if decimals:
        minutes = int(seconds // 60)
        return '%d:%04.1f' % (minutes, seconds - minutes * 60)
    total = int(round(seconds))
    return '%d:%02d' % divmod(total, 60)


def mono_notes(notes):
    """
    把所有尾巴压到下一个音开始之前，保证同一时刻只有一个音在响。

    游戏只认单音，所以导出之前必须过一遍；返回 (新列表, 被剪短的音数)。
    """
    items = sorted(notes, key=lambda note: (note.start, note.pitch))
    out = []
    trimmed = 0
    for index, note in enumerate(items):
        dur = max(float(note.dur), MIN_DUR)
        if index + 1 < len(items):
            room = items[index + 1].start - note.start
            if room < dur - 1e-9:
                dur = max(room, MIN_DUR)
                trimmed += 1
        out.append(Note(float(note.start), dur, int(note.pitch)))
    return out, trimmed


class Score:
    """一份可编辑的谱：一串音符 + 主音 + 来源信息，自带撤销栈。"""

    def __init__(self, notes=None, tonic=60, bpm=120.0, path='', track_index=-1,
                 track_name=''):
        self.notes = [Note(float(n.start), float(n.dur), int(n.pitch))
                      for n in (notes or [])]
        self.tonic = int(tonic)
        self.bpm = float(bpm)
        self.path = path                 # 从哪个 midi 来的（另存 / 导出时拿来起名）
        self.track_index = int(track_index)
        self.track_name = track_name
        self.dirty = False               # 改过还没存？
        self._undo = []
        self._redo = []

    # ---- 查询 ----

    def ordered(self):
        return sorted(self.notes, key=lambda note: (note.start, note.pitch))

    def total(self):
        return max([note.end for note in self.notes] or [0.0])

    def pitch_range(self):
        if not self.notes:
            return LOW_PITCH, HIGH_PITCH
        return min(note.pitch for note in self.notes), max(note.pitch for note in self.notes)

    # ---- 撤销 / 重做 ----

    def snapshot(self):
        return [(note.start, note.dur, note.pitch) for note in self.notes]

    @staticmethod
    def _from(snapshot):
        return [Note(start, dur, pitch) for start, dur, pitch in snapshot]

    def mark(self):
        """动手改之前先存一份，供 Ctrl+Z 回退。"""
        self._undo.append(self.snapshot())
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)
        self._redo = []
        self.dirty = True
        return True

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self.snapshot())
        self.notes = self._from(self._undo.pop())
        self.dirty = True
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self.snapshot())
        self.notes = self._from(self._redo.pop())
        self.dirty = True
        return True

# ============ 读 / 存文件 ============

def midi_bpm(path, default=120.0):
    """从 midi 里读第一个速度标记，没有就用 120。"""
    try:
        import mido
        for track in mido.MidiFile(path).tracks:
            for message in track:
                if message.type == 'set_tempo':
                    return round(60.0 * 1e6 / message.tempo, 3)
    except Exception:                    # 坏文件 / 没装 mido 都当默认速度
        pass
    return default


def midi_tracks(path):
    """这个 midi 有哪几条音轨（给「自己选音轨」用）。"""
    return midi_analyze.describe_tracks(midi_analyze.load(path))


def read_midi(path, track_index=None):
    """
    读一个 midi，挑出（或指定）一条主旋律，返回 (Score, 说明文字)。

    track_index=None 交给 midi_analyze 自动挑；分析出来的谱本来就是单音的，
    这里再压一遍尾巴只是保险。
    """
    analysis = midi_analyze.analyze(path, track_index)
    notes = [Note(note.start, max(note.duration, MIN_DUR), note.pitch)
             for note in analysis.notes]
    notes, trimmed = mono_notes(notes)
    score = Score(notes, tonic=analysis.tonic, bpm=midi_bpm(path), path=path,
                  track_index=analysis.track_index, track_name=analysis.track_name)
    bits = ['音轨 %d「%s」' % (analysis.track_index, analysis.track_name or '未命名'),
            '%d 个音' % len(notes),
            '主音 %s（%d）' % (midi_analyze.pitch_name(analysis.tonic), analysis.tonic),
            '速度 %g BPM' % score.bpm]
    if analysis.cleanup:
        bits.append(analysis.cleanup)
    if trimmed:
        bits.append('顺手剪短了 %d 个尾巴，保证单音' % trimmed)
    return score, '；'.join(bits)


def save_project(score, path):
    """谱面 -> .mproj（其实就是个 json），没改完也能存。"""
    data = {
        'format': PROJECT_FORMAT,
        'version': PROJECT_VERSION,
        'saved': time.strftime('%Y-%m-%d %H:%M:%S'),
        'tonic': score.tonic,
        'bpm': score.bpm,
        'source': score.path,
        'track_index': score.track_index,
        'track_name': score.track_name,
        'notes': [[round(note.start, 4), round(note.dur, 4), int(note.pitch)]
                  for note in score.ordered()],
    }
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    score.dirty = False
    return path


def load_project(path):
    """读回 .mproj，返回 (Score, 说明文字)。"""
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise midi_analyze.MidiError('工程文件读不动：%s' % error)
    if data.get('format') != PROJECT_FORMAT:
        raise midi_analyze.MidiError('这不是本编辑器的工程文件：%s' % os.path.basename(path))
    notes = [Note(start, dur, pitch) for start, dur, pitch in data.get('notes') or []]
    score = Score(notes, tonic=int(data.get('tonic', 60)), bpm=float(data.get('bpm', 120.0)),
                  path=data.get('source') or '', track_index=int(data.get('track_index', -1)),
                  track_name=data.get('track_name') or '')
    score.dirty = False
    return score, '工程文件：%d 个音，主音 %d' % (len(notes), score.tonic)

# ============ 钢琴卷帘 ============

# 缩放时可选的网格步长（秒），取第一个够宽的
GRID_STEPS = (0.05, 0.1, 0.2, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
# 黑键在八度里的位置（画背景色 / 键盘用）
BLACK_KEYS = (1, 3, 6, 8, 10)


class PianoRoll(QWidget):
    """
    钢琴卷帘：横着是时间，竖着是音高，方块就是音。

    鼠标直接改：双击空白加音、右键音块删音、按住左键拖动、拖右边缘改长短。
    """

    edited = Signal()                    # 谱面变了（窗口去刷新计数 / 重新合成）
    played = Signal(float)               # 光标被挪到第几秒
    audition = Signal(object)            # 想单独听这个音（双击音块）

    def __init__(self, score=None, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.score = score or Score()
        self.pps = 80.0                  # 每秒多少像素（缩放）
        self.offset = 0.0                # 左边第一列是第几秒
        self.top_pitch = 84              # 最上面一行是哪个音
        self.row_h = 11.0                # 一行多高（1 个半音）
        self.playhead = 0.0              # 光标（秒）
        self.selected = []               # 选中的音（就是 Score.notes 里的对象）
        self.new_dur = DEFAULT_DUR       # 双击加音时默认多长
        self.snap = True                 # 吸附网格
        self._fitted = False             # 还没按真实宽度缩放过（见 resizeEvent）
        self._drag = None
        self._hbar = QScrollBar(Qt.Horizontal, self)
        self._vbar = QScrollBar(Qt.Vertical, self)
        self._hbar.setSingleStep(24)
        self._vbar.setSingleStep(1)
        self._hbar.valueChanged.connect(self._on_hbar)
        self._vbar.valueChanged.connect(self._on_vbar)

    # ---- 坐标换算 ----

    def roll_rect(self):
        """真正画卷帘的那块区域（去掉上面的尺、左边的键盘、右边和下边的滚动条）。"""
        return QRectF(KEYS_W, RULER_H, max(1.0, self.width() - KEYS_W - SB_W),
                      max(1.0, self.height() - RULER_H - SB_W))

    def rows(self):
        """可视区能放下几行（几个半音）。"""
        return max(1, int(self.roll_rect().height() / self.row_h))

    def x_of(self, seconds):
        return self.roll_rect().left() + (float(seconds) - self.offset) * self.pps

    def t_of(self, x):
        return self.offset + (float(x) - self.roll_rect().left()) / self.pps

    def y_of(self, pitch):
        return self.roll_rect().top() + (self.top_pitch - int(pitch)) * self.row_h

    def pitch_of(self, y):
        return self.top_pitch - int((float(y) - self.roll_rect().top()) // self.row_h)

    def grid_step(self):
        """网格线间隔（秒）：跟着缩放走，太密了就换大一级。"""
        for step in GRID_STEPS:
            if step * self.pps >= 46.0:
                return step
        return GRID_STEPS[-1]

    def snap_time(self, seconds):
        """按网格对齐（关掉吸附就原样返回）。"""
        if not self.snap:
            return max(0.0, float(seconds))
        step = self.grid_step()
        return max(0.0, round(float(seconds) / step) * step)

    # ---- 滚动 ----

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width, height = self.width(), self.height()
        self._hbar.setGeometry(KEYS_W, max(0, height - SB_W),
                               max(0, width - KEYS_W - SB_W), SB_W)
        self._vbar.setGeometry(max(0, width - SB_W), RULER_H, SB_W,
                               max(0, height - RULER_H - SB_W))
        self.sync_bars()
        if not self._fitted:
            # 窗口还没显示时控件只有个默认大小，这时候「适应窗口」算出来的缩放是错的；
            # 等第一次拿到真实宽度再算一次。
            self.fit()

    def sync_bars(self):
        """把滚动条的取值范围跟当前缩放 / 谱面对齐。"""
        roll = self.roll_rect()
        content = max(self.score.total() * self.pps, 1.0)
        self._hbar.setRange(0, max(0, int(content - roll.width())))
        self._hbar.setPageStep(int(roll.width()))
        self._hbar.setSingleStep(max(1, int(self.grid_step() * self.pps / 2.0)))
        self._hbar.setValue(int(self.offset * self.pps))
        self._hbar.setVisible(content > roll.width() + 1)
        rows = self.rows()
        self._vbar.setRange(0, max(0, 128 - rows))
        self._vbar.setPageStep(rows)
        self._vbar.setValue(max(0, min(127, 127 - self.top_pitch)))
        self._vbar.setVisible(rows < 128)

    def _on_hbar(self, value):
        self.offset = max(0.0, value / self.pps)
        self.update()

    def _on_vbar(self, value):
        self.top_pitch = int(127 - max(0, min(127, value)))
        self.update()

    def zoom(self, factor, around=None):
        """以 around（秒）为锚点缩放：鼠标指哪儿就放大哪儿。"""
        if around is None:
            around = self.playhead
        before = self.pps
        self.pps = max(2.0, min(4000.0, self.pps * float(factor)))
        if abs(self.pps - before) > 1e-9:
            self.offset = max(0.0, around - (around - self.offset) * (before / self.pps))
        self.sync_bars()
        self.update()

    def fit(self):
        """整首歌刚好铺满可视区，纵向对准音域。"""
        roll = self.roll_rect()
        self._fitted = roll.width() > 120.0     # 太窄说明还没布局完，回头再试
        total = max(self.score.total(), 0.5)
        self.pps = max(2.0, min(4000.0, max(roll.width() - 8.0, 60.0) / total))
        self.offset = 0.0
        low, high = self.score.pitch_range()
        rows = self.rows()
        top = int(round((low + high) / 2.0 + rows / 2.0 - 1))
        self.top_pitch = max(rows - 1, min(127, top))
        self.sync_bars()
        self.update()

    # ---- 音符操作 ----

    def note_rect(self, note):
        return QRectF(self.x_of(note.start), self.y_of(note.pitch),
                      max(2.0, note.dur * self.pps), max(4.0, self.row_h - 1.0))

    def hit(self, point):
        """点在哪个音上（后画的在上，所以倒着找）。"""
        for note in reversed(self.score.ordered()):
            if self.note_rect(note).contains(point):
                return note
        return None

    def edge_hit(self, point):
        """点的是不是「右边缘」—— 用来分辨拖动位置还是拖长度。"""
        note = self.hit(point)
        if note is None:
            return None
        rect = self.note_rect(note)
        return note if rect.right() - float(point.x()) <= EDGE_PX else None

    def changed(self):
        self.sync_bars()
        self.update()
        self.edited.emit()

    def add_note(self, start, pitch, dur=None):
        """在 start 秒、pitch 这个音高加一个音，返回新音符。"""
        if dur is None:
            dur = self.new_dur
        start = max(0.0, float(start))
        pitch = max(LOW_PITCH, min(HIGH_PITCH, int(pitch)))
        dur = max(MIN_DUR, float(dur))
        self.score.mark()
        note = Note(start, dur, pitch)
        self.score.notes.append(note)
        self.selected = [note]
        self.changed()
        return note

    def delete_note(self, note):
        if note not in self.score.notes:
            return False
        self.score.mark()
        self.score.notes.remove(note)
        if note in self.selected:
            self.selected.remove(note)
        self.changed()
        return True

    def delete_selected(self):
        """删掉选中的那些音，返回删了几个（整批只算一步撤销）。"""
        if not self.selected:
            return 0
        self.score.mark()
        count = 0
        for note in list(self.selected):
            if note in self.score.notes:
                self.score.notes.remove(note)
                count += 1
        self.selected = []
        self.changed()
        return count

    def set_playhead(self, seconds):
        self.playhead = max(0.0, min(float(seconds), max(self.score.total(), 0.0)))

    def show_time(self, seconds):
        """播放时把光标留在可视区里（跑到边上了就跟着滚一点）。"""
        self.playhead = max(0.0, float(seconds))
        roll = self.roll_rect()
        x = self.x_of(self.playhead)
        if x > roll.right() - 40.0 or x < roll.left():
            self.offset = max(0.0, self.playhead - roll.width() / self.pps * 0.15)
            self.sync_bars()
        self.update()

    # ---- 鼠标 ----

    def mousePressEvent(self, event):
        pos = QPointF(event.position())
        roll = self.roll_rect()
        if event.button() == Qt.LeftButton and pos.y() < roll.top():
            self.set_playhead(self.t_of(pos.x()))       # 点时间尺 = 把光标挪过去
            self.played.emit(self.playhead)
            self.update()
            return
        if event.button() == Qt.RightButton:
            note = self.hit(pos)
            if note is not None:
                self.delete_note(note)                  # 右键音块 = 删掉
            return
        if event.button() != Qt.LeftButton:
            return
        self.setFocus()
        note = self.hit(pos)
        if note is None:
            self.selected = []
            self.set_playhead(self.t_of(pos.x()))
            self.played.emit(self.playhead)
            self._drag = {'kind': 'seek'}
            self.update()
            return
        if event.modifiers() & Qt.ShiftModifier:
            if note not in self.selected:
                self.selected.append(note)
        else:
            self.selected = [note]
        if self.edge_hit(pos) is not None:
            self._drag = {'kind': 'resize', 'note': note, 'dur': note.dur, 'marked': False}
        else:
            self._drag = {'kind': 'move', 'grab': self.t_of(pos.x()),
                          'pgrab': self.pitch_of(pos.y()), 'marked': False,
                          'orig': [(item, item.start, item.pitch) for item in self.selected]}
        self.update()

    def mouseMoveEvent(self, event):
        pos = QPointF(event.position())
        drag = self._drag
        if drag is None:
            self.setCursor(Qt.SizeHorCursor if self.edge_hit(pos) is not None
                           else Qt.ArrowCursor)
            return
        if drag['kind'] == 'seek':
            self.set_playhead(self.t_of(pos.x()))
            self.played.emit(self.playhead)
            self.update()
            return
        if not drag['marked']:                          # 真动了才记一步撤销
            self.score.mark()
            drag['marked'] = True
        if drag['kind'] == 'resize':
            note = drag['note']
            note.dur = max(MIN_DUR, self.snap_time(self.t_of(pos.x())) - note.start)
        else:
            shift = self.t_of(pos.x()) - drag['grab']
            delta = drag['pgrab'] - self.pitch_of(pos.y())
            for note, start, pitch in drag['orig']:
                note.start = max(0.0, self.snap_time(start + shift))
                note.pitch = max(LOW_PITCH, min(HIGH_PITCH, int(pitch + delta)))
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag is not None and self._drag.get('kind') != 'seek':
            self.changed()
        self._drag = None
        self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = QPointF(event.position())
        note = self.hit(pos)
        if note is not None:
            self.selected = [note]                      # 双击音块 = 单独听一下
            self.update()
            self.audition.emit(note)
            return
        self.add_note(self.snap_time(self.t_of(pos.x())), self.pitch_of(pos.y()))

    def wheelEvent(self, event):
        delta = event.angleDelta()
        if event.modifiers() & Qt.ControlModifier:      # Ctrl+滚轮 = 缩放
            self.zoom(1.15 if delta.y() > 0 else 1 / 1.15, self.t_of(event.position().x()))
        elif event.modifiers() & Qt.ShiftModifier:      # Shift+滚轮 = 横向滚
            self._hbar.setValue(self._hbar.value() - delta.y())
        elif delta.y():
            self._vbar.setValue(self._vbar.value() - int(delta.y() / 4))
        elif delta.x():
            self._hbar.setValue(self._hbar.value() - int(delta.x() / 4))

    # ---- 键盘 ----

    def _nudge(self, dstart=0.0, dpitch=0, ddur=0.0):
        if not self.selected:
            return
        self.score.mark()
        for note in self.selected:
            note.start = max(0.0, note.start + dstart)
            note.pitch = max(LOW_PITCH, min(HIGH_PITCH, note.pitch + dpitch))
            note.dur = max(MIN_DUR, note.dur + ddur)
        self.changed()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected:
            self.delete_selected()
            return
        if not self.selected:
            super().keyPressEvent(event)
            return
        step = self.grid_step() if self.snap else 0.05
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        if key == Qt.Key_Left:
            self._nudge(-step, 0, step if shift else 0.0)
        elif key == Qt.Key_Right:
            self._nudge(step, 0, step if shift else 0.0)
        elif key == Qt.Key_Up:
            self._nudge(0, 12 if shift else 1)
        elif key == Qt.Key_Down:
            self._nudge(0, -12 if shift else -1)
        elif key == Qt.Key_Home:
            self._nudge(0, 0, 0)
            for note in self.selected:
                note.start = 0.0
            self.changed()
        else:
            super().keyPressEvent(event)

    # ---- 画 ----

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(theme.c('#0d1017')))
        roll = self.roll_rect()
        painter.save()
        painter.setClipRect(roll)
        self._paint_rows(painter, roll)
        self._paint_grid(painter, roll)
        self._paint_notes(painter, roll)
        self._paint_playhead(painter, roll)
        painter.restore()
        self._paint_keys(painter, roll)
        self._paint_ruler(painter, roll)

    def _paint_rows(self, painter, roll):
        """一行一个半音，黑键那几行压暗一点，横着看得清音高。"""
        for index in range(self.rows() + 1):
            pitch = self.top_pitch - index
            if not 0 <= pitch <= 127:
                continue
            dark = (pitch % 12) in BLACK_KEYS
            painter.fillRect(QRectF(roll.left(), self.y_of(pitch), roll.width(), self.row_h),
                             QColor(theme.c('#111620') if dark else theme.c('#171d29')))

    def _paint_grid(self, painter, roll):
        step = self.grid_step()
        first = int(math.floor(self.offset / step)) * step
        index = int(round(first / step))
        thin = QPen(QColor(theme.c('#1c2230')))
        bold = QPen(QColor(theme.c('#2a3450')))
        while True:
            x = self.x_of(index * step)
            if x >= roll.right():
                break
            if x >= roll.left() - 1.0:
                painter.setPen(bold if index % 4 == 0 else thin)
                painter.drawLine(QPointF(x, roll.top()), QPointF(x, roll.bottom()))
            index += 1
        painter.setPen(QPen(QColor(theme.c('#222b3d'))))
        for row in range(self.rows() + 1):
            pitch = self.top_pitch - row
            if 0 <= pitch <= 127 and pitch % 12 == 0:
                y = self.y_of(pitch)
                painter.drawLine(QPointF(roll.left(), y), QPointF(roll.right(), y))

    def _paint_notes(self, painter, roll):
        tonic = self.score.tonic
        font = QFont(self.font())
        font.setPointSizeF(8.0)
        painter.setFont(font)
        for note in self.score.ordered():
            rect = self.note_rect(note)
            if rect.right() < roll.left() or rect.left() > roll.right():
                continue
            if rect.bottom() < roll.top() - 1.0 or rect.top() > roll.bottom() + 1.0:
                continue
            token = token_of(note.pitch, tonic)
            base, light = NOTE_COLORS.get(color_key(token), NOTE_COLORS[''])
            body = rect.adjusted(0.0, 0.0, -1.0, 0.0)
            path = QPainterPath()
            path.addRoundedRect(body, 3.0, 3.0)
            grad = QLinearGradient(body.topLeft(), body.bottomLeft())
            grad.setColorAt(0.0, QColor(light))
            grad.setColorAt(1.0, QColor(base))
            painter.setBrush(QBrush(grad))
            if note in self.selected:
                painter.setPen(QPen(QColor(theme.c('#ffffff')), 1.6))
            else:
                painter.setPen(QPen(QColor(base).darker(150), 1.0))
            painter.drawPath(path)
            if body.width() > 24.0 and body.height() > 8.0:
                painter.setPen(QPen(QColor(theme.c('#0b0e14'))))
                painter.drawText(body.adjusted(4.0, 0.0, -3.0, 0.0),
                                 Qt.AlignLeft | Qt.AlignVCenter, token.text)

    def _paint_playhead(self, painter, roll):
        x = self.x_of(self.playhead)
        if not roll.left() <= x <= roll.right():
            return
        painter.setPen(QPen(QColor(theme.c('#ff6b6b')), 1.4))
        painter.drawLine(QPointF(x, roll.top()), QPointF(x, roll.bottom()))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(theme.c('#ff6b6b'))))
        painter.drawEllipse(QPointF(x, roll.top() + 3.0), 4.0, 4.0)

    def _paint_keys(self, painter, roll):
        painter.save()
        painter.setClipRect(QRectF(0.0, roll.top(), KEYS_W, roll.height()))
        painter.fillRect(QRectF(0.0, roll.top(), KEYS_W, roll.height()), QColor(theme.c('#0b0e14')))
        font = QFont(self.font())
        font.setPointSizeF(7.5)
        painter.setFont(font)
        for row in range(self.rows() + 1):
            pitch = self.top_pitch - row
            if not 0 <= pitch <= 127:
                continue
            rect = QRectF(0.0, self.y_of(pitch), KEYS_W, self.row_h)
            dark = (pitch % 12) in BLACK_KEYS
            painter.fillRect(rect, QColor(theme.c('#1a2030') if dark else theme.c('#c3ccdc')))
            if pitch % 12 == 0:
                painter.setPen(QPen(QColor(theme.c('#39415a'))))
                painter.drawText(rect.adjusted(5.0, 0.0, -4.0, 0.0),
                                 Qt.AlignLeft | Qt.AlignVCenter,
                                 midi_analyze.pitch_name(pitch))
        painter.restore()

    def _paint_ruler(self, painter, roll):
        painter.save()
        painter.fillRect(QRectF(0.0, 0.0, self.width(), RULER_H), QColor(theme.c('#11151d')))
        painter.setClipRect(QRectF(roll.left(), 0.0, roll.width(), RULER_H))
        font = QFont(self.font())
        font.setPointSizeF(7.5)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.c('#6f7787'))))
        mark = self.grid_step() * 2.0
        index = int(math.floor(self.offset / mark))
        while True:
            x = self.x_of(index * mark)
            if x >= roll.right():
                break
            if x >= roll.left() - 1.0:
                painter.drawLine(QPointF(x, RULER_H - 6.0), QPointF(x, RULER_H))
                painter.drawText(QRectF(x + 4.0, 0.0, 64.0, RULER_H - 4.0),
                                 Qt.AlignLeft | Qt.AlignVCenter,
                                 format_time(index * mark, True))
            index += 1
        painter.restore()
        painter.setPen(QPen(QColor(theme.c('#232937'))))
        painter.drawLine(QPointF(0.0, RULER_H), QPointF(self.width(), RULER_H))

# ============ 主窗口 ============

class EditorWindow(QWidget):
    """编辑器主窗口：上面一排按钮，中间卷帘，下面进度条 + 日志。"""

    audio_ready = Signal(str)            # 后台合成完了（跨线程回主线程用）

    def __init__(self, parent=None, embedded=False):
        """
        embedded=True 时当作主程序里的一个页面用（塞进标签页）：

        * 不再自己改窗口标题（那会改到主程序的标题栏上）；
        * 关窗口不再弹「要保存吗」（改由主程序在退出前调 ask_save()）；
        * 快捷键只在编辑器自己拿到焦点时生效，免得抢主程序那边的按键。

        单独跑（python editor.py 或者 AutoPlayEditor.exe）时用默认的 embedded=False。
        """
        super().__init__(parent)
        self.embedded = embedded
        self.on_export = None            # 导出 midi 之后叫一声（主程序拿它自动载入）
        self.setObjectName('root')
        if not embedded:
            self.setWindowTitle('%s v%s' % (APP_TITLE, APP_VERSION))
            self.resize(1180, 740)
            # 单独跑（AutoPlayEditor.exe）的时候要能最大化：编辑工程铺满整块屏幕，
            # 卷帘看得多。QWidget 当顶层窗口默认不带最大化按钮，这儿明确要一个。
            self.setWindowFlags(Qt.WindowType.Window
                                | Qt.WindowType.WindowSystemMenuHint
                                | Qt.WindowType.WindowMinimizeButtonHint
                                | Qt.WindowType.WindowMaximizeButtonHint
                                | Qt.WindowType.WindowCloseButtonHint)
        self.apply_style()
        self.score = Score()
        self.preview = preview.Preview(self.log)
        self.playing = False
        self._pending_start = 0.0
        self._played_from = 0.0
        self._t0 = 0.0
        self._seeking = False
        self._build_ui()
        self._bind_shortcuts()
        self.audio_ready.connect(self._on_audio_ready)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._rebuild = QTimer(self)
        self._rebuild.setSingleShot(True)
        self._rebuild.setInterval(320)
        self._rebuild.timeout.connect(self._rebuild_playback)
        # 自动保存：改过没存就悄悄存一份（被强杀 / 崩了，下次启动自己摆回来）
        self._autosave_sig = None
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_TICK_MS)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._autosave_timer.start()
        self._refresh_buttons()
        self._refresh_counter()
        self._refresh_max_button()          # 建出来的时候主窗口可能已经最大化了
        self.log('%s v%s 就绪。打开一个 midi 或者工程文件开工，Ctrl+O 也能打开。'
                 % (APP_TITLE, APP_VERSION))
        if not preview.Preview.available():
            self.log('注意：这台机器没有 winsound，试听和播放都用不了（只在 Windows 上能听）。')

    # ---- 界面 ----

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(APP_TITLE)
        title.setObjectName('title')
        head.addWidget(title)
        self.file_label = QLabel('还没有打开文件')
        self.file_label.setObjectName('value')
        head.addWidget(self.file_label)
        head.addStretch(1)
        self.open_button = QPushButton('打开 MIDI…')
        self.open_button.setObjectName('primary')
        self.open_button.clicked.connect(lambda: self.open_midi())
        head.addWidget(self.open_button)
        self.project_button = QPushButton('打开工程…')
        self.project_button.clicked.connect(lambda: self.open_project())
        head.addWidget(self.project_button)
        self.save_button = QPushButton('保存工程')
        self.save_button.clicked.connect(lambda: self.save_project_as())
        head.addWidget(self.save_button)
        self.export_button = QPushButton('导出 MIDI…')
        self.export_button.clicked.connect(lambda: self.export_midi())
        head.addWidget(self.export_button)
        # 最大化 / 还原：编辑工程时铺满整块屏幕，卷帘能多看一大截。
        # 嵌在主程序里的时候动的其实是主窗口那一层（见 toggle_maximize）。
        self.max_button = QPushButton('□')
        self.max_button.setObjectName('maxButton')      # 跟主窗口右上角那个同一套样式
        self.max_button.setFixedSize(32, 32)
        self.max_button.setToolTip('最大化 / 还原（也可以按 F11）')
        self.max_button.clicked.connect(self.toggle_maximize)
        head.addWidget(self.max_button)
        root.addLayout(head)

        tools = QHBoxLayout()
        tools.addWidget(QLabel('主音'))
        self.tonic_spin = QSpinBox()
        self.tonic_spin.setRange(0, 127)
        self.tonic_spin.setValue(60)
        self.tonic_spin.setToolTip('简谱上的「1」是哪个音；改它只影响记号显示和导出对照')
        self.tonic_spin.valueChanged.connect(self._on_tonic)
        tools.addWidget(self.tonic_spin)
        self.tonic_label = QLabel('C4（60）')
        self.tonic_label.setObjectName('value')
        tools.addWidget(self.tonic_label)
        tools.addSpacing(10)
        tools.addWidget(QLabel('新音时长'))
        self.dur_spin = QDoubleSpinBox()
        self.dur_spin.setRange(MIN_DUR, 30.0)
        self.dur_spin.setDecimals(2)
        self.dur_spin.setSingleStep(0.05)
        self.dur_spin.setValue(DEFAULT_DUR)
        self.dur_spin.setSuffix(' 秒')
        self.dur_spin.setToolTip('双击空白 / 点「加音」时新音有多长')
        self.dur_spin.valueChanged.connect(self._on_add_dur)
        tools.addWidget(self.dur_spin)
        self.snap_check = QCheckBox('吸附网格')
        self.snap_check.setChecked(True)
        self.snap_check.setToolTip('拖动时对齐到网格；按住 Alt 拖动可临时取消')
        self.snap_check.toggled.connect(self._on_snap)
        tools.addWidget(self.snap_check)
        tools.addStretch(1)
        self.play_button = QPushButton('♪ 播放')
        self.play_button.setObjectName('primary')
        self.play_button.clicked.connect(self.toggle_play)
        tools.addWidget(self.play_button)
        self.stop_button = QPushButton('■ 停止')
        self.stop_button.clicked.connect(lambda: self.stop_play())
        tools.addWidget(self.stop_button)
        root.addLayout(tools)

        edits = QHBoxLayout()
        buttons = {}
        for key, text, slot, tip in (
                ('add', '＋ 加音', self.add_at_playhead, '在光标处加一个音（双击空白也行）'),
                ('del', '－ 删音', self.delete_selected, '删掉选中的音（点音块选中，Delete 也行）'),
                ('out', '－ 缩小', lambda: self.roll.zoom(1 / 1.3), '横轴缩小（Ctrl+滚轮）'),
                ('in', '＋ 放大', lambda: self.roll.zoom(1.3), '横轴放大（Ctrl+滚轮）'),
                ('fit', '适应窗口', self.fit_view, '缩放回「整首歌刚好铺满」'),
                ('undo', '撤销', self.undo, 'Ctrl+Z'),
                ('redo', '重做', self.redo, 'Ctrl+Y')):
            button = QPushButton(text)
            button.setObjectName('mini')
            button.setToolTip(tip)
            button.clicked.connect(slot)
            edits.addWidget(button)
            buttons[key] = button
        edits.addStretch(1)
        self.undo_button = buttons['undo']
        self.redo_button = buttons['redo']
        root.addLayout(edits)

        self.roll = PianoRoll(self.score, self)
        self.roll.new_dur = DEFAULT_DUR
        self.roll.edited.connect(self._on_edited)
        self.roll.played.connect(self._on_played)
        self.roll.audition.connect(self.play_note)
        root.addWidget(self.roll, 1)

        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setRange(0, 1000)
        self.slider.setValue(0)
        self.slider.sliderPressed.connect(self._on_slider_press)
        self.slider.sliderMoved.connect(self._on_slider_move)
        self.slider.sliderReleased.connect(self._on_slider_release)
        root.addWidget(self.slider)

        info = QHBoxLayout()
        hint = QLabel(PICK_HINT)
        hint.setObjectName('hint')
        hint.setWordWrap(True)
        info.addWidget(hint, 1)
        self.counter = QLabel('0 个音 · 总长 0:00.0')
        self.counter.setObjectName('value')
        info.addWidget(self.counter)
        root.addLayout(info)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(92)
        root.addWidget(self.log_view)

    def _bind_shortcuts(self):
        for keys, slot in (('Space', self.toggle_play),
                           ('Ctrl+Z', self.undo),
                           ('Ctrl+Y', self.redo),
                           ('Ctrl+Shift+Z', self.redo),
                           ('Ctrl+O', lambda: self.open_midi()),
                           ('Ctrl+S', self.save_project_as),
                           ('Ctrl+E', self.export_midi),
                           ('Ctrl+A', self.select_all)):
            shortcut = QShortcut(QKeySequence(keys), self)
            if self.embedded:
                # 塞在主程序里的时候，只有焦点在编辑器这一页才响应 ——
                # 否则在「演奏」页按空格会跑去播放编辑器里的谱
                shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        if not self.embedded:
            # 塞在主程序里的时候，F11 归主窗口管（它才是那个要被最大化的窗口）
            full = QShortcut(QKeySequence('F11'), self)
            full.activated.connect(self.toggle_maximize)
            self._max_shortcut = full

    def toggle_maximize(self):
        """
        最大化 / 还原。

        嵌在主程序里的时候自己不是个窗口，要动的是主窗口那一层 —— 所以取
        window()（没有父窗口时 window() 就是自己，正好两种情形都对）。
        """
        top = self.window() if self.embedded else self
        if top.isMaximized():
            top.showNormal()
        else:
            top.showMaximized()

    def _repolish(self, widget):
        """换了 objectName 之后让 Qt 按新样式重画一遍。"""
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    # ---- 状态刷新 ----

    def log(self, text):
        stamp = time.strftime('%H:%M:%S')
        line = '[%s] %s' % (stamp, text)
        view = getattr(self, 'log_view', None)
        if view is None:
            print(line)
            return
        view.append(line)

    def _refresh_title(self):
        if self.embedded:
            return                       # 标题归主程序管，别去动它
        name = os.path.basename(self.score.path) if self.score.path else '未命名'
        self.setWindowTitle('%s%s — %s v%s' % ('*' if self.score.dirty else '',
                                               name, APP_TITLE, APP_VERSION))

    def _refresh_buttons(self):
        if not hasattr(self, 'play_button'):
            return
        ready = bool(self.score.notes)
        self.play_button.setText('■ 停止' if self.playing else '♪ 播放')
        self.play_button.setObjectName('playing' if self.playing else 'primary')
        self._repolish(self.play_button)
        self.play_button.setEnabled(ready and preview.Preview.available())
        self.stop_button.setEnabled(self.playing)
        self.undo_button.setEnabled(self.score.can_undo())
        self.redo_button.setEnabled(self.score.can_redo())
        self.save_button.setEnabled(ready)
        self.export_button.setEnabled(ready)

    def _refresh_counter(self):
        if not hasattr(self, 'counter'):
            return
        self.counter.setText('%d 个音 · 总长 %s'
                             % (len(self.score.notes), format_time(self.score.total(), True)))

    # ---- 打开 / 保存 ----

    def open_midi(self, path=None):
        """选一个 midi 打开；有好几条音轨时会先问用哪条。"""
        if not path:
            path, _filter = QFileDialog.getOpenFileName(self, '打开 MIDI', '', MIDI_FILTER)
        if not path:
            return False
        return self.open_path(path)

    def _ask_track(self, tracks, auto_index=-1):
        """多音轨文件问一句：自动挑，还是自己指一条。返回音轨号，取消返回 None。"""
        items = ['自动挑选（推荐）']
        for info in tracks:
            mark = '，推荐' if info.index == auto_index else ''
            items.append('%d：%s（%d 个音%s）'
                         % (info.index, info.name or '未命名', info.note_count, mark))
        current = 0
        for position, info in enumerate(tracks):
            if info.index == auto_index:
                current = position + 1
        choice, ok = QInputDialog.getItem(
            self, '选音轨', '这个文件有 %d 条音轨，用哪条当主旋律？' % len(tracks),
            items, current, False)
        if not ok:
            return None
        position = items.index(choice)
        return -1 if position == 0 else tracks[position - 1].index

    def open_path(self, path, track_index=None, ask=True):
        """打开一个 midi。track_index=None 表示自动挑（多音轨且 ask 时先问一句）。"""
        try:
            midi = midi_analyze.load(path)
            tracks = midi_analyze.describe_tracks(midi)
        except midi_analyze.MidiError as error:
            self.log('打不开：%s' % error)
            return False
        if track_index is None and ask and len(tracks) > 1:
            auto_index = -1
            try:
                auto_index, _reason = midi_analyze.select_melody_track(tracks)
            except midi_analyze.MidiError:
                pass
            chosen = self._ask_track(tracks, auto_index)
            if chosen is None:
                self.log('取消了，没打开')
                return False
            if chosen >= 0:
                track_index = chosen
        try:
            score, info = read_midi(path, track_index)
        except midi_analyze.MidiError as error:
            self.log('读不出来：%s' % error)
            return False
        self.set_score(score, info)
        return True

    def open_project(self, path=None, unsaved=False):
        """
        打开 .mproj 工程文件（接着上次改）。

        unsaved=True 是给「自动保存恢复」用的：这份谱面还没真正存成工程，
        标成「改过没存」，才不会被下一次自动保存当成「已经存过了」给删掉。
        """
        if not path:
            path, _filter = QFileDialog.getOpenFileName(self, '打开工程', '', PROJECT_FILTER)
        if not path:
            return False
        try:
            score, info = load_project(path)
        except midi_analyze.MidiError as error:
            self.log(str(error))
            return False
        self.set_score(score, info)
        self.score.dirty = bool(unsaved)
        self._refresh_title()
        return True

    def set_score(self, score, info=''):
        """把窗口切到这份谱上。"""
        self.stop_play()
        self.score = score
        self.tonic_spin.blockSignals(True)
        self.tonic_spin.setValue(score.tonic)
        self.tonic_spin.blockSignals(False)
        self.tonic_label.setText('%s（%d）' % (midi_analyze.pitch_name(score.tonic), score.tonic))
        self.roll.score = score
        self.roll.selected = []
        self.roll.playhead = 0.0
        self.roll.offset = 0.0
        self.roll.fit()
        self.preview.stop()
        self.preview.samples = None
        self.preview.total = 0.0
        self._set_slider(0.0)
        name = os.path.basename(score.path) if score.path else '未命名'
        self.file_label.setText(name if score.track_index < 0
                                else '%s · 音轨 %d' % (name, score.track_index))
        self._refresh_buttons()
        self._refresh_counter()
        self._refresh_title()
        if info:
            self.log(info)

    def save_project_as(self):
        """存成 .mproj —— 没改完也能存，下次接着改。"""
        if not self.score.notes:
            self.log('谱面是空的，先打开个文件吧')
            return False
        base = os.path.basename(self.score.path) if self.score.path else 'MIDI工程'
        guess = os.path.splitext(base)[0] + PROJECT_SUFFIX
        path, _filter = QFileDialog.getSaveFileName(self, '保存工程', guess, PROJECT_FILTER)
        if not path:
            return False
        if not path.lower().endswith(PROJECT_SUFFIX):
            path += PROJECT_SUFFIX
        try:
            save_project(self.score, path)
        except OSError as error:
            self.log('存不了：%s' % error)
            return False
        self.log('工程存好了：%s（%d 个音）' % (path, len(self.score.notes)))
        if self._autosave_sig is not None:            # 那份底是我存的才由我来撤
            autosave.drop(autosave.key_of(self.score))
            self._autosave_sig = None
        self._refresh_title()
        return True

    def export_midi(self):
        """导出单音 midi（尾巴互相压着的先剪短，游戏那边只认单音）。"""
        if not self.score.notes:
            self.log('谱面是空的，导不出东西')
            return False
        base = os.path.basename(self.score.path) if self.score.path else 'melody'
        guess = os.path.splitext(base)[0] + '.mid'
        path, _filter = QFileDialog.getSaveFileName(self, '导出 MIDI', guess, MIDI_FILTER)
        if not path:
            return False
        if not os.path.splitext(path)[1]:
            path += '.mid'
        notes, trimmed = mono_notes(self.score.notes)
        try:
            audio2midi.write_midi([(note.start, note.dur, note.pitch) for note in notes],
                                  path, bpm=self.score.bpm, name='melody')
        except Exception as error:               # mido 不给力 / 路径写不进去
            self.log('导出失败：%s' % error)
            return False
        self.log('导出好了：%s（%d 个音）' % (path, len(notes)))
        if trimmed:
            self.log('有 %d 个音的尾巴压到后一个音上了，已经剪短，保证同一时刻只有一个音' % trimmed)
        if self.on_export is not None:
            self.on_export(path)         # 主程序接过去直接载入，不用再手动选一遍
        return True

    # ---- 改谱面 ----

    def _on_edited(self):
        self._refresh_counter()
        self._refresh_buttons()
        self._refresh_title()
        if self.playing:
            self._rebuild.start()            # 改完就听新的（等手停一下再重新合成）

    # ---- 自动保存 ----

    def _autosave_tick(self):
        """
        隔几秒看一眼：改过没存就悄悄存一份，存过了就把那一份撤掉。

        故意做成「定时看一眼」而不是「每次改动都写」—— 改谱面一秒钟能动几十次，
        拖一下就写一次盘太糟践硬盘了。内容没变的话连写都不写（比对指纹）。
        """
        score = getattr(self, 'score', None)
        if score is None:
            return
        try:
            if not score.notes or not score.dirty:
                # 存过工程 / 刚载入 / 空了：这一份不用留底了
                if self._autosave_sig is not None:
                    autosave.drop(autosave.key_of(score))
                    self._autosave_sig = None
                return
            sig = autosave.signature(score)
            if sig == self._autosave_sig:
                return                        # 跟上一次写的一模一样，别白写
            if autosave.dump(score):
                self._autosave_sig = sig
        except Exception:                     # 自动保存绝不能把界面搞崩
            pass

    def _refresh_max_button(self):
        """右上角那个按钮：按状态换字 / 换配色（嵌在主程序里时看的是主窗口）。"""
        button = getattr(self, 'max_button', None)
        if button is None:
            return
        top = self.window() if self.embedded else self
        maximized = bool(top.isMaximized())
        button.setText('▣' if maximized else '□')
        button.setToolTip('还原窗口（也可以按 F11）' if maximized
                          else '最大化 / 还原（也可以按 F11）')
        button.setProperty('maxed', maximized)
        self._repolish(button)

    def changeEvent(self, event):
        """窗口最大化 / 还原了：右上角按钮跟着变。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._refresh_max_button()

    def _on_played(self, seconds):
        self._set_slider(seconds)

    def _on_tonic(self, value):
        if not hasattr(self, 'roll'):
            return
        self.score.tonic = int(value)
        self.score.dirty = True
        self.tonic_label.setText('%s（%d）' % (midi_analyze.pitch_name(int(value)), int(value)))
        self._refresh_title()
        self.roll.update()

    def _on_add_dur(self, value):
        if hasattr(self, 'roll'):
            self.roll.new_dur = float(value)

    def _on_snap(self, state):
        if hasattr(self, 'roll'):
            self.roll.snap = bool(state)

    def add_at_playhead(self):
        """在光标处加一个音，音高沿用当前选中的那个（没选就取屏幕中间）。"""
        if self.roll.selected:
            pitch = self.roll.selected[-1].pitch
        else:
            pitch = self.roll.top_pitch - self.roll.rows() // 2
        note = self.roll.add_note(self.roll.playhead, pitch)
        self.log('加了一个音：%s，%s 起，%.2f 秒'
                 % (token_of(note.pitch, self.score.tonic).text,
                    format_time(note.start, True), note.dur))

    def delete_selected(self):
        count = self.roll.delete_selected()
        self.log('删了 %d 个音' % count if count else '先点一个音再删')

    def select_all(self):
        self.roll.selected = list(self.score.notes)
        self.roll.update()
        self.log('全选：%d 个音' % len(self.roll.selected))

    def fit_view(self):
        self.roll.fit()
        self.log('缩放：整首歌铺满窗口')

    def undo(self):
        if self.score.undo():
            self.roll.selected = []
            self.roll.sync_bars()
            self.roll.update()
            self._refresh_buttons()
            self._refresh_counter()
            self._refresh_title()
            self.log('撤销一步')
        else:
            self.log('没有可以撤销的了')

    def redo(self):
        if self.score.redo():
            self.roll.selected = []
            self.roll.sync_bars()
            self.roll.update()
            self._refresh_buttons()
            self._refresh_counter()
            self._refresh_title()
            self.log('重做一步')
        else:
            self.log('没有可以重做的了')

    # ---- 试听 / 播放 ----

    def toggle_play(self):
        """♪ 播放 / ■ 停止（空格也行）。"""
        if self.playing:
            self.stop_play()
        else:
            self.start_play()

    def start_play(self):
        if not self.score.notes:
            self.log('谱面是空的，先打开个文件')
            return
        if not preview.Preview.available():
            self.log('这台机器没有 winsound，放不出声')
            return
        self._pending_start = self.roll.playhead
        self.log('合成中…（%d 个音）' % len(self.score.notes))
        threading.Thread(target=self._render_then_play, daemon=True).start()

    def _render_then_play(self):
        """后台合成整首，别把界面卡住（合成完发信号回主线程）。"""
        try:
            notes = [(note.start, note.dur, note.pitch) for note in self.score.ordered()]
            total = self.preview.render_pitches(notes)
            self.audio_ready.emit('ok:%.3f' % total)
        except Exception as error:           # 合成炸了也得说一声，别静悄悄
            self.audio_ready.emit('err:%s' % error)

    def _on_audio_ready(self, message):
        kind, _sep, payload = message.partition(':')
        if kind == 'err':
            self.log('合成失败：%s' % payload)
            return
        self._start_at(self._pending_start)

    def _rebuild_playback(self):
        """改谱面之后接着放：从当前光标那儿重新合成再放。"""
        if not self.playing:
            return
        self._pending_start = self.roll.playhead
        threading.Thread(target=self._render_then_play, daemon=True).start()

    def _start_at(self, seconds):
        started = self.preview.play_from(seconds)
        if started is None:
            self.log('播放失败（没有声音设备？）')
            return
        self._played_from = float(started)
        self._t0 = time.time()
        self.playing = True
        self._timer.start()
        self.roll.show_time(self._played_from)
        self._set_slider(self._played_from)
        self._refresh_buttons()

    def _tick(self):
        if not self.playing or self._seeking:
            return                       # 手指按在进度条上时让它说了算
        position = self._played_from + (time.time() - self._t0)
        if position >= self.preview.total - 0.02:
            self.stop_play(finished=True)
            return
        self.roll.show_time(position)
        self._set_slider(position)

    def stop_play(self, finished=False):
        self._timer.stop()
        self.preview.stop()
        self.playing = False
        self._refresh_buttons()
        if finished:
            self.roll.offset = 0.0
            self.roll.show_time(0.0)
            self.roll.sync_bars()
            self._set_slider(0.0)
            self.log('放完了')
        self.roll.update()

    def play_note(self, note):
        """双击音块：只放这一个音，听听改对没有。"""
        if winsound is None:
            self.log('这台机器放不出声')
            return
        seconds = max(MIN_DUR, min(float(note.dur), AUDITION_MAX))
        try:
            samples, _total = preview.synth_pitches([(0.0, seconds, note.pitch)])
            path = preview.write_wav(samples, preview.new_path(7))
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
        except Exception as error:
            self.log('试听失败：%s' % error)
            return
        self.log('试听 %s（%s），%.2f 秒'
                 % (token_of(note.pitch, self.score.tonic).text,
                    midi_analyze.pitch_name(note.pitch), seconds))

    # ---- 进度条 ----

    def _set_slider(self, seconds):
        total = max(self.score.total(), 0.001)
        value = int(max(0.0, min(1.0, float(seconds) / total)) * 1000)
        self.slider.blockSignals(True)
        self.slider.setValue(value)
        self.slider.blockSignals(False)

    def _on_slider_press(self):
        self._seeking = True
        self.preview.stop()                  # 拖的时候先别响，松手再接着放

    def _on_slider_move(self, value):
        total = max(self.score.total(), 0.001)
        self.roll.show_time(value / 1000.0 * total)

    def _on_slider_release(self):
        self._seeking = False
        seconds = self.roll.playhead
        if self.playing:
            self._start_at(seconds)
        else:
            self.roll.show_time(seconds)

    # ---- 收尾 ----

    def ask_save(self):
        """
        有没保存的改动就问一句。返回 False = 用户反悔了，别关。

        自己关窗口时由 closeEvent 调；塞在主程序里时由主程序的 closeEvent 调。
        """
        if not (self.score.dirty and self.score.notes):
            return True
        answer = QMessageBox.question(
            self, '还没保存', '谱面改过还没存成工程文件，要存一下吗？',
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Yes:
            self.save_project_as()
            return not self.score.dirty      # 存了一半又取消了，当作没存
        return True

    def shutdown(self):
        """停掉正在放的声音和定时器（主程序要关了的时候叫一下）。"""
        self._timer.stop()
        self._rebuild.stop()
        autosave_timer = getattr(self, '_autosave_timer', None)
        if autosave_timer is not None:
            autosave_timer.stop()
        self.preview.stop()

    def apply_style(self):
        """照当前配色主题上一遍样式表（主程序换主题时会叫到这里）。"""
        self.setStyleSheet(theme.paint(STYLE))

    def closeEvent(self, event):
        if not self.ask_save():
            event.ignore()
            return
        self.shutdown()
        event.accept()


def style_sheet():
    """当前配色主题下的编辑器样式表。"""
    return theme.paint(STYLE)


def main(argv=None):
    """独立跑编辑器：python editor.py [某个 .mid / .mproj]"""
    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    app.setApplicationName(APP_TITLE)
    theme.load_all()
    theme.set_current(theme.saved_name())
    app.setStyleSheet(style_sheet())
    window = EditorWindow()
    window.show()
    for extra in argv[1:]:
        if not os.path.exists(extra):
            continue
        if extra.lower().endswith(PROJECT_SUFFIX):
            window.open_project(extra)
        else:
            window.open_path(extra)
        break
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
