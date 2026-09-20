# -*- coding: utf-8 -*-
"""
AutoPlay：选 midi -> 生成简谱 -> 在游戏里自动演奏

    F6 开始演奏    F7 暂停/继续    F8 停止    ESC 退出

界面用 Qt 写（装了 PySide6 或 PyQt6 都能跑），打包成不带黑框的 exe。
点「开始演奏」后主窗口只是让到后面，不隐藏也不最小化（隐藏会让程序切到后台，
热键容易失灵；最小化又会把全屏游戏顶回桌面）。演奏进度由一个独立的半透明
小浮窗贴在屏幕右上角显示，结束 / 停止都不会再把主窗口弹出来。
想调界面就点托盘图标，或者从托盘菜单选「显示窗口」。

    python main.py 1.mid        # 启动时自动转换
    python main.py --no-admin   # 不提权（调试界面用）
"""

import ctypes
import faulthandler
import os
import sys
import theme
import threading
import time
import traceback

try:
    import winreg        # 只用来自查一次：把老版本留下的开机自启项清掉（见 drop_old_autostart）
except ImportError:                                        # pragma: no cover
    winreg = None

try:                                                  # 优先 Qt 官方绑定
    from PySide6.QtCore import QEvent, QPoint, QRectF, QSettings, Qt, QTimer, Signal
    from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QIcon, QKeySequence, QPainter,
                               QPalette, QPen, QPixmap, QShortcut)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                                   QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                                   QListWidget, QListWidgetItem, QMenu, QProgressBar,
                                   QPushButton, QSlider,
                                   QScrollArea, QSizePolicy, QSpinBox, QSystemTrayIcon,
                                   QTabWidget, QTextEdit, QVBoxLayout, QWidget)
except ImportError:                                   # 装了 PyQt6 也行
    from PyQt6.QtCore import QEvent, QPoint, QRectF, QSettings, Qt, QTimer, pyqtSignal as Signal
    from PyQt6.QtGui import (QBrush, QColor, QCursor, QFont, QIcon, QKeySequence, QPainter,
                             QPalette, QPen, QPixmap, QShortcut)
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                                 QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QMenu, QProgressBar,
                                 QPushButton, QSlider,
                                 QScrollArea, QSizePolicy, QSpinBox, QSystemTrayIcon,
                                 QTabWidget, QTextEdit, QVBoxLayout, QWidget)

import audiowatch
import autosave
import edition
import fileassoc
try:                                  # 精简版不带简谱编辑器
    import editor
except Exception:                     # pragma: no cover
    editor = None
import follow
import hotkeys
import jianpu
import library
import midi_analyze
import notesound
import recorder
import single
try:                                  # 精简版不带「音频转 MIDI」
    from mp3midi import audio2midi
except Exception:                     # pragma: no cover
    audio2midi = None
import player
import preview
import relay


# 精简版没有录制，索性把这一行从键位表 / 提示 / 对话框里整个摘掉，
# 免得界面上挂着一个按下去只会说「这一版没有」的键。
if not edition.has_recorder():
    hotkeys.ACTION_ORDER = tuple(a for a in hotkeys.ACTION_ORDER if a != 'record')
    hotkeys.DEFAULT_BINDINGS = dict((a, c) for a, c in hotkeys.DEFAULT_BINDINGS.items()
                                    if a != 'record')

APP_TITLE = 'MIDI 简谱自动演奏' + ('（精简版）' if edition.LITE else '')
APP_VERSION = '1.0'
MIDI_FILTER = 'MIDI 文件 (*.mid *.midi *.kar *.rmi);;所有文件 (*.*)'
# 覆盖模式下不用系统文件框，自己列目录，靠这个认出 MIDI 文件
MIDI_SUFFIX = ('.mid', '.midi', '.kar', '.rmi')
# 音频文件：选到这些就先转成单音 MIDI，再走原来那套流程
AUDIO_SUFFIX = ('.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma', '.opus', '.aiff')
AUDIO_FILTER = ('音频文件 (*.mp3 *.wav *.flac *.ogg *.m4a *.aac *.wma *.opus *.aiff);;'
                '所有文件 (*.*)')
SPEEDS = ['0.5', '0.75', '1.0', '1.25', '1.5', '2.0']
HOLD_MS = (10, 200)          # 「最短按键」可选范围（毫秒）
HOLD_MS_DEFAULT = 35         # 默认一个音最少按 35 毫秒，太短游戏会吞音

# 试听：状态胶囊上的进度多久刷一次（毫秒）
PREVIEW_TICK_MS = 200

# 录制：状态胶囊 / 右上角浮窗上的「录到几个音」多久刷一次（毫秒）
REC_TICK_MS = 250
# 录制导出的 midi 用这个速度。录下来的是相对时长，这个数只是给别的软件一个参考。
REC_BPM = 120.0
# 录制时在桌面上跟着按键出声（游戏里自动静音：那个音游戏自己会放，再叠一个只会打架）
MONITOR_DEFAULT = True
# 录制时把「系统提示音」那一路按住（录制时蹦出来的「叮」多半就是它）
MUTE_DEFAULT = True
# 「控制台」= 主界面那块运行日志：默认不显示，托盘图标右键里随时开
LOG_VISIBLE_DEFAULT = False
# 「谁在响」默认听多久（秒）
DIAG_SECONDS = 10.0
# 「音长吸附」：录完把每个音的时值吸到最近的格子上（毫秒）。'关' = 原样保留
SNAP_CHOICES = ['关', '10 ms', '20 ms', '25 ms', '50 ms', '100 ms']
SNAP_DEFAULT = '10 ms'
# 试听进度条的分辨率：0~1000 千分比（用秒做范围的话，几毫秒一个刻度没意义）
SEEK_RANGE = 1000

# 唤起主界面后「总在最前」保持多久（毫秒）。过了这段时间如果没在用，就撤掉，
# 免得一直压在游戏上面。
TOPMOST_MS = 12000

# 前台兜底：多久检查一次「前台有没有被我们抢走」，以及一次唤起最多还几次。
# 只要浮层贴着游戏，这个勤快一点没关系 —— 它只读一个句柄，不抢任何东西。
GAME_WATCH_MS = 250
GAME_WATCH_TRIES = 240

# 覆盖模式：顶部这块高度内按住可以拖动整个浮层（无边框窗口没有标题栏）
DRAG_H = 52

# 浮层形态整体留一点透明，好让底下的游戏画面露出来（在游戏里唤起时必须这样）。
# 但**编辑器那一页不透明**：那一页信息密、要盯着看色块和数字，半透明只会增加
# 眼睛的负担。见 _overlay_opacity()。
OVERLAY_OPACITY = 0.96
OVERLAY_OPACITY_EDITOR = 1.0

# 托盘菜单里各个热键动作用的名字（和界面上的说法稍微不一样，短一点）
TRAY_HOTKEY_LABELS = {'start': '开始演奏', 'pause': '暂停 / 继续', 'stop': '停止',
                      'show': '显示窗口', 'follow': '跟奏模式', 'record': '录制'}


def is_audio(path):
    """这个文件要不要先转成 MIDI（按扩展名认）。"""
    return os.path.splitext(path)[1].lower() in AUDIO_SUFFIX


# 老版本写过开机自启项，位置在 HKCU 里（不需要管理员权限就能改）。
# 这个功能已经整个删掉了，这里只留一个「启动时清掉残留」用。
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'AutoPlay'

# 跟奏窗口（下落式提示）现在跟着主程序一起发，装完就有。
#
# 以前是靠 exe 名字里有没有 follow 来决定露不露（AutoPlay.exe 是「干净的老版本」、
# AutoPlayFollow.exe 才带跟奏），那是为了留一条退路。现在编辑器也并进主程序了，
# 「干净的老版本」本来就已经不是原来那个了，再藏着一个成熟功能没意义 ——
# 这个开关留着，但一律打开；AutoPlayFollow.spec 还能照常打包（等于同一份程序）。
FOLLOW_MODE = True

# 状态胶囊的颜色：(文字色, 底色透明度)。底色由文字色兑出来 —— 这样换主题时
# 底色会跟着走（以前写死 rgba，紫主题下会露出一块蓝底）。
STATUS_COLORS = {
    'idle':   (theme.c('#9aa6ba'), 0.14),
    'ready':  (theme.c('#7fb0ff'), 0.16),
    'play':   (theme.c('#5fd18b'), 0.16),
    'pause':  (theme.c('#e0b341'), 0.16),
    'error':  (theme.c('#f08a8a'), 0.16),
    'rec':    (theme.c('#e06c75'), 0.18),      # 录制中：跟「演奏中」一眼分得开
}


def _rgba(color, alpha):
    """'#rrggbb' + 透明度 -> 'rgba(r,g,b,a)'（状态胶囊的底色用）。"""
    value = theme.c(color).lstrip('#')
    try:
        red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return color
    return 'rgba(%d,%d,%d,%s)' % (red, green, blue, alpha)

STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 13px; color: #e6e9ef; }
QWidget#root { background: #0f1219; }
/* 覆盖模式没有标题栏，给浮层描一圈边，免得糊在游戏画面上分不清边界 */
QWidget#root[cover="true"] { border: 1px solid #2b3345; }
QDialog { background: #0f1219; }
QPushButton#coverClose { padding: 0; border-radius: 14px; background: #1d2330;
                         color: #8b93a7; font-size: 14px; }
QPushButton#coverClose:hover { background: #b0413e; color: #ffffff; }
/* 右上角的最大化 / 还原：跟「收起」一样是个圆钮，但 hover 走蓝色，别跟关闭混了 */
QPushButton#maxButton { padding: 0; border-radius: 8px; background: #24405f;
                        border: 1px solid #3b82f6; color: #bcd8ff;
                        font-size: 17px; font-weight: 600; }
QPushButton#maxButton:hover { background: #3b82f6; border-color: #7fb0ff; color: #ffffff; }
/* 标题行上的小圆钮：快捷键设置入口（省地方，详情在 tooltip 和托盘右键里） */
QPushButton#iconButton { padding: 0; border-radius: 8px; background: #1d2330;
                         border: 1px solid #2b3345; color: #b9c1d1; font-size: 15px; }
QPushButton#iconButton:hover { background: #242c3c; border-color: #3b82f6; color: #ffffff; }
QPushButton#maxButton:pressed { background: #2f6fd0; border-color: #7fb0ff; }
QPushButton#maxButton[maxed="true"] { background: #2f6fd0; border-color: #9ec9ff;
                                          color: #ffffff; }
QLabel { color: #e6e9ef; }
QLabel#title { font-size: 19px; font-weight: 600; }
QLabel#subtitle { color: #8b93a7; font-size: 12px; }
QLabel#fieldLabel { color: #8b93a7; font-size: 12px; }
QLabel#value { color: #dfe4ee; }
QLabel#counter { color: #8b93a7; font-size: 12px; }
QLabel#hint { color: #6f7787; font-size: 12px; }
QFrame#card { background: #161a23; border: 1px solid #232937; border-radius: 12px; }
/* min-height 是「内容最小高度」：样式表里的 padding 不算进 Qt 的最小尺寸，
   不写这一句，窗口一矮，布局就会把按钮压到 26px 高 —— 按钮上的字被上下切掉。
   加上它，布局的最小高度就是真实需要的高度，宁可让窗口长高也不裁字。 */
QPushButton { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
              padding: 6px 15px; min-height: 17px; color: #dfe4ee; }
QPushButton:hover { background: #242c3c; }
QPushButton:pressed { background: #1a2029; }
QPushButton:disabled { background: #171b24; border-color: #222836; color: #5c6478; }
QPushButton#primary { background: #3b82f6; border-color: #3b82f6; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #4b8ef8; }
QPushButton#primary:disabled { background: #24405f; border-color: #24405f; color: #7d8ea0; }
/* 正在试听时按钮染红，一眼能看出「再按一下就是停」 */
QPushButton#previewOn { background: #3a2226; border-color: #6b2f33; color: #f0a0a0; }
QPushButton#previewOn:hover { background: #46282d; }
QComboBox { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
            padding: 6px 10px; min-height: 17px; color: #dfe4ee; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background: #1d2330; border: 1px solid #2b3345;
                              selection-background-color: #3b82f6; outline: none; }
/* 自绘下拉框：候选列表是主窗口里的子控件，不开新窗口 —— 游戏不会被顶回桌面 */
QPushButton#combo { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
                    padding: 6px 10px; min-height: 17px; color: #dfe4ee; text-align: left; }
QPushButton#combo:hover { background: #242c3c; }
QPushButton#combo[open="true"] { border-color: #3b82f6; background: #202a3c; }
QFrame#comboPanel { background: #12161f; border: 1px solid #2b3345; border-radius: 10px; }
QWidget#comboInner { background: transparent; }
QPushButton#comboRow { background: transparent; border: none; border-radius: 6px;
                       padding: 2px 8px; color: #cbd3e1; font-size: 12px; text-align: left; }
QPushButton#comboRow:hover { background: #232b3a; color: #ffffff; }
QPushButton#comboRow[current="true"] { background: #1d3555; color: #cfe1ff; font-weight: 600; }
QPushButton#comboRow:disabled { background: transparent; color: #565e70; }
QSpinBox { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
           padding: 5px 8px; min-height: 17px; color: #dfe4ee; }
QSpinBox:focus { border-color: #3b82f6; }
QSpinBox::up-button, QSpinBox::down-button { background: #232a38; border: none; width: 16px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #2c3547; }
QSpinBox::up-arrow { width: 0; height: 0; border-left: 3px solid transparent;
                     border-right: 3px solid transparent; border-bottom: 4px solid #9aa6ba; }
QSpinBox::down-arrow { width: 0; height: 0; border-left: 3px solid transparent;
                       border-right: 3px solid transparent; border-top: 4px solid #9aa6ba; }
QLineEdit { background: #1d2330; border: 1px solid #2b3345; border-radius: 8px;
            padding: 6px 10px; min-height: 17px; color: #dfe4ee; }
QLineEdit:focus { border-color: #3b82f6; }
QLabel#warn { color: #f0a0a0; background: #3a2226; border: 1px solid #6b2f33;
              border-radius: 10px; padding: 8px 10px; }
/* 「公开曲库」那种要好好说的事：主色打底，比红字温和 */
QLabel#notice { color: #bcd8ff; background: #1d3555; border: 1px solid #24405f;
                border-radius: 10px; padding: 8px 10px; }
QCheckBox { color: #dfe4ee; spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 5px;
                       border: 1px solid #2b3345; background: #1d2330; }
QCheckBox::indicator:hover { border-color: #3b82f6; }
QCheckBox::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QProgressBar { background: #1b2130; border: none; border-radius: 4px; }
QProgressBar::chunk { background: #3b82f6; border-radius: 4px; }
/* 试听进度条：细槽 + 小圆点把手，深色底上看得清又不抢眼 */
QSlider { min-height: 18px; }
QSlider::groove:horizontal { height: 4px; background: #1b2130; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #3b82f6; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
                             background: #c8d2e2; }
QSlider::handle:horizontal:hover { background: #ffffff; }
QSlider::groove:horizontal:disabled { background: #171c26; }
QSlider::sub-page:horizontal:disabled { background: #2b3345; }
QSlider::handle:horizontal:disabled { background: #3b4457; }
QTextEdit { background: #0c0f15; border: 1px solid #232937; border-radius: 12px;
            color: #b9c1d1; font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px; padding: 8px; }
QListWidget { background: #0c0f15; border: 1px solid #232937; border-radius: 12px;
              color: #dfe4ee; padding: 4px; outline: none; }
QListWidget::item { padding: 6px 10px; border-radius: 6px; }
QListWidget::item:hover { background: #1b2130; }
QListWidget::item:selected { background: #3b82f6; color: #ffffff; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 4px; }
QScrollBar::handle:vertical { background: #2b3345; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMenu { background: #161a23; border: 1px solid #232937; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background: #242c3c; }
/* 「演奏 / 简谱编辑器」两个标签页：扁平的胶囊风，别用 Fusion 默认那种凸起的样子 */
QTabWidget::pane { border: none; }
QTabWidget::tab-bar { left: 14px; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab { background: transparent; color: #8b93a7; padding: 8px 20px;
               margin: 6px 3px 0px 0px; border-radius: 9px; }
QTabBar::tab:hover { color: #c9d2e2; background: #171c26; }
QTabBar::tab:selected { background: #1d2330; color: #e6e9ef; font-weight: 600; }
"""


# ============ 系统相关 ============

LOG_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'AutoPlay')
LOG_PATH = os.path.join(LOG_DIR, 'AutoPlay.log')


# ============ 装在哪、曲子在哪 ============

def app_dir():
    """
    程序自己所在的目录。

    打包后是 exe 那一层（也就是安装目录），源码运行就是仓库目录 ——
    内置曲库、说明文件都按这个位置找。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_subdir(*names):
    """在几个可能的位置里挑第一个存在的子目录：安装目录 / _internal / 源码目录。"""
    roots = [app_dir(), os.path.join(app_dir(), '_internal'),
             os.path.dirname(os.path.abspath(__file__))]
    for root in roots:
        for name in names:
            path = os.path.join(root, name)
            if os.path.isdir(path):
                return path
    return os.path.join(app_dir(), names[0])


# 内置曲库：安装目录下的 songs\（安装程序装出来就是这个结构）。
# 源码运行时直接用仓库里的 songs\。
SONG_DIR = app_subdir('songs', '曲库')
# 谱面写不进 midi 旁边时（装在 Program Files 又没提权之类）退到这儿
SCORE_DIR = os.path.join(LOG_DIR, 'scores')
# 录制的谱面 / midi 放这儿（录的东西跟哪个 midi 都没关系，单独一个文件夹好找）
REC_DIR = os.path.join(LOG_DIR, 'recordings')


def library_songs():
    """内置曲库里的 midi 列表，按文件名排序。"""
    try:
        names = sorted(os.listdir(SONG_DIR), key=lambda name: name.lower())
    except OSError:
        return []
    return [os.path.join(SONG_DIR, name) for name in names
            if name.lower().endswith(MIDI_SUFFIX)]


def write_score(midi_path, analysis, pairs):
    """
    写谱面：优先写在 midi 旁边（跟以前一样）。

    装在 Program Files 这类只读目录、又没有提权的时候会写不进去，那就退到
    %LOCALAPPDATA%\\AutoPlay\\scores 去，别让整条流程卡在写文件上。
    """
    try:
        return jianpu.write_score(midi_path, analysis.tonic, pairs,
                                  track_index=analysis.track_index)
    except OSError as exc:
        os.makedirs(SCORE_DIR, exist_ok=True)
        log_event('谱面写不进 %s（%s），改写到 %s'
                  % (os.path.dirname(os.path.abspath(midi_path)), exc, SCORE_DIR))
        return jianpu.write_score(midi_path, analysis.tonic, pairs,
                                  out_dir=SCORE_DIR, track_index=analysis.track_index)


def log_event(text):
    """
    往 %LOCALAPPDATA%\\AutoPlay\\AutoPlay.log 记一行。

    程序没有黑框，出错 / 退出都没有任何输出，所以启动、载入、演奏、退出、崩溃
    都往这个文件记一笔：万一「闪退」，看最后几行就知道走到哪一步了。
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, 'a', encoding='utf-8') as fh:
            fh.write('[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass


def log_crash(text):
    """出错时把完整的 traceback 记进同一个日志。"""
    log_event('出错了：\n%s' % text)


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """以管理员身份重新启动自己，返回 True 表示已经发起（当前进程该退了）。"""
    try:
        import ctypes
        if getattr(sys, 'frozen', False):
            target, params = sys.executable, ''
        else:
            target = sys.executable
            params = '"%s"' % os.path.abspath(sys.argv[0])
        extra = ' '.join('"%s"' % arg for arg in sys.argv[1:] if arg != '--no-admin')
        result = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', target, (params + ' ' + extra).strip(), None, 1)
    except Exception:
        return False
    return result > 32


def drop_old_autostart():
    """
    把老版本写下的开机自启项删掉，返回有没有真的删掉一条。

    v1.2 以前界面上有个「开机自动启动」（默认还是开的），会往 HKCU 的 Run 键写一条
    AutoPlay。用户明确说这个功能容易招人烦，所以整个删掉了 —— 启动时顺手把以前的
    残留清干净，免得换过路径 / 删过文件夹之后还留着一个指向老程序的启动项。
    本来就没有的话，什么都不记，日志保持干净。
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            try:
                winreg.QueryValueEx(key, RUN_NAME)
            except OSError:
                return False                           # 没有残留，别打扰用户
            winreg.DeleteValue(key, RUN_NAME)
    except Exception:
        return False
    log_event('清掉了老版本留下的开机自启项')
    return True


def dark_palette():
    """
    深色配色。

    Qt 的 Fusion 风格自带浅色调色板，凡是用到调色板的地方（数字框里的输入区、
    工具提示等）就会白底白字；样式表管不到这些，得把调色板也换掉。
    """
    palette = QPalette()
    text = QColor(theme.c('#e6e9ef'))
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.c('#0f1219')))
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.c('#141922')))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.c('#161a23')))
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.c('#1d2330')))
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.c('#161a23')))
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.c('#3b82f6')))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.c('#ffffff')))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.c('#6f7787')))
    return palette


def set_topmost(widget, topmost=True):
    """给窗口加 / 撤「总在最前」。游戏在前面时，只有这样才能压住它。"""
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_bool
        insert = -1 if topmost else -2                 # HWND_TOPMOST / HWND_NOTOPMOST
        # NOACTIVATE（0x0010）一定要带：SetWindowPos 少了它会顺手把前台抢过来，
        # 全屏游戏就掉回桌面了。
        user32.SetWindowPos(ctypes.c_void_p(int(widget.winId())), ctypes.c_void_p(insert),
                            0, 0, 0, 0,
                            0x0002 | 0x0001 | 0x0010 | 0x0040)  # NOMOVE|NOSIZE|NOACTIVATE|SHOWWINDOW
        return True
    except Exception:
        return False


def set_no_activate(widget, on=True):
    """
    给窗口加 / 撤 WS_EX_NOACTIVATE：加上之后连点它都不会把焦点从游戏抢走。

    录屏软件的浮层就是这么干的 —— 鼠标照样能按，前台窗口一直是游戏。
    代价是这个窗口收不到键盘（所以要留一个鼠标能点的「收起」按钮）。
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(int(widget.winId()))
        if not hwnd:
            return False
        gwl_exstyle, ws_ex_noactivate = -20, 0x08000000
        user32.GetWindowLongW.restype = ctypes.c_long
        style = user32.GetWindowLongW(hwnd, gwl_exstyle)
        user32.SetWindowLongW(hwnd, gwl_exstyle,
                              style | ws_ex_noactivate if on
                              else style & ~ws_ex_noactivate)
        return True
    except Exception:
        return False


def show_no_activate(widget):
    """把窗口显示出来但不激活（SW_SHOWNOACTIVATE + 置顶），游戏不会被打回桌面。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(int(widget.winId()))
        user32.ShowWindow(hwnd, 4)                          # SW_SHOWNOACTIVATE
        user32.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0,
                            0x0002 | 0x0001 | 0x0010 | 0x0040)
        return True
    except Exception:
        return False


def force_window_front(widget):
    """
    把窗口真的顶到最前面，返回有没有抢到前台焦点。

    Qt 的 raise_() / activateWindow() 走的是 SetForegroundWindow，而 Windows 默认
    拒绝「后台进程抢前台」——全屏游戏在前面时就会变成「热键明明触发了，窗口却没冒出来」
    （日志里能看到「唤起主窗口」记了，人却看不见窗口）。所以这里自己来：

    1. SetWindowPos(HWND_TOPMOST)：贴到最高层，游戏压不住它；
    2. ShowWindow(SW_RESTORE)：万一被压住 / 最小化了，先恢复出来；
    3. AttachThreadInput 接到前台线程再 SetForegroundWindow：这样抢前台才会被接受。
    """
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return False
        set_topmost(widget, True)
        return foreground_to(hwnd)
    except Exception:
        log_crash(traceback.format_exc())
        return False


def foreground_to(hwnd):
    """
    把前台交给某个**窗口句柄**（不是 Qt 控件），返回有没有成功。

    Windows 默认拒绝「后台进程抢前台」，所以得先 AttachThreadInput 挂到当前前台线程上，
    这一步才会被接受。游戏掉回桌面之后想把它救回来，用的就是这个。
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = int(hwnd or 0)
        if not hwnd:
            return False
        user32.ShowWindow(ctypes.c_void_p(hwnd), 9)            # SW_RESTORE
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        front = int(user32.GetForegroundWindow() or 0)
        my_thread = kernel32.GetCurrentThreadId()
        front_thread = user32.GetWindowThreadProcessId(ctypes.c_void_p(front), None) if front else 0
        if front_thread and front_thread != my_thread:
            user32.AttachThreadInput(front_thread, my_thread, True)
            try:
                user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
            finally:
                user32.AttachThreadInput(front_thread, my_thread, False)
        else:
            user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        return int(user32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        log_crash(traceback.format_exc())
        return False


class RECT(ctypes.Structure):
    """Win32 的 RECT（GetWindowRect / GetMonitorInfo 都要用）。"""

    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    """Win32 的 MONITORINFO，只要 rcMonitor / rcWork 两项。"""

    _fields_ = [('cbSize', ctypes.c_ulong), ('rcMonitor', RECT),
                ('rcWork', RECT), ('dwFlags', ctypes.c_ulong)]


def foreground_hwnd():
    """当前前台窗口的句柄（拿不到就是 0）。"""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def window_pid(hwnd):
    """某个窗口属于哪个进程。"""
    try:
        user32 = ctypes.windll.user32
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def foreground_is_fullscreen():
    """
    前台是不是一个**盖满整屏的别人的窗口** —— 也就是大概率的全屏游戏。

    这是「现在到底在不在游戏里」唯一靠谱的判据：浮层是「不接受焦点」的，
    贴在游戏上的时候前台**仍然还是游戏**，所以这里问到的就是游戏自己。

    看的是整块显示器（不是工作区）：普通最大化窗口底下留着任务栏，盖不满；
    真正全屏的程序（全屏游戏、F11 的浏览器）才盖得满。

    返回那个窗口的句柄；不是全屏（或者在桌面上）就返回 0。
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = foreground_hwnd()
        if not hwnd:
            return 0
        if window_pid(hwnd) == os.getpid():
            return 0                       # 前台就是我们自己，那当然不算游戏
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), title, 512)
        if not title.value or title.value == 'Program Manager':
            return 0                       # 桌面 / 锁屏，不是全屏程序
        rect = RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return 0
        monitor = user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)     # NEAREST
        if not monitor:
            return 0
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            return 0
        if not covers_monitor(rect, info.rcMonitor):
            return 0
        return hwnd
    except Exception:
        return 0


def covers_monitor(rect, monitor, ratio=0.98):
    """
    rect 这块矩形有没有盖满 monitor 这整块屏。

    看的是「整块显示器」而不是工作区：普通最大化窗口底下留着任务栏，盖不满；
    真正全屏的程序（全屏游戏、F11 的浏览器）才盖得满。
    """
    width = float(monitor.right - monitor.left) or 1.0
    height = float(monitor.bottom - monitor.top) or 1.0
    left = max(rect.left, monitor.left)
    top = max(rect.top, monitor.top)
    right = min(rect.right, monitor.right)
    bottom = min(rect.bottom, monitor.bottom)
    if right <= left or bottom <= top:
        return False
    return (right - left) / width >= ratio and (bottom - top) / height >= ratio


# 图标在 noteicon.py 里（矢量画的音符，不依赖字体），安装程序和快捷方式用的是同一个
from noteicon import make_icon


def style_sheet():
    """当前配色主题下的整套样式表（换主题时重新算一遍就行）。"""
    return theme.paint(STYLE)


def _saved_theme():
    """上次用的那套配色（没记过就用默认）。"""
    return theme.saved_name()


def theme_folder():
    """主题文件放在哪儿（托盘里「打开主题文件夹」用）。"""
    folders = theme.folders()
    return folders[-1] if folders else ''


class Overlay(QWidget):
    """
    演奏进度浮窗：独立的小窗口，半透明、置顶、不抢焦点、鼠标能穿透。

    贴在屏幕（鼠标所在的那块）右上角，只显示「状态 + 进度」，尽量不挡游戏画面。
    主窗口是下沉还是收进托盘都不影响它。
    """

    WIDTH = 232
    HEIGHT = 52
    MARGIN = 14
    TEXT_COLORS = {
        'ready': ('已就绪', theme.c('#7fb0ff')),
        'play':  ('演奏中', theme.c('#5fd18b')),
        'pause': ('已暂停', theme.c('#e0b341')),
        'stop':  ('已停止', theme.c('#9aa6ba')),
        'done':  ('演奏完成', theme.c('#7fb0ff')),
        'rec':   ('录制中', theme.c('#e06c75')),
    }

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        # SetWindowPos 的参数类型写死：HWND 是 64 位指针，不声明的话 ctypes 会按
        # 32 位整数传，句柄被截断，可能会动到别的窗口
        try:
            user32 = ctypes.windll.user32
            user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            user32.SetWindowPos.restype = ctypes.c_bool
        except Exception:
            pass
        self.state = 'stop'
        self.done = 0
        self.total = 1
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hide_timeout)
        self.on_timeout = None          # 倒计时结束后由主界面接手（比如回到「已就绪」）
        self._top_timer = QTimer(self)
        self._top_timer.setInterval(1200)
        self._top_timer.timeout.connect(self.keep_on_top)

    # ---------- 对外接口 ----------

    def set_ready(self, total=None):
        """读好 midi、等开始：显示「已就绪」，进度归零（没有演奏时也该看得见）。"""
        if total is not None:
            self.total = max(int(total), 1)
        self.state, self.done = 'ready', 0
        self._hide_timer.stop()
        self.move_to_corner()
        if not self.isVisible():
            self.show()
        self.keep_on_top()
        self._top_timer.start()
        self.update()

    def begin(self, total):
        """开始演奏：显示浮窗并归零。"""
        self.state, self.done, self.total = 'play', 0, max(int(total), 1)
        self._hide_timer.stop()
        self.move_to_corner()
        self.show()
        self.keep_on_top()
        self._top_timer.start()
        self.update()

    def set_progress(self, done, total):
        self.done, self.total = done, max(int(total), 1)
        if self.state != 'play':
            self.state = 'play'
        self.update()

    def set_state(self, state):
        """state: play / pause / stop / done / rec"""
        self.state = state
        self.update()

    def record_begin(self):
        """开始录制：把浮窗亮出来，状态写「录制中」。"""
        self.state, self.done, self.total = 'rec', 0, 1
        self._hide_timer.stop()
        self.move_to_corner()
        self.show()
        self.keep_on_top()
        self._top_timer.start()
        self.update()

    def record_count(self, count):
        """录制中：把「录到几个音」亮出来。"""
        self.done, self.total = max(int(count), 0), 1
        self.update()

    def finish(self, state='stop'):
        """演奏结束：先把结果亮出来，3 秒后自己收起来。"""
        self.state = state
        self._top_timer.stop()
        self.update()
        self._hide_timer.start(3000)

    def shutdown(self):
        self._hide_timer.stop()
        self._top_timer.stop()
        self.hide()

    # ---------- 内部 ----------

    def move_to_corner(self):
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - self.MARGIN + 1, area.top() + self.MARGIN)

    def keep_on_top(self):
        """游戏有时会把置顶抢走，这里再顶一次；SWP_NOACTIVATE 保证不抢焦点。"""
        if not self.isVisible():
            return
        try:
            ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(int(self.winId())), ctypes.c_void_p(-1),
                0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
        except Exception:
            try:
                self.raise_()
            except Exception:
                pass

    def _on_hide_timeout(self):
        """结果展示完了：先问主界面要不要切回「已就绪」，没人管就直接收起来。"""
        if callable(self.on_timeout):
            try:
                if self.on_timeout():
                    return
            except Exception:
                pass
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(255, 255, 255, 34), 1))
        painter.setBrush(QBrush(QColor(10, 13, 20, 170)))
        painter.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 10, 10)

        text, color = self.TEXT_COLORS.get(self.state, self.TEXT_COLORS['stop'])
        font = QFont('Microsoft YaHei UI', 9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(color)))
        painter.drawText(12, 6, self.width() - 24, 20,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        counter = ('%d 个音' % self.done if self.state == 'rec'
                   else '%d / %d' % (self.done, self.total))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(QColor(176, 185, 203)))
        painter.drawText(12, 6, self.width() - 24, 20,
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         counter)

        bar_y, bar_h = 34, 5
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 38)))
        painter.drawRoundedRect(QRectF(12, bar_y, self.width() - 24, bar_h), 2.5, 2.5)
        ratio = min(1.0, max(0.0, self.done / float(self.total or 1)))
        # 录制中没有「进度」可言，整条淡红亮着，表示「一直在录」
        filled = (self.width() - 24) if self.state == 'rec' else int((self.width() - 24) * ratio)
        if filled > 0:
            brush = QColor(color)
            if self.state == 'rec':
                brush.setAlpha(110)
            painter.setBrush(QBrush(brush))
            painter.drawRoundedRect(QRectF(12, bar_y, max(filled, bar_h), bar_h), 2.5, 2.5)


class HotkeyDialog(QDialog):
    """
    快捷键设置：点某一行的按键，然后直接按下想用的组合键就录进去了。

    只认 hotkeys.VK_NAMES 里的键（字母 / 数字 / F1-F24 / 方向键 / 空格…），
    修饰键可以带 Ctrl / Shift / Alt；录的时候按 Esc 取消这一行。
    """

    def __init__(self, keymap, parent=None):
        super().__init__(parent)
        self.setWindowTitle('快捷键设置')
        self.setModal(True)
        self.setMinimumWidth(430)
        self.keymap = keymap.copy()
        self.recording = None          # 正在录的是哪个动作
        self.buttons = {}
        self._build()
        self._refresh()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        tip = QLabel('点右边的按键，再按下想用的组合键（可以带 Ctrl / Shift / Alt）。\n'
                     '录的时候按 Esc 取消这一行；「默认」把这一行恢复成出厂设置。')
        tip.setObjectName('hint')
        root.addWidget(tip)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        for row, action in enumerate(hotkeys.ACTION_ORDER):
            label = QLabel(hotkeys.ACTION_LABELS[action])
            button = QPushButton()
            button.setMinimumWidth(170)
            button.setToolTip('点一下，然后按下新的组合键')
            button.clicked.connect(lambda _=False, a=action: self.start_record(a))
            button.installEventFilter(self)          # 录制时把按键抢过来
            self.buttons[action] = button
            reset = QPushButton('默认')
            reset.setFixedWidth(60)
            reset.setToolTip('恢复默认：%s' % hotkeys.pretty_combo(hotkeys.DEFAULT_BINDINGS[action]))
            reset.clicked.connect(lambda _=False, a=action: self.reset_row(a))
            grid.addWidget(label, row, 0)
            grid.addWidget(button, row, 1)
            grid.addWidget(reset, row, 2)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)

        self.tip_error = QLabel('')
        self.tip_error.setObjectName('hint')
        self.tip_error.setWordWrap(True)
        root.addWidget(self.tip_error)

        bottom = QHBoxLayout()
        all_reset = QPushButton('全部恢复默认')
        all_reset.clicked.connect(self.reset_all)
        bottom.addWidget(all_reset)
        bottom.addStretch(1)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        save = QPushButton('保存')
        save.setDefault(True)
        save.clicked.connect(self.accept)
        bottom.addWidget(cancel)
        bottom.addWidget(save)
        root.addLayout(bottom)

    # ---------- 录制 ----------

    def eventFilter(self, obj, event):
        """录制中：把落在按钮上的按键截下来当新键位。"""
        if self.recording and obj is self.buttons.get(self.recording):
            if event.type() == QEvent.Type.KeyPress:
                self._grab(event)
                return True
            if event.type() == QEvent.Type.KeyRelease:
                return True                 # 连抬键一起吃掉，免得按钮顺手被点一下
        return super().eventFilter(obj, event)

    def _grab(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.recording = None
            self.tip_error.setText('')
            self._refresh()
            return
        if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
                           Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return                          # 只按了修饰键，继续等真正的键
        name = hotkeys.VK_NAMES.get(int(event.nativeVirtualKey() or 0))
        if not name:
            self.tip_error.setText('这个键绑不了，换一个（字母、数字、F1-F24、方向键、空格都可以）')
            return
        mods = []
        held = event.modifiers()
        if held & Qt.KeyboardModifier.ControlModifier:
            mods.append('ctrl')
        if held & Qt.KeyboardModifier.ShiftModifier:
            mods.append('shift')
        if held & Qt.KeyboardModifier.AltModifier:
            mods.append('alt')
        combo = '+'.join(mods + [name])
        other = self.keymap.used_by(combo, skip=self.recording)
        if other:
            self.tip_error.setText('「%s」已经占了 %s，换一个吧'
                                   % (hotkeys.ACTION_LABELS[other], hotkeys.pretty_combo(combo)))
            return
        self.keymap.set(self.recording, combo)
        self.recording = None
        self.tip_error.setText('')
        self._refresh()

    def start_record(self, action):
        self.recording = action
        self.tip_error.setText('')
        self._refresh()
        # 焦点挪到这个按钮上，接下来的按键才会经过上面的过滤器
        self.buttons[action].setFocus(Qt.FocusReason.OtherFocusReason)

    def reset_row(self, action):
        self.keymap.reset(action)
        if self.recording == action:
            self.recording = None
        self.tip_error.setText('')
        self._refresh()

    def reset_all(self):
        self.keymap.reset()
        self.recording = None
        self.tip_error.setText('')
        self._refresh()

    def _refresh(self):
        for action, button in self.buttons.items():
            if action == self.recording:
                button.setText('按下新的快捷键…')
            else:
                combo = self.keymap.combo_of(action)
                button.setText(hotkeys.pretty_combo(combo) if combo else '未设置')


class SeekSlider(QSlider):
    """
    试听进度条。

    普通 QSlider 点空白处只会按 pageStep 翻一「页」，这里改成**点到哪儿跳到哪儿** ——
    试听要的就是「直接听这一段」。手指按着的时候（scrubbing）界面会暂停自动刷新，
    免得播放进度跟手指抢这个把手；松手才真的跳过去。
    """

    seeked = Signal(int)            # 松手：请求跳到这个位置（0~SEEK_RANGE）

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, SEEK_RANGE)
        self.setPageStep(0)         # 不给翻页留机会：位置只由「点/拖到哪儿」决定
        self._dragging = False

    def scrubbing(self):
        """手指还在把手上吗（界面据此暂停自动刷新）。"""
        return self._dragging

    def _value_at(self, x):
        width = max(self.width(), 1)
        return int(round(min(max(float(x), 0.0), float(width)) / width * self.maximum()))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self.setValue(self._value_at(event.position().x()))
            return                  # 不交给基类：基类会按 pageStep 翻页，不是我们要的
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.setValue(self._value_at(event.position().x()))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.seeked.emit(self.value())
            return
        super().mouseReleaseEvent(event)


class InlineCombo(QPushButton):
    """
    自绘下拉框：候选列表是主窗口里的一块子控件，**从头到尾不开第二个窗口**。

    为什么不用 QComboBox：它的下拉列表其实是一个独立的顶层窗口，一弹出来就会把
    自己设成前台 —— 独占全屏的游戏碰上这一下就被打回桌面。给那个弹窗挂
    WS_EX_NOACTIVATE 也压不住（实测过了：标志挂上了，Qt 自己还会再激活它一次）。
    这里的列表只是主窗口里的普通子控件，一个额外的 HWND 都没有，游戏半点感觉没有。

    接口照着 QComboBox 常用的那几个做：addItem / addItems / currentText /
    currentData / setCurrentText / setCurrentIndex / findData / count /
    itemText / clear / setItemEnabled，信号只有 currentTextChanged。

    列表项可以单独禁用（音轨列表里没音符的那几条要灰掉）。
    """

    currentTextChanged = Signal(str)

    ROW_HEIGHT = 26                 # 一行多高
    MIN_PANEL_HEIGHT = 72           # 列表最矮这么高，再矮就没法点了

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('combo')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._items = []            # [[文字, 附带数据, 能不能选]]
        self._index = -1
        self._panel = None          # 列表那块面板（第一次打开时才建）
        self._rows = []             # 面板里的每一行
        self.clicked.connect(self._toggle)

    # ============ QComboBox 那套接口 ============

    def addItem(self, text, data=None):
        self._items.append([str(text), data, True])
        if self._index < 0:
            self._index = 0
        self._refresh_text()
        return len(self._items) - 1

    def addItems(self, texts):
        for text in texts:
            self.addItem(text)

    def clear(self):
        self._close()
        self._items = []
        self._index = -1
        self._refresh_text()
        self._rebuild_rows()

    def count(self):
        return len(self._items)

    def itemText(self, index):
        return self._items[index][0] if 0 <= index < len(self._items) else ''

    def itemData(self, index):
        return self._items[index][1] if 0 <= index < len(self._items) else None

    def setItemEnabled(self, index, on=True):
        """某一条能不能选（灰掉的那条点了没反应）。"""
        if 0 <= index < len(self._items):
            self._items[index][2] = bool(on)
            self._rebuild_rows()

    def findData(self, data):
        """按附带数据找第几行（找不到给 -1）。"""
        for index, item in enumerate(self._items):
            if item[1] == data:
                return index
        return -1

    def currentIndex(self):
        return self._index

    def currentText(self):
        return self.itemText(self._index)

    def currentData(self):
        return self.itemData(self._index)

    def setCurrentIndex(self, index):
        if 0 <= index < len(self._items) and index != self._index:
            self._index = index
            self._refresh_text()
            self._rebuild_rows()
            self.currentTextChanged.emit(self.currentText())

    def setCurrentText(self, text):
        """跟 QComboBox 一样：找不到这个文字就不动（不新建一条）。"""
        text = str(text)
        for index, item in enumerate(self._items):
            if item[0] == text:
                self.setCurrentIndex(index)
                return
        if self._index < 0 and self._items:
            self.setCurrentIndex(0)

    # ============ 展开 / 收起 ============

    def _refresh_text(self):
        text = self.currentText()
        self.setText('%s  ▾' % text if text else '▾')
        self.setToolTip(self.toolTip())          # 触发一次重绘，文字变了宽度也跟着变

    def _ensure_panel(self):
        """列表那块面板：建在主窗口里，跟着主窗口一起动。"""
        if self._panel is not None:
            return
        window = self.window()
        panel = QFrame(window)
        panel.setObjectName('comboPanel')
        panel.hide()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)
        area = QScrollArea(panel)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        inner = QWidget()
        inner.setObjectName('comboInner')
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        area.setWidget(inner)
        outer.addWidget(area)
        self._panel = panel
        self._panel_box = box

    def _rebuild_rows(self):
        """按当前条目重建列表里的每一行。"""
        if self._panel is None:
            return
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        for index, (text, _data, allowed) in enumerate(self._items):
            row = QPushButton(text, self._panel)
            row.setObjectName('comboRow')
            row.setProperty('current', index == self._index)
            row.setEnabled(allowed)
            row.setFixedHeight(self.ROW_HEIGHT)
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.clicked.connect(lambda _checked=False, i=index: self._pick(i))
            self._panel_box.addWidget(row)
            self._rows.append(row)

    def _natural_height(self):
        """列表全展开要占多高（行高 × 行数 + 内边距）。"""
        return len(self._items) * (self.ROW_HEIGHT + 2) + 2 * 4 + 8

    def _open(self):
        if not self._items:
            return
        self._ensure_panel()
        self._rebuild_rows()
        panel = self._panel
        window = panel.parentWidget()
        width = max(self.width(), self.sizeHint().width(), 120)
        left = self.mapTo(window, QPoint(0, self.height() + 2))
        x = min(max(left.x(), 4), max(window.width() - width - 4, 4))
        natural = self._natural_height()
        top = left.y() - self.height() - 2       # 按钮的上沿
        below = window.height() - left.y() - 6
        if below >= min(natural, self.MIN_PANEL_HEIGHT) or below >= top:
            y, room = left.y(), below                    # 放下面（够用，或者上面更挤）
        else:
            y, room = max(top - natural, 4), max(top - 6, self.MIN_PANEL_HEIGHT)
        height = int(min(natural, max(room, self.MIN_PANEL_HEIGHT)))
        panel.setGeometry(int(x), int(y), int(width), height)
        panel.show()
        panel.raise_()
        window.installEventFilter(self)         # 点别处就收起来
        self.setProperty('open', True)
        self._repolish()

    def _close(self):
        if self._panel is None or not self._panel.isVisible():
            self.setProperty('open', False)
            self._repolish()
            return
        self._panel.hide()
        window = self._panel.parentWidget()
        if window is not None:
            window.removeEventFilter(self)
        self.setProperty('open', False)
        self._repolish()

    def _repolish(self):
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _toggle(self):
        if self._panel is not None and self._panel.isVisible():
            self._close()
        else:
            self._open()

    def _pick(self, index):
        self._close()
        if 0 <= index < len(self._items) and self._items[index][2]:
            self.setCurrentIndex(index)

    def eventFilter(self, watched, event):
        """列表开着的时候，点面板以外的地方就收起来。"""
        if self._panel is not None and self._panel.isVisible() \
                and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
            if event.type() == QEvent.Type.MouseButtonPress:
                point = event.position().toPoint() if hasattr(event, 'position') else event.pos()
                inside_panel = self._panel.geometry().contains(point)
                inside_button = self.rect().contains(self.mapFrom(watched, point))
                if not inside_panel and not inside_button:
                    self._close()
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self._close()
        super().hideEvent(event)


class MainWindow(QWidget):
    message = Signal(str)          # 后台线程 -> 界面 的日志
    progress = Signal(int, int)    # 演出进度
    finished = Signal(str)         # 演奏结束：'done' 正常走完 / 'stop' 中途停了
    hotkey = Signal(str)           # 全局热键 -> 主线程动作
    audio_done = Signal(str, str)  # 音频转 MIDI 结束：(MIDI 路径, 出错信息)
    preview_ready = Signal(str)    # 试听合成结束：出错信息（空串 = 好了）
    diag_sound = Signal(str)       # 「谁在响」听到一声：一句话
    diag_done = Signal(str)        # 「谁在响」听完了：总结

    def __init__(self, initial=None, parent=None):
        super().__init__(parent)
        self.setObjectName('root')
        self.setWindowTitle('%s v%s' % (APP_TITLE, APP_VERSION))
        self.setMinimumSize(700, 560)
        self.resize(780, 700)
        self.setWindowIcon(make_icon())

        self.settings = QSettings('AutoPlay', 'AutoPlay')
        self.player = player.Player(log=self.message.emit)
        self.analysis = None
        self.score_path = None
        self.score_events = []
        self.worker = None
        self.tray = None
        self.hotkeys = None
        self.hotkey_timer = None
        self.midi_path = None
        self.track_index = None
        self.last_dir = SONG_DIR if os.path.isdir(SONG_DIR) else os.getcwd()
        self.overlay = Overlay()
        self.overlay.on_timeout = self._overlay_after_finish
        self.overlay_on = True         # 进度浮窗是不是想显示（托盘菜单可切）
        self.cover_action = None       # 托盘里的「游戏内覆盖」勾选项
        self.follow = None             # 跟奏窗口，第一次打开时才创建
        self.follow_on = False         # 想不想显示跟奏窗口
        self.practice = False          # 正在「等我按对」的练习模式（不走演奏器）
        self.follow_action = None      # 托盘里的「跟奏模式」勾选项
        # 唤起主窗口后「总在最前」的计时器：到点还没在用就撤掉，别一直压着游戏
        self.topmost_timer = QTimer(self)
        self.topmost_timer.setSingleShot(True)
        self.topmost_timer.setInterval(TOPMOST_MS)
        self.topmost_timer.timeout.connect(self._drop_topmost)
        # 前台兜底：浮层贴着游戏的时候，万一前台还是被我们抢了，把游戏推回去
        self.game_watch = QTimer(self)
        self.game_watch.setInterval(GAME_WATCH_MS)
        self.game_watch.timeout.connect(self._watch_game_focus)
        # 全局热键的键位表：默认 F6 / F7 / F8 / Ctrl+F1 / Ctrl+F2，界面上可以改。
        # 要在 _build() 之前读出来 —— 界面底部那行提示按当前键位显示。
        self.keymap = self._load_hotkey_map()
        self.hotkey_actions = {}
        self.cover_mode = True         # 游戏内覆盖：无边框 + 唤起时不抢焦点
        self._drag_offset = None       # 拖浮层用的偏移
        self._choosing = None          # 正在开着的「选文件」面板
        self._pick_dir = self.last_dir # 自绘文件列表当前在哪个文件夹
        self.pick_list = None          # 自绘文件列表里的控件（开着的时候才有）
        self.pick_ok = None
        self.pick_online = None
        self._fitted = False           # 第一次露头时按内容量过一次高度没
        self.log_action = None         # 托盘里「运行日志（控制台）」那一项（没托盘时是 None）
        self.tray = None               # 托盘图标本身（系统托盘不可用时是 None）
        self.hotkey_actions = {}       # 托盘菜单里的键位项（同上）
        self.overlay_active = False    # 现在是不是「浮层形态」（贴着游戏的那种窗口）
        self._maximized = False        # 主窗口现在最大化了没（编辑工程时铺满屏幕用）
        self._summoned_in_game = False # 这次是 Ctrl+F1 从游戏里唤起来的（决定用哪种选文件）
        self._game_hwnd = 0            # 唤起前占着全屏的那个窗口（游戏）：抢了前台要还给它
        self._watch_left = 0           # 前台兜底还能出手几次
        self._last_in_game = None      # 上一次判断「在不在游戏里」的结果（变了就要跟界面同步）
        self.desk_tips = {}            # 浮层形态下会被临时改文案的按钮，记着原本的提示
        self._converting = False       # 正在把音频转成 MIDI
        self.editor = None             # 简谱编辑器那一页（第一次点进去才建）
        self._building_editor = False  # 正在建编辑器（防止标签页来回切时重入）
        self.editor_windows = []       # 另外开出来的编辑器窗口（外部双击工程文件时用）
        # 试听：合成走后台线程，播放交给 preview.Preview（winsound 异步放）
        self.previewer = preview.Preview(log=self.message.emit)
        self._previewing = False       # 正在试听（包括还在合成的那一小会儿）
        self._preview_path = None      # 这次试听用的 wav
        self._preview_t0 = None        # 从哪一刻开始放的（自己数秒算进度）
        self._preview_base = 0.0       # 这一遍是从第几秒开始放的（拨过进度条就不是 0 了）
        self._preview_total = 0.0      # 试听总长（秒）
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(PREVIEW_TICK_MS)
        self.preview_timer.timeout.connect(self._preview_tick)
        # 录制：F10 开关，录完写成谱面 + midi，顺手送进简谱编辑器
        self.recorder = recorder.Recorder(log=self.message.emit, on_note=self._rec_note)
        self.recording = False
        self.rec_timer = QTimer(self)
        self.rec_timer.setInterval(REC_TICK_MS)
        self.rec_timer.timeout.connect(self._rec_tick)
        # 录制时给自己一个「琴键反馈」：桌面上按一下响一下，游戏里静音（见 _rec_note）
        self.note_sound = notesound.NotePlayer(log=self.message.emit)
        self._monitor_on = MONITOR_DEFAULT   # 「录制时发声」开着没
        self._rec_tonic = 60                 # 这次录制按哪个主音出声（开始录时定下来）
        self._mute_system = MUTE_DEFAULT     # 录制时按住「系统提示音」开着没
        self._mute_token = None              # 按住之后的凭据（收工时拿它放回去）
        self.diag = None                     # 「谁在响」那个听诊器

        self._build()
        self.message.connect(self.log)
        self.progress.connect(self._on_progress)
        self.finished.connect(self._on_finished)
        self.hotkey.connect(self._on_hotkey)
        self.audio_done.connect(self._on_audio_done)
        self.preview_ready.connect(self._on_preview_ready)
        self.diag_sound.connect(self._on_diag_sound)
        self.diag_done.connect(self._on_diag_done)
        self._setup_tray()
        self._setup_hotkeys()
        self._load_settings()
        # 上次要是崩在录制里，系统提示音还按着 —— 现在给它放回去
        try:
            if audiowatch.recover_hold():
                self.log('上次录制没正常收工：系统提示音已经放回去了')
        except Exception:
            pass
        # 窗口里按 ESC 只收窗口（全局 ESC 太危险：游戏里按一下就把程序退了）
        # 覆盖模式下 ESC 是「收起回游戏」；普通模式还是原来的「退出程序」
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.activated.connect(self._on_esc)
        # F11：最大化 / 还原。无边框窗口没有标题栏，编辑工程想铺满屏幕就按它
        max_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        max_shortcut.activated.connect(self.toggle_maximize)
        self._max_shortcut = max_shortcut
        log_event('界面准备好了')
        if initial:
            self.open_initial(initial)
        else:
            self.load_default_song()
        # 上次被强杀 / 崩了留下的自动保存：摆回编辑器（正常退出时是空的）
        self._restore_autosave(initial or '')

    # ---------- 界面 ----------

    def _build(self):
        # 分两页：「演奏」是原来那套，「简谱编辑器」是转出来的谱不对时拿来手改的。
        # 编辑器和主程序在同一个进程里，是一个标签页，不是另开的程序。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setObjectName('mainTabs')
        outer.addWidget(self.tabs)
        self.play_page = QWidget()
        self.tabs.addTab(self.play_page, '演奏')

        root = QVBoxLayout(self.play_page)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(APP_TITLE)
        title.setObjectName('title')
        subtitle = QLabel('选一个 midi，点开始演奏，然后切回游戏 · v%s' % APP_VERSION)
        subtitle.setObjectName('subtitle')
        titles.addWidget(title)
        titles.addWidget(subtitle)
        head.addLayout(titles, 1)
        self.status = QLabel('就绪')
        self.status.setObjectName('pill')
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 试听时这里要显示「试听 0:12 / 1:35」，比「已就绪」长得多。先把宽度按最长那句
        # 留够，不然布局会把文字从两头裁掉（看着像「听 0:00 / 2:」）。
        metrics = self.status.fontMetrics()
        width = getattr(metrics, 'horizontalAdvance', None) or metrics.width
        self.status.setMinimumWidth(width('试听 00:00 / 00:00') + 30)
        head.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        # 覆盖模式下没有标题栏，给一个「收起」按钮（右键托盘图标也能退出）
        self.cover_close = QPushButton('×')
        self.cover_close.setObjectName('coverClose')
        self.cover_close.setFixedSize(28, 28)
        self.cover_close.setToolTip('收起浮层，回到游戏（也可以按 Esc）')
        self.cover_close.clicked.connect(self.hide_cover)
        self.cover_close.setVisible(False)
        head.addWidget(self.cover_close, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)
        # 无边框窗口没有标题栏，自己给一个「最大化」按钮。它在 _build 末尾挂到标签栏
        # 右上角 —— 挂那儿「演奏 / 简谱编辑器」两页都看得见，编辑工程时抬手就能按。
        self.max_button = QPushButton('□')
        self.max_button.setObjectName('maxButton')
        self.max_button.setFixedSize(32, 32)
        self.max_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.max_button.setToolTip('最大化 / 还原（也可以按 F11）')
        self.max_button.clicked.connect(self.toggle_maximize)
        # 快捷键设置的入口：摆在标题行（原来在按钮行最右边，那一行被它撑到 640px 宽，
        # 窗口一窄就把「开始演奏 / 停止」的字裁掉）。这里一个小圆钮就够，说明在 tooltip 里。
        # 配色主题：主界面 / 右上角浮窗 / 跟奏 / 编辑器一起换（主题是 json，见 theme.py）
        self.theme_combo = InlineCombo()
        self.theme_combo.addItems(theme.names())
        self.theme_combo.setCurrentText(theme.current_name())
        self.theme_combo.setFixedWidth(96)
        self.theme_combo.setToolTip('配色主题：主界面、右上角进度浮窗、跟奏面板、编辑器一起换。\n'
                                    '主题是 json，放在：\n%s\n'
                                    '改完在托盘图标右键 →「配色主题」→「重新载入主题文件」。'
                                    % theme_folder())
        self.theme_combo.currentTextChanged.connect(self.on_theme_changed)
        head.addWidget(self.theme_combo, 0, Qt.AlignmentFlag.AlignTop)
        self.hotkey_btn = QPushButton('⌨')
        self.hotkey_btn.setObjectName('iconButton')
        self.hotkey_btn.setFixedSize(32, 32)
        self.hotkey_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hotkey_btn.setToolTip('快捷键设置：点某一行的按键，再直接按下想用的组合键就录进去了\n'
                                   '（默认 F6 / F7 / F8 / F10 / Ctrl+F1 / Ctrl+F2；托盘图标右键里也能开）')
        self.hotkey_btn.clicked.connect(self.open_hotkey_dialog)
        head.addWidget(self.hotkey_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.set_status('就绪', 'idle')

        card = QFrame()
        card.setObjectName('card')
        body = QVBoxLayout(card)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(10)

        pick = QHBoxLayout()
        pick.setSpacing(10)
        self.btn_choose = QPushButton('选择 MIDI 文件…')
        self.btn_choose.clicked.connect(self.choose_file)
        self.btn_audio = None
        if edition.has_audio():          # 精简版不带音频转 MIDI，这个按钮连做都不做
            self.btn_audio = QPushButton('音频转 MIDI…')
            self.btn_audio.setToolTip('选 mp3 / wav / flac，先转成 MIDI（主旋律单音轨 + 备用旋律 + 完整转谱），\n'
                                      '再走原来的流程。\n'
                                      '默认用 basic-pitch（Spotify 的开源转写模型，ONNX 推理）：有伴奏、\n'
                                      '有编曲也认得出主旋律；没装就退回自带的 YIN（只对独奏 / 清唱有效）。\n'
                                      '转完按「♪ 试听」听一遍，不对就在「选择」下拉框里换另一条旋律线重转。')
            self.btn_audio.clicked.connect(self.choose_audio)
        self.btn_songs = QPushButton('曲库')
        self.btn_songs.setToolTip('曲库：自带的那一份在 %s（默认那首是《鸟之诗》），\n'
                                  '双击就能读进来；浮层右下角那个「联网曲库」是网上的共享曲库。'
                                  % SONG_DIR)
        self.btn_songs.clicked.connect(self.open_library)
        self.btn_edit = None
        if edition.has_editor():         # 精简版不带编辑器
            self.btn_edit = QPushButton('简谱编辑器…')
            self.btn_edit.setToolTip('切开编辑器那一页，把当前这首铺成钢琴卷帘手动改：\n'
                                     '双击加音、右键删音、拖着改长短，改完导出 midi 会直接载回来')
            self.btn_edit.clicked.connect(self.open_editor)
        self.file_label = QLabel('还没有选择文件')
        self.file_label.setObjectName('value')
        self.file_label.setWordWrap(True)
        self.file_label.setMinimumWidth(90)
        pick.addWidget(self.btn_choose, 0)
        for button in (self.btn_audio, self.btn_songs, self.btn_edit):
            if button is not None:       # 精简版少两个按钮
                pick.addWidget(button, 0)
        pick.addWidget(self.file_label, 1)
        body.addLayout(pick)

        self.track_label = self._info_row(body, '音轨')

        pick_track = QHBoxLayout()
        pick_track.setSpacing(8)
        pick_tag = QLabel('选择')
        pick_tag.setObjectName('fieldLabel')
        pick_tag.setFixedWidth(32)
        self.track_box = InlineCombo()
        self.track_box.setToolTip('多音轨文件可以自己挑一条音轨；默认已经自动选了最像主旋律的那条')
        self.btn_track = QPushButton('用这条音轨重转')
        self.btn_track.clicked.connect(self.use_selected_track)
        self.btn_track.setEnabled(False)
        pick_track.addWidget(pick_tag, 0, Qt.AlignmentFlag.AlignVCenter)
        pick_track.addWidget(self.track_box, 1)
        pick_track.addWidget(self.btn_track, 0)
        body.addLayout(pick_track)

        self.tonic_label = self._info_row(body, '主音')
        self.score_label = self._info_row(body, '谱面')

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.btn_start = QPushButton('▶  开始演奏')
        self.btn_start.setObjectName('primary')
        self.btn_start.clicked.connect(lambda: self.hotkey.emit('start'))
        self.btn_pause = QPushButton('⏸  暂停')
        self.btn_pause.clicked.connect(lambda: self.hotkey.emit('pause'))
        self.btn_stop = QPushButton('■  停止')
        self.btn_stop.clicked.connect(lambda: self.hotkey.emit('stop'))
        self.btn_stop.setEnabled(False)
        self.btn_preview = QPushButton('♪  试听')
        self.btn_preview.setToolTip('把当前谱面用电子琴音色弹一遍，听听转出来的旋律对不对。\n'
                                    '按谱面原样时值放（不受「音长 / 速度」影响），再按一次就停。\n'
                                    '转完音频先听一遍，确认主旋律抓对了再进游戏。')
        self.btn_preview.clicked.connect(self.toggle_preview)
        self.btn_preview.setEnabled(False)          # 选完文件才能试听
        # 试听进度条：点一下 / 拖着就能跳到想听的地方。winsound 不能跳转，
        # 由 preview.play_from 把后面那截另存出来放，所以拖动照样好使。
        self.preview_slider = SeekSlider()
        self.preview_slider.setToolTip('试听进度：点一下或拖到想听的地方')
        self.preview_slider.setEnabled(False)
        self.preview_slider.seeked.connect(self.seek_preview)
        self.preview_slider.valueChanged.connect(self._show_preview_value)
        self.preview_time = QLabel('0:00 / 0:00')
        self.preview_time.setObjectName('counter')
        self.preview_time.setMinimumWidth(92)
        self.preview_time.setAlignment(Qt.AlignCenter)
        controls.addWidget(self.btn_start)
        controls.addWidget(self.btn_pause)
        controls.addWidget(self.btn_stop)
        # 录制按钮：只认游戏里那八个键，全程不开任何窗口，所以在游戏里也能点
        self.btn_record = None
        if edition.has_recorder():
            self.btn_record = QPushButton('⏺  录制')
            self.btn_record.setToolTip('按 %s（或点这里）开始录：这期间你在游戏里弹的每一个键都会\n'
                                       '被记下来 —— z x c v b n m , 加上鼠标左键(降调) /\n'
                                       '中键(升半音) / 右键(升调)。再按一下收工。\n'
                                       '录完自动写成 TONIC 谱面 + 单音 midi，并送进「简谱编辑器」等你修。\n'
                                       '录制期间别的键（跑步的 WASD、热键…）一律不录。'
                                       % hotkeys.pretty_combo(hotkeys.DEFAULT_BINDINGS['record']))
            self.btn_record.clicked.connect(self.toggle_record)
            controls.addWidget(self.btn_record)
        controls.addWidget(self.btn_preview)
        controls.addSpacing(4)
        controls.addWidget(self.preview_slider, 1)
        controls.addWidget(self.preview_time)
        controls.addSpacing(4)
        # 浮层形态下这两个入口会被临时改提示，先记下原本的
        self.desk_tips = dict((button, button.toolTip())
                              for button in (self.btn_edit, self.hotkey_btn)
                              if button is not None)
        body.addLayout(controls)

        # 第二行：怎么弹
        params = QHBoxLayout()
        params.setSpacing(8)
        params.addWidget(QLabel('音长'))
        self.note_mode = InlineCombo()
        self.note_mode.addItems(player.NOTE_MODES)
        self.note_mode.setFixedWidth(92)
        self.note_mode.setToolTip('「等长演奏」：每个音都按住「单音时长」这么久，长短一样、听着最稳，\n'
                                  '顶到下一个音时自动缩短（节奏还是原速，不把整首歌放慢）；\n'
                                  '「原样演奏」：按 midi 的时值来，长短不一、更有起伏，但短音可能被吞')
        self.note_mode.currentTextChanged.connect(self.on_note_mode)
        params.addWidget(self.note_mode)
        params.addWidget(QLabel('单音时长'))
        self.note_ms = QSpinBox()
        self.note_ms.setRange(player.NOTE_MS_RANGE[0], player.NOTE_MS_RANGE[1])
        self.note_ms.setValue(player.NOTE_MS_DEFAULT)
        self.note_ms.setSuffix(' ms')
        self.note_ms.setFixedWidth(86)
        self.note_ms.setToolTip('「等长演奏」时一个音按住多久（毫秒）。\n'
                                '不会改动曲子的快慢：下一个音来得太早时，这个音自己缩短。')
        self.note_ms.valueChanged.connect(self.on_note_ms)
        params.addWidget(self.note_ms)
        self.stretch_box = QCheckBox('整首放慢')
        self.stretch_box.setToolTip('谱面里有音比「单音时长」还短时，把整首歌等比放慢（等于降 BPM）来凑够长度，\n'
                                    '长短关系不变。\n'
                                    '不勾（默认）：节奏保持原速，挤在一起的音自己缩短 —— 听着跟原曲一样快。')
        self.stretch_box.toggled.connect(self.on_stretch)
        params.addWidget(self.stretch_box)
        params.addWidget(QLabel('最短按键'))
        self.hold_ms = QSpinBox()
        self.hold_ms.setRange(HOLD_MS[0], HOLD_MS[1])
        self.hold_ms.setValue(HOLD_MS_DEFAULT)
        self.hold_ms.setSuffix(' ms')
        self.hold_ms.setFixedWidth(86)
        self.hold_ms.setToolTip('实际按下时至少按住这么久（游戏吞音的兜底）。\n'
                                '「原样演奏」里特别短的音也至少按这么久；想每个音都按满就勾上\n'
                                '「整首放慢」。')
        params.addWidget(self.hold_ms)
        params.addWidget(QLabel('速度'))
        self.speed = InlineCombo()
        self.speed.addItems(SPEEDS)
        self.speed.setCurrentText('1.0')
        self.speed.setFixedWidth(78)
        self.speed.setToolTip('整体快慢。1.0 是谱面原速；速度调快会把单音时长一起压短，\n'
                              '太快的话游戏可能又开始吞音')
        params.addWidget(self.speed)
        params.addStretch(1)
        body.addLayout(params)

        opts = QHBoxLayout()
        opts.setSpacing(8)
        self.cover_box = QCheckBox('游戏内覆盖')
        self.cover_box.setToolTip('主界面去掉标题栏，Ctrl+F1 唤起时像录屏软件的浮层那样盖在游戏上：\n'
                                  '置顶显示，但**不抢游戏焦点** —— 独占全屏的游戏不会被打回桌面。\n'
                                  '连「选择 MIDI 文件…」和里面的文件框也不抢焦点。\n'
                                  '顶部空白处按住可以拖动浮层，右上角 ✕ 收起回游戏。\n'
                                  '停在「简谱编辑器」那一页、人又在桌面上时**不置顶**：\n'
                                  '这种时候要对着别的东西改谱，能正常切到别的窗口。\n'
                                  '代价：浮层收不到键盘（选文件用鼠标双击；要改编辑器里的\n'
                                  '数字、或者改快捷键，先回桌面再用）。')
        self.cover_box.toggled.connect(self.on_cover_toggled)
        opts.addWidget(self.cover_box)
        self.follow_box = None
        self.follow_pos = None
        self.follow_pace = None
        if FOLLOW_MODE:
            self.follow_box = QCheckBox('跟奏模式')
            self.follow_box.setToolTip('在游戏上盖一层下落式提示：长条的长度就是这个音要按多久，\n'
                                       '哪一列有键按下去就发光。鼠标能穿透，不影响演奏。')
            self.follow_box.toggled.connect(self.on_follow_toggled)
            opts.addWidget(self.follow_box)
            opts.addWidget(QLabel('跟奏位置'))
            self.follow_pos = InlineCombo()
            self.follow_pos.addItems(follow.FollowWindow.POSITIONS)
            self.follow_pos.setFixedWidth(94)
            self.follow_pos.currentTextChanged.connect(self.on_follow_pos)
            opts.addWidget(self.follow_pos)
            opts.addWidget(QLabel('跟奏节奏'))
            self.follow_pace = InlineCombo()
            self.follow_pace.addItems(follow.PACE_MODES)
            self.follow_pace.setFixedWidth(98)
            self.follow_pace.setToolTip('「等我按对」是练习模式：程序不发按键，音符停在判定线上\n'
                                        '等你按对那个键，按对了才继续往下走，新手不用被原曲速度拖着跑。\n'
                                        '「原速跟奏」就是按原曲的速度自动走。')
            self.follow_pace.currentTextChanged.connect(self.on_follow_pace)
            opts.addWidget(self.follow_pace)
        opts.addStretch(1)
        body.addLayout(opts)

        # 再一行：跟「录制」「工程文件关联」有关的开关 + 「谁在响」
        # （诊断按钮任何版本都有：这行就一定会建出来）
        opts2 = QHBoxLayout()
        opts2.setSpacing(8)
        self.monitor_box = None
        self.mute_box = None
        self.snap_combo = None
        if edition.has_recorder():
            self.monitor_box = QCheckBox('录制时发声')
            self.monitor_box.setToolTip('勾上：录制时按下一个琴键就实时响一声 —— 听得出自己弹的是哪个音、\n'
                                        '有没有按错。\n'
                                        '不勾：录制全程不出声。\n'
                                        '开就是开、关就是关，不去猜「现在在不在游戏里」—— 游戏里的音\n'
                                        '是游戏自己放的，嫌吵就自己把它关掉（快捷键 F10 那一行旁边就是它）。')
            self.monitor_box.setChecked(MONITOR_DEFAULT)
            self.monitor_box.toggled.connect(self.on_monitor_toggled)
            opts2.addWidget(self.monitor_box)
            self.mute_box = QCheckBox('录制时静音系统提示音')
            self.mute_box.setToolTip('勾上：一开始录制，就把「系统提示音」那一路按住（录制时蹦出来的\n'
                                     '那声「叮」多半就是它），收工自动放回去。\n'
                                     '只动系统那一路：游戏、音乐、程序自己的琴键声都不受影响。\n'
                                     '万一程序崩了没放回去，下次打开会自动放开。')
            self.mute_box.setChecked(MUTE_DEFAULT)
            self.mute_box.toggled.connect(self.on_mute_toggled)
            opts2.addWidget(self.mute_box)
            opts2.addWidget(QLabel('音长吸附'))
            self.snap_combo = InlineCombo()
            self.snap_combo.addItems(SNAP_CHOICES)
            self.snap_combo.setCurrentText(SNAP_DEFAULT)
            self.snap_combo.setFixedWidth(88)
            self.snap_combo.setToolTip('录完把每个音的时值吸到最近的格子上：按了 354 毫秒、格子上\n'
                                       '理论值该是 350 毫秒，就记成 350 毫秒 —— 手抖出来的零头\n'
                                       '不留进谱面。空档（休止）也一起吸，节奏才不会被切碎。\n'
                                       '「关」= 你按了多少毫秒就是多少毫秒。')
            self.snap_combo.currentTextChanged.connect(self.on_snap_changed)
            opts2.addWidget(self.snap_combo)
        self.assoc_box = None
        # 关联要写注册表指向「本 exe」；源码运行时没有 exe 可指，这个勾选框就不露出来
        if edition.has_editor() and fileassoc.exe_of():
            self.assoc_box = QCheckBox('双击 .mproj 工程文件用本程序打开')
            self.assoc_box.setToolTip('把 .mproj（简谱工程文件）关联到本程序：以后在资源管理器里双击它，\n'
                                      '程序会直接带着这个工程进「简谱编辑器」，不用先开程序再打开。\n'
                                      '只写在当前用户（HKCU）里，不需要管理员权限；取消勾选就撤掉。')
            self.assoc_box.toggled.connect(self.on_assoc_toggled)
            opts2.addWidget(self.assoc_box)
        self.diag_button = QPushButton('🔔 谁在响')
        self.diag_button.setToolTip('录制时蹦出一声提示音，不知道谁干的？点它，然后正常按你的键：\n'
                                    '接下来 %d 秒里只要有程序出声，就把它的名字记到下面日志里。\n'
                                    '（再点一次就提前收工）' % int(DIAG_SECONDS))
        self.diag_button.clicked.connect(self.on_diag)
        opts2.addWidget(self.diag_button)
        opts2.addStretch(1)
        body.addLayout(opts2)

        bar_row = QHBoxLayout()
        bar_row.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.counter = QLabel('0 / 0')
        self.counter.setObjectName('counter')
        self.counter.setFixedWidth(70)
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar_row.addWidget(self.bar, 1)
        bar_row.addWidget(self.counter, 0)
        body.addLayout(bar_row)
        root.addWidget(card)

        # 「控制台」：运行日志。默认藏着（信息太杂，挡着也占地方），
        # 托盘图标右键里点「运行日志（控制台）」就出来，选完文件才想看的人再开。
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(130)
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        root.addWidget(self.log_view, 1)
        self._log_visible = LOG_VISIBLE_DEFAULT
        self.log_view.setVisible(self._log_visible)

        # 覆盖模式下「选文件」就嵌在这块面板里 —— 不新建窗口，游戏一点都不会被打扰
        self.pick_panel = QFrame()
        self.pick_panel.setObjectName('card')
        self.pick_layout = QVBoxLayout(self.pick_panel)
        self.pick_layout.setContentsMargins(8, 8, 8, 8)
        self.pick_layout.setSpacing(6)
        self.pick_panel.setVisible(False)
        root.addWidget(self.pick_panel, 1)

        self.hint = QLabel('')
        self.hint.setObjectName('hint')
        self.hint.setWordWrap(True)      # 键位提示会随自定义变长，换行别把窗口撑宽
        root.addWidget(self.hint)

        # 编辑器那一页先占个位，第一次点进去才真的建（建起来要几百毫秒，
        # 而且它挺吃屏幕宽度，没打算用的人不用为它买单）
        self.editor_page = QWidget()
        self.editor_layout = QVBoxLayout(self.editor_page)
        self.editor_layout.setContentsMargins(0, 0, 0, 0)
        if edition.has_editor():
            self.tabs.addTab(self.editor_page, '简谱编辑器')
            self.tabs.currentChanged.connect(self._on_tab_changed)
        else:
            # 精简版只有「演奏」一页，一条标签孤零零挂着不好看，藏掉
            self.tabs.tabBar().setVisible(False)
        # 最大化按钮放标题行。标签栏右上角也试过，但那儿的样式被 QSS 改过、
        # 位置飘，不如老老实实摆在标题行里；编辑器那一页自己也有一个（见 editor.py）
        head.addWidget(self.max_button, 0, Qt.AlignmentFlag.AlignTop)

    def _set_file_label(self, path):
        """
        文件那一栏只写「文件名」，完整路径进 tooltip。

        整条路径（C:/Users/…/songs/鸟之诗.mid）在窄窗口里没有可换行的空格，
        Qt 会把它当成一个超长的「词」—— 结果这一行按 660px 算最小宽度，
        旁边的按钮全被挤到裁字。只留文件名既看得清，也不会把那一行撑开。
        """
        text = str(path or '').strip()
        if not text:
            self.file_label.setText('还没有选择文件')
            self.file_label.setToolTip('')
            return
        name = os.path.basename(text) or text
        self.file_label.setText(name)
        self.file_label.setToolTip(text)

    def _info_row(self, layout, name):
        row = QHBoxLayout()
        row.setSpacing(10)
        tag = QLabel(name)
        tag.setObjectName('fieldLabel')
        tag.setFixedWidth(32)
        value = QLabel('—')
        value.setObjectName('value')
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(tag, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(value, 1)
        layout.addLayout(row)
        return value

    def set_status(self, text, kind='idle'):
        self._status_last = (text, kind)      # 换主题时要用它把这一颗重上一遍
        foreground, alpha = STATUS_COLORS.get(kind, STATUS_COLORS['idle'])
        foreground = theme.c(foreground)
        background = _rgba(foreground, alpha)
        self.status.setText(text)
        self.status.updateGeometry()          # 文字变长了要重新排一次，别等下一帧才跟上
        self.status.setStyleSheet(
            'background: %s; color: %s; border-radius: 11px; padding: 4px 12px; font-size: 12px;'
            % (background, foreground))

    def log(self, text):
        lines = str(text).splitlines() or ['']
        self.log_view.append('[%s] %s' % (time.strftime('%H:%M:%S'), lines[0]))
        for line in lines[1:]:
            self.log_view.append(line)

    # ---------- 托盘 / 热键 ----------

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.log('系统托盘不可用（不影响演奏，右上角浮窗照常显示）')
            return
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip('%s v%s' % (APP_TITLE, APP_VERSION))
        # 菜单要自己留一个引用：Qt 的托盘图标只存裸指针，Python 那边一回收就会崩
        self.tray_menu = QMenu()
        self.hotkey_actions = {
            'show': self.tray_menu.addAction('显示窗口', lambda *_: self.show_window()),
        }
        self.tray_menu.addSeparator()
        self.hotkey_actions['start'] = self.tray_menu.addAction(
            '开始演奏', lambda: self.hotkey.emit('start'))
        self.hotkey_actions['pause'] = self.tray_menu.addAction(
            '暂停 / 继续', lambda: self.hotkey.emit('pause'))
        self.hotkey_actions['stop'] = self.tray_menu.addAction(
            '停止', lambda: self.hotkey.emit('stop'))
        self.tray_menu.addSeparator()
        self.tray_menu.addAction('显示 / 隐藏进度浮窗', self.toggle_overlay)
        self.cover_action = self.tray_menu.addAction('游戏内覆盖', self.toggle_cover)
        self.cover_action.setCheckable(True)
        self.tray_menu.addSeparator()
        self.follow_action = self.tray_menu.addAction('跟奏模式', self.toggle_follow)
        self.hotkey_actions['follow'] = self.follow_action
        self.follow_action.setCheckable(True)
        if not FOLLOW_MODE:
            self.follow_action.setEnabled(False)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction('快捷键设置…', self.open_hotkey_dialog)
        self.log_action = self.tray_menu.addAction('运行日志（控制台）', self.toggle_log)
        self.log_action.setCheckable(True)
        self.log_action.setToolTip('主界面下面那块运行日志：出问题 / 想知道程序走到哪一步时打开')
        self.theme_menu = self.tray_menu.addMenu('配色主题')
        self.theme_actions = {}
        self._fill_theme_menu()
        self.tray_menu.addSeparator()
        self.tray_menu.addAction('退出', lambda: self.hotkey.emit('quit'))
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray)
        self.tray.show()

    def _sync_editor_log(self):
        """
        编辑器里那一条日志跟着同一个开关走。

        控制台关着的时候，编辑器那一页也只留画布 —— 日志还是照常往缓存里写，
        托盘里点开「运行日志（控制台）」就能一起看到。
        """
        ed = getattr(self, 'editor', None)
        if ed is None:
            return
        try:
            ed.log_view.setVisible(self._log_visible)
        except Exception:
            pass

    def toggle_log(self, checked=None):
        """
        显示 / 隐藏主界面下面那块运行日志（控制台）。

        默认是**不显示**的：那堆「读了哪个文件、哪一行报错」的信息平时用不上，
        挂在界面上又杂又占地方。要看的就在托盘图标上右键点它（跟快捷键设置一个路子），
        选择会被记住，下次打开还是上次的样子。
        """
        want = (not self._log_visible) if checked is None else bool(checked)
        self._log_visible = want
        self.log_view.setVisible(want)
        self._sync_editor_log()
        if getattr(self, 'log_action', None) is not None:
            self.log_action.setChecked(want)
        try:
            self.settings.setValue('log_visible', want)
        except Exception:
            pass
        if want:
            # 刚打开时窗口可能太矮，日志挤不出地方 —— 顺手把窗口拉高一点
            need = self.play_page.minimumSizeHint().height() + 24
            if self.height() < need:
                screen = self.screen().availableGeometry() if self.screen() else None
                limit = screen.height() - 140 if screen else need
                self.resize(self.width(), int(max(need, min(limit, need))))
            self.log('运行日志（控制台）打开了：再点一次托盘里那一项就收起')
        else:
            self._fit_window_height()       # 收起来就顺手把窗口收一下，别留着空地

    def toggle_overlay(self):
        """显示 / 隐藏右上角的进度浮窗。"""
        self.overlay_on = not self.overlay_on
        if self.overlay_on:
            self.overlay.set_ready(len(self.score_events) or None)
        else:
            self.overlay.shutdown()
        log_event('进度浮窗：%s' % ('显示' if self.overlay_on else '隐藏'))

    def _overlay_after_finish(self):
        """浮窗把结果亮完之后：有谱面就切回「已就绪」并继续显示，否则收起来。"""
        if self.overlay_on and self.score_events:
            self.overlay.set_ready(len(self.score_events))
            return True
        return False

    def _on_tray(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def show_window(self, in_game=False):
        """
        游戏里按 Ctrl+F1 / 点托盘：把主界面顶到游戏上面来。

        in_game=True 表示是按 Ctrl+F1 从游戏里唤起来的 —— 这种时候窗口走「浮层形态」，
        选文件也走浮层里的列表，一个窗口都不弹，游戏掉不回桌面。
        点托盘图标属于桌面上的操作（in_game=False），照旧是普通窗口 + 系统文件框。
        """
        fullscreen = foreground_is_fullscreen()      # 唤起之前谁占着全屏（多半就是游戏）
        overlay = self.cover_mode or bool(in_game and fullscreen)
        self._summoned_in_game = bool(in_game and overlay)
        log_event('唤起主窗口')
        # 先把形态摆好再显示：显示的那一刻就已经是「不接受焦点」的窗口了，
        # 顺序反过来的话，show 这一下自己就会把前台从游戏那儿抢走。
        self._apply_window_mode(overlay)
        try:
            self.showNormal()          # 万一之前被最小化了，先恢复出来
        except Exception:
            pass
        if self.overlay_active:
            set_topmost(self, not self._editing_on_desktop())
            self._topmost_on = not self._editing_on_desktop()
            show_no_activate(self)
            self._watch_game_focus_start(fullscreen)
            log_event('主窗口以浮层方式贴到游戏上（不抢焦点：点它、按它里面的按钮，游戏都不会掉回桌面）')
        else:
            self.raise_()
            front = force_window_front(self)
            log_event('主窗口已顶到最前%s'
                      % ('' if front else '（没抢到前台焦点：游戏可能是独占全屏，改成无边框窗口试试）'))
        if self.topmost_timer is not None:
            self.topmost_timer.start(TOPMOST_MS)

    # ---------- 游戏内覆盖模式 ----------

    def _apply_window_mode(self, overlay=None):
        """
        切换窗口形态。

        overlay=True：像跟奏框那样贴在最上面 —— 无边框 + 总在最前 + **不接受焦点**，
            点它、点它里面的按钮都不会把前台从游戏那儿抢走，游戏也就不会掉回桌面。
        overlay=False：普通窗口，有标题栏、能打字，桌面上用着方便。

        浮层这套配方跟跟奏框（follow.py）一模一样，是实机验证过「点不抢焦点」的那一套：
        WindowDoesNotAcceptFocus 能让 Qt 自己一直维持 WS_EX_NOACTIVATE。光靠手动
        SetWindowLong 不够 —— 窗口一重建（setWindowFlags / 显示）那个位就可能被 Qt
        顺手抹掉，于是「点一下就掉回桌面」又回来了。
        """
        if overlay is None:
            overlay = self.cover_mode
        try:
            if overlay and self._maximized:
                self.showNormal()          # 浮层形态不最大化：它就是贴着游戏的一小块
            visible = self.isVisible()
            pos = self.pos()
            self.overlay_active = bool(overlay)
            flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
            if self.overlay_active:
                flags |= Qt.WindowType.WindowDoesNotAcceptFocus
                # 桌面上用编辑器的时候不要 WindowStaysOnTopHint：一直压在最上面，
                # 想切到别的窗口就只能先最小化（见 _editing_on_desktop）。
                if not self._editing_on_desktop():
                    flags |= Qt.WindowType.WindowStaysOnTopHint
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
                self.setWindowOpacity(self._overlay_opacity())
            else:
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
                self.setWindowOpacity(OVERLAY_OPACITY_EDITOR)
            self.setWindowFlags(flags)
            self._topmost_on = None          # 标志重设过了，置顶状态得重新算一遍
            # 再手动补一层：Qt 万一没认这个位，咱自己保证它一定在。
            set_no_activate(self, self.overlay_active)
            if self.cover_close is not None:
                self.cover_close.setVisible(self.overlay_active)
            self.setProperty('cover', 'true' if self.cover_mode else 'false')
            self.style().unpolish(self)      # 动态属性改了要重新套一遍样式
            self.style().polish(self)
            if visible:                    # setWindowFlags 会把窗口藏起来，得重新显示
                if self.overlay_active:
                    show_no_activate(self)  # 要显示，但绝不抢前台
                else:
                    self.show()
                self.move(pos)
                if self._maximized and not self.overlay_active:
                    self.showMaximized()   # 换个形态别把最大化弄丢了
            self._sync_game_ui()
            self._sync_editor_focus()
            self._sync_overlay_topmost()
        except Exception as exc:
            self.log('切换窗口模式出错：%s' % exc)

    def _editor_page_active(self):
        """现在停在「简谱编辑器」那一页吗。"""
        return (getattr(self, 'editor_page', None) is not None
                and getattr(self, 'tabs', None) is not None
                and self.tabs.currentWidget() is self.editor_page)

    def _editing_on_desktop(self):
        """
        是不是「在桌面上用编辑器」。

        这种时候主窗口**不置顶**：编辑器是用来对着别的东西改谱的（查资料、看原谱、
        开个播放器），窗口一直压在最上面的话，想切到别的窗口就只能先把它最小化，
        很烦。放下置顶之后它就是个普通窗口，alt+tab / 点别的窗口都正常。

        游戏里不这么干 —— 那时候浮层必须贴着游戏，这是另一码事。
        """
        return self._editor_page_active() and not self.in_game()

    def _overlay_opacity(self):
        """
        浮层形态下，整个窗口该有多透明。

        编辑器那一页给 1.0（不透明）：那块地方挤满了色块、数字和标签，半透明会让
        底下的游戏画面和它搅在一起，看久了眼睛累。别的页照旧留一点透明 —— 在游戏
        里唤起主界面就是为了「能看见游戏、还能改点东西」，全挡住反而不好用。
        """
        return OVERLAY_OPACITY_EDITOR if self._editor_page_active() else OVERLAY_OPACITY

    def _sync_overlay_opacity(self):
        """换页之后把浮层不透明度补上（编辑器页要变回不透明）。"""
        if not getattr(self, 'overlay_active', False):
            return
        try:
            self.setWindowOpacity(self._overlay_opacity())
        except Exception:
            pass

    def _sync_overlay_topmost(self):
        """
        换页 / 进出游戏之后，把「置顶」这个状态补对。

        桌面上停在编辑页 -> 撤掉置顶（能正常切到别的窗口）；
        别的页、或者人在游戏里 -> 该置顶就置顶。

        这里只动 Win32 的 topmost 位（SetWindowPos），不去 setWindowFlags ——
        重设窗口标志会把窗口藏一下再显示，编辑到一半闪一下、焦点还可能丢，不划算。
        """
        if not getattr(self, 'overlay_active', False):
            return
        want = not self._editing_on_desktop()
        if getattr(self, '_topmost_on', None) != want:
            set_topmost(self, want)
            self._topmost_on = want

    def _sync_game_ui(self):
        """
        在游戏里的时候，把「一点就要弹新窗口」的入口收起来。

        系统文件框、改快捷键的对话框……任何一个新窗口都会把全屏游戏顶回桌面，
        而这是用户最烦的那件事。宁可在游戏里点不动，也别把游戏顶掉。

        注意判据是「现在在不在游戏里」，不是「窗口是不是浮层形态」：桌面上用着
        浮层（覆盖模式默认开着）时，这两个入口必须照常能点。
        """
        overlay = self.in_game()
        for widget, hint in ((getattr(self, 'hotkey_btn', None),
                              '游戏里不改快捷键（录键要抢焦点，会把游戏顶回桌面）；回桌面再改'),
                             (getattr(self, 'btn_edit', None),
                              '游戏里不开简谱编辑器（编辑器处处要弹窗口）；回桌面再用')):
            if widget is None:
                continue
            if overlay:
                widget.setEnabled(False)
                widget.setToolTip(hint)
            else:
                widget.setEnabled(True)
                if widget in self.desk_tips:
                    widget.setToolTip(self.desk_tips[widget])

    def in_game(self):
        """
        现在该不该按「在游戏里」的规矩来（选文件走浮层列表，绝不弹任何新窗口）。

        两种都算：
          - 这次是 Ctrl+F1 从游戏里唤起来的（这一条是**粘**的：一直算到收起浮层、
            或者从托盘重新把窗口叫出来为止。宁可多按一会儿游戏里的规矩，也不能让
            系统文件框在游戏里冒出来）；
          - 或者前台本来就压着一个全屏程序 —— 这条管的是「程序在桌面上开着，
            用户直接切进游戏」的情况，那种时候点浮层上的「选择文件」同样不该弹窗。
        """
        if self._summoned_in_game:
            return True
        return bool(foreground_is_fullscreen())

    def _watch_game_focus_start(self, game_hwnd=None):
        """
        开始盯着「现在到底在不在游戏里」。

        窗口露着的这段时间一直跑（250 毫秒看一眼，就两次 Win32 调用），干两件事：
          1. 「在不在游戏里」一变就跟上 —— 游戏里那些会弹窗的入口要立刻灰掉，
             回到桌面又要立刻能点；
          2. game_hwnd 是唤起前占着全屏的那个窗口（多半就是游戏）：这期间万一前台被
             我们抢了，立刻还给它。
        """
        if game_hwnd:
            self._game_hwnd = int(game_hwnd)
            self._watch_left = GAME_WATCH_TRIES
        if self.game_watch is not None and not self.game_watch.isActive():
            self.game_watch.start(GAME_WATCH_MS)

    def _watch_game_focus_stop(self):
        """窗口藏起来了：不看了，兜底对象也忘掉。"""
        self._game_hwnd = 0
        self._watch_left = 0
        self._last_in_game = None
        if self.game_watch is not None:
            self.game_watch.stop()

    def _watch_game_focus(self):
        """250 毫秒看一眼：状态变了就跟上，前台被我们抢了就把游戏推回去。"""
        if not self.isVisible():
            self._watch_game_focus_stop()
            return
        now = self.in_game()
        if now != self._last_in_game:
            self._last_in_game = now
            self._sync_game_ui()
            self._sync_overlay_topmost()          # 进出游戏，置顶的规矩不一样
            log_event('进游戏了：浮层里的入口按游戏规矩来' if now else '回桌面了：系统文件框、编辑器都恢复')
        if now and self.overlay_active:
            self._give_foreground_back()

    def _give_foreground_back(self):
        """
        兜底：万一前台还是被我们抢了（个别游戏、个别控件会这样），立刻还给游戏。
        这样「点一下就掉回桌面」就不可能发生。

        只在「前台变成了我们自己的窗口」时出手 —— 用户自己 Alt+Tab 去开浏览器之类的，
        前台是别人的窗口，这里一概不碰。
        """
        if self._watch_left <= 0:
            return
        hwnd = self._game_hwnd
        if not hwnd:
            return
        try:
            alive = bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd)))
        except Exception:
            alive = False
        if not alive:
            self._game_hwnd = 0
            self._watch_left = 0
            return
        front = foreground_hwnd()
        if not front or front == hwnd:
            return                                  # 前台还在游戏那儿（正常），不用管
        if window_pid(front) != os.getpid():
            return                                  # 用户自己切走了，别去抢
        self._watch_left -= 1
        if foreground_to(hwnd):
            log_event('把前台还给游戏（浮层差点把游戏顶掉）')

    def on_cover_toggled(self, enabled):
        """界面上勾 / 取消「游戏内覆盖」。"""
        self.cover_mode = bool(enabled)
        self.settings.setValue('cover', self.cover_mode)
        if not self.cover_mode:
            self._summoned_in_game = False   # 覆盖关了就别再按游戏里的规矩来
        self._apply_window_mode()
        if self.cover_action is not None:
            self.cover_action.blockSignals(True)
            self.cover_action.setChecked(self.cover_mode)
            self.cover_action.blockSignals(False)
        self._refresh_hotkey_labels()
        self.log('游戏内覆盖：%s' % ('开（Ctrl+F1 只置顶、不抢焦点）' if self.cover_mode else '关'))

    def toggle_cover(self):
        """托盘菜单里点「游戏内覆盖」，转手交给勾选框处理。"""
        self.cover_box.setChecked(not self.cover_box.isChecked())

    def hide_cover(self):
        """收起浮层回游戏（Esc / 右上角 ✕）。窗口只是藏起来，演奏和热键照常。"""
        if not self.isVisible():
            return
        set_topmost(self, False)
        self.hide()
        self._watch_game_focus_stop()
        self._summoned_in_game = False     # 再唤起来（点托盘）就按桌面规矩：系统文件框
        log_event('收起主窗口，回游戏')

    def showEvent(self, event):
        """每次露头都把「不抢焦点」补回去：Qt 显示窗口时会把扩展样式重写一遍。"""
        super().showEvent(event)
        if self.overlay_active:
            set_no_activate(self, True)
        self._watch_game_focus_start()
        # 头一次露头时量一下真实高度：控制台关着的话窗口用不了 700 那么高。
        # 得等一轮事件循环再量 —— 这一刻布局还没把「日志藏起来了」算进去。
        if not self._fitted:
            self._fitted = True
            QTimer.singleShot(120, self._fit_window_height)

    def _fit_window_height(self):
        """
        控制台关着的时候，界面下边会剩一大块空地 —— 顺手把窗口收一下。

        只在「没开控制台 / 没最大化 / 不是被 Ctrl+F1 从游戏里唤起来」的时候收：
        开着控制台要地方；游戏里唤起来的那个浮层位置是算好的，别去动它。
        （桌面上普通形态和浮层形态都能收 —— 启动默认就是浮层形态。）
        """
        if self._log_visible or self._maximized or self._summoned_in_game:
            return
        try:
            need = (self.play_page.sizeHint().height()
                    + self.tabs.tabBar().height() + 18)
        except Exception:                            # pragma: no cover
            return
        screen = self.screen().availableGeometry() if self.screen() else None
        limit = screen.height() - 140 if screen else need
        need = int(max(430, min(need, limit)))
        if abs(self.height() - need) > 8:
            self.resize(self.width(), need)

    def _on_esc(self):
        if self.overlay_active:
            self.hide_cover()
        else:
            self.quit_app('窗口里按了 ESC')

    def mousePressEvent(self, event):
        """覆盖模式下按住顶部空白处可以拖动浮层（无边框窗口没有标题栏）。"""
        if (self.cover_mode and event.button() == Qt.MouseButton.LeftButton
                and event.position().toPoint().y() <= DRAG_H):
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击顶部空白处也能最大化 / 还原（无边框窗口没有标题栏）。"""
        if (self.cover_mode and event.button() == Qt.MouseButton.LeftButton
                and event.position().toPoint().y() <= DRAG_H):
            self.toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def toggle_maximize(self):
        """最大化 / 还原。编辑工程的时候铺满整块屏幕，卷帘看得多、改着方便。"""
        if self.windowState() & Qt.WindowState.WindowMaximized:
            self.showNormal()
            log_event('主窗口还原')
        else:
            self.showMaximized()
            log_event('主窗口最大化')

    def _refresh_max_button(self):
        """右上角那个按钮：按状态换字、换配色和提示。"""
        if getattr(self, 'max_button', None) is not None:
            self.max_button.setText('▣' if self._maximized else '□')
            self.max_button.setToolTip('还原窗口（也可以按 F11）' if self._maximized
                                       else '最大化 / 还原（也可以按 F11）')
            self.max_button.setProperty('maxed', bool(self._maximized))
            self.max_button.style().unpolish(self.max_button)
            self.max_button.style().polish(self.max_button)
        # 编辑器那一页自己也有一个一样的按钮，跟着一起变（别一个亮一个暗）
        editor_page = getattr(self, 'editor', None)
        if editor_page is not None:
            try:
                editor_page._refresh_max_button()
            except Exception:
                pass

    def changeEvent(self, event):
        """盯着「是不是最大化了」，按钮上的字跟着变。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._maximized = bool(self.windowState() & Qt.WindowState.WindowMaximized)
            self._refresh_max_button()

    def _drop_topmost(self):
        """唤起之后过一阵：窗口已经不在用了就撤掉「总在最前」，别一直挡着游戏。"""
        if self._editing_on_desktop():
            # 桌面上正用着编辑器：本来就该是「普通窗口」，直接撤掉置顶
            if set_topmost(self, False):
                self._topmost_on = False
            return
        # 文件对话框开着的时候也算「还在用」：撤了置顶对话框会沉到游戏后面，
        # 用户正挑着 midi 突然看不见了。
        dialog = QApplication.activeModalWidget() or QApplication.activeWindow()
        if (not self.isVisible() or self.isActiveWindow() or dialog is not None
                or self._choosing is not None or self._playing()):
            return
        if set_topmost(self, False):
            self._topmost_on = False
            log_event('主窗口取消置顶')

    def _step_aside(self):
        """
        演奏时把主窗口让开：只下沉，不隐藏也不最小化。

        隐藏会让程序「切到后台」，热键和托盘都容易出毛病；最小化又会把全屏游戏
        顶回桌面。下沉之后窗口还在，游戏不会被顶掉，热键也照常。
        """
        try:
            set_topmost(self, False)      # 撤掉唤起时的置顶，免得一直压在游戏上面
            self.lower()
        except Exception:
            pass

    def _setup_hotkeys(self):
        """全局热键：自己装的低级钩子 + keyboard 库，双保险，见 hotkeys.py。"""
        try:
            self.hotkeys = hotkeys.HotkeyListener(on_action=self.hotkey.emit,
                                                  log=self.message.emit,
                                                  keys=self.keymap)
            self.hotkeys.start()
        except Exception as exc:
            self.hotkeys = None
            self.log('全局热键不可用（界面按钮仍能用）：%s' % exc)
            return
        self.hotkey_timer = QTimer(self)
        self.hotkey_timer.setInterval(5000)
        self.hotkey_timer.timeout.connect(self._refresh_hotkeys)
        self.hotkey_timer.start()

    def _refresh_hotkeys(self):
        """定时自检：钩子被系统摘掉或者线程死了就重新装，免得演奏到一半失灵。"""
        try:
            self.hotkeys.refresh()
        except Exception as exc:
            self.log('热键自检出错：%s' % exc)

    # ---------- 快捷键自定义 ----------

    def _load_hotkey_map(self):
        """从设置里读回用户改过的键位；没改过的那几项就用默认值。"""
        saved = {}
        for action in hotkeys.ACTION_ORDER:
            value = self.settings.value('hotkey_' + action, None)
            if value:
                saved[action] = str(value)
        return hotkeys.HotkeyMap(saved)

    def _save_hotkey_map(self):
        for action, combo in self.keymap.items():
            self.settings.setValue('hotkey_' + action, combo)

    def open_hotkey_dialog(self):
        """界面 / 托盘里点「快捷键设置」，改完立刻生效，不用重启。"""
        if self.in_game():
            # 录键必须抢键盘焦点，那就一定会把全屏游戏顶回桌面 —— 游戏里干脆不给开
            self.log('游戏里不改快捷键（录键要抢焦点，会把游戏顶回桌面）；回到桌面再改')
            return
        dialog = HotkeyDialog(self.keymap, self)
        # 对话框开着的时候先关掉全局热键：不然录 F6 的时候顺手就把演奏开起来了
        if self.hotkeys is not None:
            self.hotkeys.dispatcher.enabled = False
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            if self.hotkeys is not None:
                self.hotkeys.dispatcher.enabled = True
        if not accepted:
            return
        if dialog.keymap.items() == self.keymap.items():
            return                       # 没改动就别折腾
        self.keymap.replace(dict(dialog.keymap.items()))
        self._save_hotkey_map()
        if self.hotkeys is not None:
            try:
                self.hotkeys.rebind()    # keyboard 库那份重新注册
            except Exception as exc:
                self.log('重新注册热键出错（自带钩子照常）：%s' % exc)
        self._refresh_hotkey_labels()
        self.log('快捷键改成：%s' % '、'.join('%s %s' % (hotkeys.pretty_combo(c),
                                                        hotkeys.ACTION_LABELS[a])
                                              for a, c in self.keymap.items()))

    def _refresh_hotkey_labels(self):
        """界面底部提示 + 托盘菜单里的键位，都跟着当前键位表走。"""
        try:
            tail = ('右上角 × 收起回游戏' if self.cover_mode else '窗口里按 ESC 退出')
            self.hint.setText(' · '.join('%s %s' % (hotkeys.pretty_combo(c),
                                                    hotkeys.ACTION_LABELS[a])
                                         for a, c in self.keymap.items())
                              + ' · %s；托盘图标右键：快捷键设置 / 运行日志 / 退出' % tail)
            for action, item in self.hotkey_actions.items():
                combo = self.keymap.combo_of(action)
                label = TRAY_HOTKEY_LABELS.get(action, hotkeys.ACTION_LABELS[action])
                item.setText('%s (%s)' % (label, hotkeys.pretty_combo(combo))
                             if combo else label)
        except Exception:
            pass

    def _on_hotkey(self, action):
        if action == 'start':
            self.start_play()
        elif action == 'show':
            # Ctrl+F1 是开关：没露头就唤出来，已经贴在游戏上了就再按一下收回去
            # （只是藏起来，程序照常跑、热键照常管用；要退出走托盘右键菜单）
            if self.isVisible():
                self.hide_cover()
            else:
                self.show_window(in_game=True)
        elif action == 'follow':
            self.toggle_follow()
        elif action == 'pause':
            if self.practice:
                paused = self.follow.toggle_pause()
                self.set_status('已暂停' if paused else '跟奏中', 'pause' if paused else 'play')
                self.overlay.set_state('pause' if paused else 'play')
                log_event('练习暂停' if paused else '练习继续')
                return
            self.player.toggle_pause()
            if self.player.running.is_set():
                paused = self.player.pause_flag.is_set()
                self.set_status('已暂停' if paused else '演奏中', 'pause' if paused else 'play')
                self.overlay.set_state('pause' if paused else 'play')
                self._sync_follow_state('pause' if paused else 'play')
                log_event('暂停' if paused else '继续')
        elif action == 'stop':
            if self.practice:
                log_event('停止跟奏练习')
                if self.follow is not None:
                    self.follow.cancel()
                self._on_practice_finish('stop')
                return
            if self.player.running.is_set():
                log_event('停止')
            self.player.stop()
        elif action == 'record':
            self.toggle_record()
        elif action == 'quit':
            self.quit_app('按了退出')

    # ---------- 设置 ----------

    def _load_settings(self):
        """
        读出上次的选择，让界面跟它一致。

        没有存过设置就用各项的默认值（见各自的 DEFAULT）。
        """
        if FOLLOW_MODE:
            self.follow_on = bool(self.settings.value('follow', False, type=bool))
            if self.follow_pos is not None:
                self.follow_pos.setCurrentText(follow.FollowWindow.resolve_position(
                    self.settings.value('follow_pos', follow.FollowWindow.POSITIONS[0])))
            if self.follow_pace is not None:
                self.follow_pace.setCurrentText(
                    str(self.settings.value('follow_pace', follow.PRACTICE_PACE)))
            self._sync_follow_ui(self.follow_on)
        # 音长：怎么弹
        if self.note_mode is not None:
            mode = str(self.settings.value('note_mode', player.NOTE_MODE_DEFAULT))
            self.note_mode.setCurrentText(mode if mode in player.NOTE_MODES
                                          else player.NOTE_MODE_DEFAULT)
        if self.note_ms is not None:
            try:
                value = int(self.settings.value('note_ms', player.NOTE_MS_DEFAULT))
            except (TypeError, ValueError):
                value = player.NOTE_MS_DEFAULT
            self.note_ms.setValue(min(max(value, player.NOTE_MS_RANGE[0]),
                                      player.NOTE_MS_RANGE[1]))
        if self.stretch_box is not None:
            self.stretch_box.blockSignals(True)
            self.stretch_box.setChecked(bool(self.settings.value('stretch', False, type=bool)))
            self.stretch_box.blockSignals(False)
        # 录制时发声（在游戏里录会自动静音，见 _rec_note）
        if self.monitor_box is not None:
            self._monitor_on = bool(self.settings.value('monitor', MONITOR_DEFAULT, type=bool))
            self.monitor_box.blockSignals(True)
            self.monitor_box.setChecked(self._monitor_on)
            self.monitor_box.blockSignals(False)
        # 控制台（运行日志）显不显示
        try:
            want_log = bool(self.settings.value('log_visible', LOG_VISIBLE_DEFAULT, type=bool))
        except Exception:
            want_log = LOG_VISIBLE_DEFAULT
        self._log_visible = want_log
        self.log_view.setVisible(want_log)
        if getattr(self, 'log_action', None) is not None:
            self.log_action.setChecked(want_log)
        # 配色主题（主界面、浮窗、跟奏、编辑器一起换）
        theme.load_all()
        want_theme = theme.set_current(str(self.settings.value('theme', theme.DEFAULT_NAME)))
        if getattr(self, 'theme_combo', None) is not None:
            self.theme_combo.blockSignals(True)
            self.theme_combo.clear()
            self.theme_combo.addItems(theme.names())
            self.theme_combo.setCurrentText(want_theme)
            self.theme_combo.blockSignals(False)
        self._fill_theme_menu()
        self.refresh_style()
        # 音长吸附
        if self.snap_combo is not None:
            self.snap_combo.blockSignals(True)
            self.snap_combo.setCurrentText(str(self.settings.value('snap', SNAP_DEFAULT)))
            self.snap_combo.blockSignals(False)
        # 录制时静音系统提示音
        if self.mute_box is not None:
            self._mute_system = bool(self.settings.value('mute_system', MUTE_DEFAULT, type=bool))
            self.mute_box.blockSignals(True)
            self.mute_box.setChecked(self._mute_system)
            self.mute_box.blockSignals(False)
        # 工程文件关联：勾选框照着注册表里的现状来（用户可能在别处改过）
        if self.assoc_box is not None:
            self.assoc_box.blockSignals(True)
            self.assoc_box.setChecked(fileassoc.is_registered(fileassoc.exe_of()))
            self.assoc_box.blockSignals(False)
        # 游戏内覆盖
        self.cover_mode = bool(self.settings.value('cover', True, type=bool))
        if self.cover_box is not None:
            self.cover_box.blockSignals(True)
            self.cover_box.setChecked(self.cover_mode)
            self.cover_box.blockSignals(False)
        self._apply_window_mode()
        self._refresh_hotkey_labels()

    def on_note_mode(self, mode):
        """换了「音长」：演奏和跟奏都要按新时值重算。"""
        self.settings.setValue('note_mode', str(mode))
        self._refresh_follow(show=self.follow_on)
        self.log('音长：%s' % mode)

    def on_note_ms(self, value):
        """换了「单音时长」。"""
        self.settings.setValue('note_ms', int(value))
        self._refresh_follow(show=self.follow_on)

    def on_stretch(self, enabled):
        """勾了「整首放慢」：谱面要重算一遍（演奏和跟奏都得跟着变）。"""
        self.settings.setValue('stretch', bool(enabled))
        self._refresh_follow(show=self.follow_on)
        self.log('整首放慢：%s' % ('开（曲子会被放慢）' if enabled else '关（保持原速）'))

    def on_monitor_toggled(self, enabled):
        """勾了「录制时发声」：记进设置，正在录的话下一个音就生效。"""
        self._monitor_on = bool(enabled)
        self.settings.setValue('monitor', self._monitor_on)
        self.log('录制时发声：%s' % ('开 —— 按下去会响' if enabled else '关 —— 录制全程不出声'))
        if not enabled and self.note_sound is not None:
            self.note_sound.stop()

    def _snap_ms(self):
        """界面上「音长吸附」选的是几毫秒（「关」是 0）。"""
        text = ''
        try:
            text = str(self.snap_combo.currentText()).strip()
        except Exception:
            return 0
        if not text or text == '关':
            return 0
        try:
            return int(float(text.split()[0]))
        except (ValueError, IndexError):
            return 0

    def on_snap_changed(self, text):
        """换了「音长吸附」：记进设置，下次打开还是它。"""
        self.settings.setValue('snap', str(text))
        self.log('音长吸附：%s' % ('关 —— 按多少毫秒就记多少毫秒' if self._snap_ms() == 0
                                 else '把每个音吸到最近的 %d 毫秒格子上' % self._snap_ms()))

    def _fill_theme_menu(self):
        """把「配色主题」子菜单按现在有的主题填一遍。"""
        if getattr(self, 'theme_menu', None) is None:
            return
        self.theme_menu.clear()
        self.theme_actions = {}
        current = theme.current_name()
        for name in theme.names():
            action = self.theme_menu.addAction(
                name, lambda _checked=False, n=name: self.on_theme_changed(n))
            action.setCheckable(True)
            action.setChecked(name == current)
            tip = theme.describe(name)
            if tip:
                action.setToolTip(tip)
            self.theme_actions[name] = action
        self.theme_menu.addSeparator()
        self.theme_menu.addAction('重新载入主题文件', self.reload_themes)
        self.theme_menu.addAction('打开主题文件夹', self.open_theme_folder)

    def refresh_style(self):
        """照当前主题把样式表重上一遍（启动、换主题、重新载入都走这儿）。"""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style_sheet())
        if getattr(self, 'editor', None) is not None:
            try:
                self.editor.apply_style()
            except Exception:
                pass
        last = getattr(self, '_status_last', None)
        if last is not None and getattr(self, 'status', None) is not None:
            self.set_status(*last)              # 状态胶囊那点颜色也要跟着换
        for win in (getattr(self, 'overlay', None), getattr(self, 'follow', None)):
            if win is not None:
                try:
                    win.update()
                except Exception:
                    pass

    def on_theme_changed(self, name):
        """换了配色主题：整套界面（含浮窗 / 跟奏 / 编辑器）立刻跟上。"""
        name = theme.set_current(str(name))
        try:
            self.settings.setValue('theme', name)
        except Exception:
            pass
        if getattr(self, 'theme_combo', None) is not None:
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(name)
            self.theme_combo.blockSignals(False)
        for key, action in (getattr(self, 'theme_actions', None) or {}).items():
            action.setChecked(key == name)
        self.refresh_style()
        self.log('配色主题：%s%s' % (name, ('（%s）' % theme.describe(name)) if theme.describe(name) else ''))

    def reload_themes(self):
        """托盘里「重新载入主题文件」：改完 json 不用重启程序。"""
        theme.load_all()
        if getattr(self, 'theme_combo', None) is not None:
            self.theme_combo.blockSignals(True)
            self.theme_combo.clear()
            self.theme_combo.addItems(theme.names())
            self.theme_combo.setCurrentText(theme.current_name())
            self.theme_combo.blockSignals(False)
        self._fill_theme_menu()
        self.on_theme_changed(theme.current_name())
        self.log('主题文件重新读过了，一共 %d 套' % len(theme.names()))

    def open_theme_folder(self):
        """打开主题文件夹：json 都在里面，照着自己改一套就行。"""
        folder = theme_folder()
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
            self.log('主题文件夹：%s' % folder)
        except Exception as exc:
            self.log('打不开主题文件夹（%s）：%s' % (folder, exc))

    def on_mute_toggled(self, enabled):
        """勾了「录制时静音系统提示音」：记进设置，正在录的话立刻生效。"""
        self._mute_system = bool(enabled)
        self.settings.setValue('mute_system', self._mute_system)
        self.log('录制时静音系统提示音：%s' % ('开' if enabled else '关'))
        if self.recording:
            self._hold_system_sounds()

    def _hold_system_sounds(self):
        """按住「系统提示音」那一路（录制开始时叫）。"""
        if not self._mute_system or not audiowatch.available():
            return
        token = audiowatch.hold_system_sounds()
        if token is None:
            self.log('系统提示音没按住 —— 这台机器上查不到那一路音频会话，录的时候要是还有「叮」，'
                     '就点「🔔 谁在响」听听是谁')
            return
        self._mute_token = token
        self.log('录制期间已按住系统提示音（收工自动放回去）')

    def _release_system_sounds(self):
        """把「系统提示音」放回去（收工 / 退出时叫）。"""
        token, self._mute_token = self._mute_token, None
        if token is None:
            return
        try:
            audiowatch.release_system_sounds(token)
        except Exception:                              # pragma: no cover
            pass

    def on_diag(self):
        """「谁在响」：听一段时间，谁出声就把名字记进日志。"""
        if self.diag is not None and self.diag.running:
            self.diag.stop()
            self.log('「谁在响」收工了')
            return
        if not audiowatch.available():
            self.log('这台机器上查不了音频会话（只有 Windows 有这套接口）')
            return
        here = audiowatch.foreground()
        self.log('「谁在响」开始了：接下来 %d 秒里谁出声就记谁。现在最前面的是 %s（%s）'
                 % (int(DIAG_SECONDS), here['name'] or '说不上来', here['class'] or '没有窗口'))
        self.diag = audiowatch.Watcher(log=self.message.emit,
                                       on_sound=self._on_diag_sound_raw,
                                       on_done=self._on_diag_done_raw,
                                       seconds=DIAG_SECONDS)
        self.diag_button.setText('⏹ 别听了')
        self.diag.start()

    def _on_diag_sound_raw(self, info):
        """（跑在听诊线程里）听见一声 —— 转给主线程写日志。"""
        self.diag_sound.emit('🔔 听到声音：%s%s'
                             % (info['name'],
                                '（系统提示音那一路 —— 勾上「录制时静音系统提示音」就能按住它）'
                                if info['system'] else ''))

    def _on_diag_done_raw(self, heard):
        """（跑在听诊线程里）听完了。"""
        names = []
        for name, _system, _at in heard:
            if name not in names:
                names.append(name)
        self.diag_done.emit('「谁在响」听完了：%s'
                            % ('、'.join(names) if names else '这期间一声都没有'))

    def _on_diag_sound(self, text):
        self.log(text)

    def _on_diag_done(self, text):
        self.log(text)
        if self.diag_button is not None:
            self.diag_button.setText('🔔 谁在响')

    def on_assoc_toggled(self, enabled):
        """关联 / 取消关联 .mproj 工程文件。"""
        exe = fileassoc.exe_of()
        try:
            if enabled:
                fileassoc.register(exe)
                self.log('已经把 .mproj 工程文件关联到 %s：以后双击工程文件就直接进编辑器'
                         % os.path.basename(exe))
                log_event('关联 .mproj -> %s' % exe)
            else:
                fileassoc.unregister()
                self.log('取消了 .mproj 的关联')
                log_event('取消 .mproj 关联')
        except Exception as exc:
            self.log('改文件关联失败：%s' % exc)
            if self.assoc_box is not None:      # 没改成，把勾选框退回去
                self.assoc_box.blockSignals(True)
                self.assoc_box.setChecked(not enabled)
                self.assoc_box.blockSignals(False)

    # ---------- 跟奏窗口 ----------

    def _speed(self):
        """界面上选的倍速。"""
        try:
            return float(self.speed.currentText())
        except ValueError:
            return 1.0

    def _ensure_follow(self):
        """跟奏窗口第一次要用的时候才创建。"""
        if not FOLLOW_MODE:
            return None
        if self.follow is None:
            try:
                self.follow = follow.FollowWindow(self.player)
                self.follow.set_anchor(follow.FollowWindow.resolve_position(
                    self.settings.value('follow_pos', follow.FollowWindow.POSITIONS[0])))
            except Exception as exc:
                self.follow = None
                self.log('跟奏窗口创建失败：%s' % exc)
                log_crash(traceback.format_exc())
        return self.follow

    def _refresh_follow(self, show=False):
        """
        把当前谱面按当前倍速交给跟奏窗口（倍速会影响音符的位置，必须重算）。

        show=True 就顺便把它显示出来；正在演奏就让音符接着走，否则显示「已就绪」。
        """
        if not self.follow_on or self.practice:
            return None
        window = self._ensure_follow()
        if window is None:
            return None
        window.set_score(self._shaped_events(), self._speed())
        if show:
            if self.player.running.is_set():
                window.begin()
            else:
                window.set_ready()
        return window

    def _sync_follow_state(self, state):
        """演奏状态变了，跟奏窗口上的字也跟着变。"""
        if self.follow is not None and self.follow_on:
            self.follow.set_state(state)

    def on_follow_toggled(self, enabled):
        """勾 / 取消界面上的「跟奏模式」。"""
        self.follow_on = bool(enabled)
        self.settings.setValue('follow', self.follow_on)
        self._sync_follow_ui(self.follow_on)
        if self.follow_on:
            self._refresh_follow(show=True)
        else:
            if self.practice:
                if self.follow is not None:
                    self.follow.cancel()       # 练习中关掉跟奏 = 结束练习
                self._on_practice_finish('stop')   # 兜底：万一没回调也不会卡在练习状态
            if self.follow is not None:
                self.follow.shutdown()
        self.log('跟奏模式：%s' % ('开（Ctrl+F2 也能开关）' if self.follow_on else '关'))
        log_event('跟奏模式：%s' % ('开' if self.follow_on else '关'))

    def on_follow_pos(self, text):
        """换跟奏窗口贴在屏幕的哪个位置。"""
        self.settings.setValue('follow_pos', text)
        if self.follow is not None:
            self.follow.set_anchor(text)

    def on_follow_pace(self, text):
        """换跟奏的节奏（练习 / 原速）。"""
        self.settings.setValue('follow_pace', text)
        if self.follow_on:
            self.log('跟奏节奏：%s' % text)

    def toggle_follow(self):
        """Ctrl+F2 / 托盘菜单：开关跟奏模式。"""
        if not FOLLOW_MODE:
            self.log('这个版本没带跟奏窗口，用 FollowPlay 文件夹里的 AutoPlayFollow.exe')
            return
        if self.follow_box is not None:
            self.follow_box.setChecked(not self.follow_box.isChecked())
        else:
            self.on_follow_toggled(not self.follow_on)

    def _sync_follow_ui(self, enabled):
        """让勾选框和托盘菜单里的勾号保持一致。"""
        if self.follow_box is not None:
            self.follow_box.blockSignals(True)
            self.follow_box.setChecked(bool(enabled))
            self.follow_box.blockSignals(False)
        if self.follow_action is not None:
            self.follow_action.blockSignals(True)
            self.follow_action.setChecked(bool(enabled))
            self.follow_action.blockSignals(False)

    # ---------- 转换 ----------

    # ---------- 联网曲库 ----------

    def open_online_library(self):
        """
        联网曲库：歌单在 GitHub 上，程序拉一个索引就知道有哪些曲子。

        这是个「桌面上的活儿」（要联网、要挑歌），所以规规矩矩开一个窗口；
        在游戏里点它就只记一行日志 —— 免得弹窗把游戏顶出全屏（跟选文件一个道理）。
        """
        if self.in_game():
            self.log('联网曲库要开窗口，回桌面再点（在游戏里弹窗会把你顶出全屏）')
            return
        url = library.source_url()
        dialog = QDialog(self)
        dialog.setWindowTitle('联网曲库')
        dialog.setMinimumSize(580, 430)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        source = QLabel('曲库地址：%s' % (url or '（还没设置）'))
        source.setObjectName('hint')
        source.setWordWrap(True)
        source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(source)
        hint = QLabel()
        hint.setWordWrap(True)
        layout.addWidget(hint)
        listing = QListWidget()
        layout.addWidget(listing, 1)
        status = QLabel('')
        status.setObjectName('hint')
        status.setWordWrap(True)
        layout.addWidget(status)
        row = QHBoxLayout()
        refresh = QPushButton('刷新')
        take = QPushButton('下载并载入')
        take.setObjectName('primary')
        upload = QPushButton('上传 / 整理曲库…')
        upload.setToolTip('把本机的 midi 传到这个公开曲库，或者按仓库里现有的文件\n'
                          '重新生成一份 library.json（索引）。传上去就代表同意分享。')
        close = QPushButton('关闭')
        for button in (refresh, take, close):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(upload)
        layout.addLayout(row)

        def fill(songs, message):
            listing.clear()
            for song in songs:
                bits = [str(song.get('title') or ''), str(song.get('artist') or '')]
                size = int(song.get('size') or 0)
                if size:
                    bits.append('%.0f KB' % (size / 1024.0))
                if library.is_cached(song):
                    bits.append('已下载')
                item = QListWidgetItem('　'.join(bit for bit in bits if bit))
                item.setData(Qt.ItemDataRole.UserRole, song)
                listing.addItem(item)
            hint.setText(message)
            take.setEnabled(bool(listing.count()))

        def reload(_checked=False):
            status.setText('正在拉曲库…')
            QApplication.processEvents()
            songs, why = library.fetch_index()
            if songs:
                fill(songs, '共 %d 首，本地已经有 %d 首。'
                     % (len(songs), len(library.installed_songs())))
                status.setText('')
            else:
                cached, _when = library.cached_index()
                if cached:
                    fill(cached, '这次没连上（%s）。下面是上次拉到的歌单，下过的还能直接用。' % why)
                else:
                    fill([], '没能拉到歌单：%s' % why)
                    if library.repo_of()[0]:
                        status.setText('这个仓库里没有 midi，或者这台机器连不上 GitHub。\n'
                                       '（仓库里只放 midi 就行，不用额外准备索引文件；\n'
                                       '地址想换一个就写进 %s）'
                                       % library.source_file())
                    else:
                        status.setText('设置方法：把曲库地址写进 %s（一行地址就行），\n'
                                       '或者填在 library_source.py 的 INDEX_URL 里再重新打包。'
                                       % library.source_file())

        def download_current(_checked=False):
            item = listing.currentItem()
            if item is None:
                status.setText('先在上面挑一首')
                return
            song = item.data(Qt.ItemDataRole.UserRole) or {}
            status.setText('正在下载 %s…' % song.get('title', ''))
            QApplication.processEvents()
            path, why = library.download(song)
            if not path:
                status.setText('没下下来：%s' % why)
                return
            self.log('联网曲库下好了：%s' % path)
            status.setText('下好了：%s' % os.path.basename(path))
            self.last_dir = os.path.dirname(path)
            if self.load(path):
                dialog.accept()

        refresh.clicked.connect(reload)
        take.clicked.connect(download_current)
        listing.itemDoubleClicked.connect(download_current)
        upload.clicked.connect(lambda: self.open_upload_dialog(url))
        close.clicked.connect(dialog.reject)
        reload()
        dialog.exec()

    def open_upload_dialog(self, url=''):
        """
        往曲库传曲子 / 重排索引。

        曲库是大家一起用的公开仓库，所以开头先把这件事说清楚：传上去就等于
        分享出去了，所有人都能下。按不按「上传」还是你说了算。

        GitHub 令牌是**内置在程序里**的（见 library.py / github_token_local.py），
        界面上没有这一项 —— 打开就能传，不用填任何东西。
        """
        url = str(url or library.source_url())
        owner, repo, branch, why = library.repo_of(url)
        dialog = QDialog(self)
        dialog.setWindowTitle('上传到曲库')
        dialog.setMinimumSize(560, 470)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        warn = QLabel('这个曲库是公开的，大家一起用 🙂\n'
                      '传上去的曲子所有人都能看见、能下载 —— 点「上传」就等于同意把它分享出去。\n'
                      '只想留着自己听的，就别往这儿传。')
        warn.setObjectName('notice')
        warn.setWordWrap(True)
        layout.addWidget(warn)

        where = QLabel('仓库：%s' % ('%s/%s @%s' % (owner, repo, branch) if owner
                                     else (why or '认不出仓库')))
        where.setObjectName('hint')
        where.setWordWrap(True)
        layout.addWidget(where)

        # 走中转还是走内置令牌：这里说清楚，省得用户以为「要填令牌才能传」
        via = QLabel(relay.describe() if relay.has_url() else
                     '上传通道：程序里内置的令牌')
        via.setObjectName('hint')
        via.setWordWrap(True)
        layout.addWidget(via)

        pick_row = QHBoxLayout()
        file_label = QLabel('（还没挑文件 —— 只有「上传」才用得到）')
        file_label.setObjectName('hint')
        file_label.setWordWrap(True)
        pick_row.addWidget(file_label, 1)
        pick = QPushButton('选择 midi…')
        pick_row.addWidget(pick)
        layout.addLayout(pick_row)

        meta = QHBoxLayout()
        meta.addWidget(QLabel('标题'))
        title_edit = QLineEdit()
        title_edit.setPlaceholderText('不填就用文件名')
        meta.addWidget(title_edit, 1)
        meta.addWidget(QLabel('艺术家'))
        artist_edit = QLineEdit()
        meta.addWidget(artist_edit, 1)
        layout.addLayout(meta)

        status = QLabel('')
        status.setObjectName('hint')
        status.setWordWrap(True)
        status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(status, 1)

        row = QHBoxLayout()
        go = QPushButton('上传并更新曲库')
        go.setObjectName('primary')
        reindex = QPushButton('按仓库现有文件重排索引')
        reindex.setToolTip('不传新曲子，只把 library.json 按仓库里现有的 midi 重新生成一遍')
        close = QPushButton('关闭')
        row.addWidget(go)
        row.addWidget(reindex)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

        def upload_one(path, title, artist):
            """
            上传一首曲子：配了中转就走中转（安装包里没令牌），
            没配就走原来那条（内置令牌 / 本机令牌文件）。
            """
            if relay.has_url():
                return relay.upload(path, title, artist, owner, repo, branch)
            return library.upload_song(path, title, artist, url=url)

        self._upload_path = ''

        def choose(_checked=False):
            path, _f = QFileDialog.getOpenFileName(
                dialog, '挑一首要传的 midi', self.last_dir or '',
                'MIDI 文件 (*.mid *.midi)')
            if not path:
                return
            self._upload_path = path
            self.last_dir = os.path.dirname(path)
            file_label.setText(os.path.basename(path))
            if not title_edit.text().strip():
                title_edit.setText(os.path.splitext(os.path.basename(path))[0])

        def busy(text):
            status.setText(text)
            QApplication.processEvents()

        def do_upload(_checked=False):
            if not owner:
                status.setText(why or '先设置曲库地址')
                return
            path = self._upload_path
            if not path or not os.path.isfile(path):
                status.setText('先选一个 midi 文件')
                return
            busy('正在上传 %s…' % os.path.basename(path))
            told, bad = upload_one(path, title_edit.text().strip(),
                                   artist_edit.text().strip())
            if bad:
                status.setText('没传成：%s' % bad)
                self.log('上传到曲库失败：%s' % bad)
                return
            status.setText('%s\n（索引可能要过一两分钟才在镜像上生效。）' % told)
            self.log('上传到曲库：%s' % told)

        def do_reindex(_checked=False):
            if not owner:
                status.setText(why or '先设置曲库地址')
                return
            busy('正在重排索引…')
            if relay.has_url():
                told, bad = relay.reindex(owner, repo, branch)
            else:
                told, bad = library.refresh_index(owner, repo, branch)
            if bad:
                status.setText('没改成：%s' % bad)
                self.log('重排曲库索引失败：%s' % bad)
                return
            status.setText(told)
            self.log('曲库：%s' % told)

        pick.clicked.connect(choose)
        go.clicked.connect(do_upload)
        reindex.clicked.connect(do_reindex)
        close.clicked.connect(dialog.reject)
        dialog.exec()

    # ---------- 内置曲库 / 简谱编辑器 ----------

    def open_library(self):
        """打开内置曲库。不管在不在覆盖模式都用浮层列表 —— 少开一个窗口总是好的。"""
        if not os.path.isdir(SONG_DIR):
            self.log('没找到内置曲库（%s）' % SONG_DIR)
            return
        self.open_picker(SONG_DIR)

    def open_editor(self):
        """切到编辑器那一页，顺手把当前这首塞进去一起看。"""
        if not edition.has_editor():
            self.log('这一版没带简谱编辑器（精简版不含编辑器）；要改谱请用完全版')
            return
        self.tabs.setCurrentWidget(self.editor_page)
        if self.editor is not None and self.midi_path:
            self.editor.open_path(self.midi_path, ask=False)

    def _on_tab_changed(self, index):
        if self.tabs.widget(index) is self.editor_page:
            self._make_editor()
        self._sync_overlay_opacity()
        self._sync_overlay_topmost()
        self._sync_editor_focus()

    def _sync_editor_focus(self):
        """
        编辑器那一页里的数字框要打字，所以停在那一页时把窗口的「不接受焦点」临时撤掉
        （只有不在游戏里才撤）。回到「演奏」页立刻补回去。

        游戏里一律不撤：宁可编辑器打不了字，也不能让浮层有机会把游戏顶回桌面。
        """
        if not self.overlay_active:
            return
        editing = self._editor_page_active()
        self._sync_overlay_opacity()
        self._sync_overlay_topmost()
        if editing and self.in_game():
            set_no_activate(self, True)
            self.log('游戏里编辑器收不到键盘（浮层不抢焦点）；要打字改数值，先回桌面再用')
            return
        set_no_activate(self, not editing)

    def _make_editor(self):
        """第一次点开这一页才把编辑器建出来（省得不用的人白等启动）。"""
        if editor is None or not edition.has_editor():
            return
        if self.editor is not None or self._building_editor:
            return
        self._building_editor = True
        try:
            self.editor = editor.EditorWindow(self.editor_page, embedded=True)
            self.editor.on_export = self._on_editor_export
            self.editor.log_view.setFixedHeight(74)   # 主窗口没编辑器窗口那么高
            self.editor.log_view.setVisible(self._log_visible)   # 控制台关着就只留画布
            self.editor_layout.addWidget(self.editor)
            log_event('打开简谱编辑器')
            if self.midi_path:
                self.editor.open_path(self.midi_path, ask=False)
        finally:
            self._building_editor = False

    def _on_editor_export(self, path):
        """编辑器导出的 midi 直接载回来 —— 改完不用再手动选一遍文件。"""
        self.log('编辑器导出了 %s，直接载入' % os.path.basename(path))
        if self.load(path):
            self.tabs.setCurrentWidget(self.play_page)

    def open_initial(self, path):
        """
        启动时带进来的那个文件。

        .mproj 是简谱工程（多半是在资源管理器里双击进来的，见 fileassoc.py），
        直接开编辑器那一页；别的（midi / 音频）照旧走载入流程。
        """
        suffix = os.path.splitext(path)[1].lower()
        if editor is not None and suffix == editor.PROJECT_SUFFIX:
            self.open_project(path)
            return
        self.load(path)

    def open_project(self, path):
        """打开一个简谱工程文件：切到编辑器那一页，把工程铺上。"""
        if not edition.has_editor() or editor is None:
            self.log('这是个简谱工程文件（%s），但精简版不带编辑器；要改谱请用完全版'
                     % os.path.basename(path))
            return False
        self._make_editor()
        if self.editor is None:
            return False
        log_event('打开工程文件：%s' % os.path.basename(path))
        # 先打开、再切页：工程读不动（文件坏了 / 被删了）就留在演奏页，
        # 免得用户被扔进一个空编辑器里对着日志发呆
        if not self.editor.open_project(path):
            return False
        self.tabs.setCurrentWidget(self.editor_page)
        return True

    def _restore_autosave(self, skip=''):
        """
        上次没收好尾（崩溃 / 被强杀 / 断电）留下的自动保存：摆回编辑器。

        正常退出时 quit_app 会把自动保存清干净，所以这儿有东西 = 上次是意外结束的。
        最新的一份放进编辑器标签页，其余的照「双击工程文件」那样各开一个窗口 ——
        上次开着几份，这次就给你摆回来几份。返回恢复了几份。
        """
        if editor is None or not edition.has_editor():
            return 0
        try:
            entries = autosave.pending()
        except Exception as exc:
            self.log('自动保存读不出来：%s' % exc)
            return 0
        if not entries:
            return 0
        skip = os.path.normcase(os.path.abspath(skip)) if skip else ''
        restored, first, names = 0, True, []
        for item in entries:
            path = item.get('path') or ''
            if skip and os.path.normcase(os.path.abspath(path)) == skip:
                continue                       # 这个就是启动时带进来的那个文件，别开两份
            if first:
                first = False
                if self._open_autosave_in_editor(path):
                    restored += 1
                    names.append(item.get('name') or os.path.basename(path))
                continue
            if self.open_project_window(path, unsaved=True):
                restored += 1
                names.append(item.get('name') or os.path.basename(path))
        if not restored:
            return 0
        self.log('上次没有正常退出，已经自动恢复 %d 份没保存的谱面：%s'
                 % (restored, '、'.join(names[:3]) + ('…' if len(names) > 3 else '')))
        self.log('这些是自动保存的底子（还没存成工程）。改完按「保存工程」或者「导出 MIDI」，'
                 '正常退出程序时它自己会清掉。')
        log_event('恢复自动保存：%d 份' % restored)
        return restored

    def _open_autosave_in_editor(self, path):
        """把自动保存的那一份摆到编辑器标签页上（不另开窗口）。"""
        try:
            self._make_editor()
            if self.editor is None:
                return False
            if not self.editor.open_project(path, unsaved=True):
                return False
        except Exception as exc:
            self.log('自动保存打开失败：%s' % exc)
            log_crash(traceback.format_exc())
            return False
        self.tabs.setCurrentWidget(self.editor_page)
        return True

    def open_project_window(self, path, unsaved=False):
        """
        另开一个编辑器窗口打开这个工程。

        给「程序已经在跑，又有人双击了一个 .mproj」用（见 single.py）：不动当前窗口，
        用户双击哪个工程就给他一个属于那个工程的窗口 —— 关掉它也不影响主窗口。
        """
        name = os.path.basename(path)
        if not edition.has_editor() or editor is None:
            self.log('收到一个工程文件（%s），但精简版不带编辑器；要改谱请用完全版' % name)
            return False
        try:
            window = editor.EditorWindow()
        except Exception as exc:
            self.log('开编辑器窗口失败：%s' % exc)
            log_crash(traceback.format_exc())
            return False
        window.setWindowIcon(make_icon())
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)   # 关了就别留着
        window.destroyed.connect(lambda _obj=None, w=window: self._forget_editor_window(w))
        if not window.open_project(path, unsaved=unsaved):
            window.close()                 # 读不动就别留个空窗口
            return False
        self.editor_windows.append(window)
        window.show()
        window.raise_()
        window.activateWindow()
        self.log('新开了一个编辑器窗口：%s' % name)
        log_event('打开工程窗口：%s' % path)
        return True

    def _forget_editor_window(self, window):
        """那个窗口关了（WA_DeleteOnClose 已经把它删了），从名单里划掉。"""
        try:
            self.editor_windows.remove(window)
        except ValueError:
            pass

    def take_handoff(self, path):
        """接过另一个进程转来的「打开这个文件」（双击工程文件起的那个进程，见 single.py）。"""
        log_event('收到外部打开请求：%s' % (path or '(显示窗口)'))
        if not path:
            self.show_window()             # 只喊了一声「显示窗口」
            return
        if editor is not None and path.lower().endswith(editor.PROJECT_SUFFIX):
            self.open_project_window(path)
        else:
            self.load(path)

    def nativeEvent(self, eventType, message):
        """
        Windows 消息兜底：接住另一个进程用 WM_COPYDATA 转过来的「打开这个文件」。

        Qt 会把原生的 Windows 消息递到这儿。只认我们自己的 magic（single.py），
        别的（包括系统文件框、拖放那些）一律原样交回去。
        """
        try:
            if bytes(eventType) == b'windows_generic_MSG':
                event = single.message_of(int(message))
                if event is not None and event.message == single.WM_COPYDATA:
                    path = single.read_payload(event.lParam)
                    if path:
                        self.take_handoff(path)
                        # 第二个元素才是这条消息的返回值：对面的 SendMessageTimeout
                        # 靠它判断「有人处理了」。返回 0 等于说「没人管」，对面就会
                        # 再起一个新进程 —— 那正是我们想避免的。
                        return True, 1
        except Exception as exc:
            log_event('处理外部消息出错：%s' % exc)
        return super().nativeEvent(eventType, message)

    def load_default_song(self):
        """没指定文件时载入内置曲库的第一首（安装包装出来就是《鸟之诗》）。"""
        songs = library_songs()
        if not songs:
            self.log('没指定 midi，也没找到内置曲库（%s）' % SONG_DIR)
            return False
        self.log('没指定文件，载入内置曲库默认曲：%s' % os.path.basename(songs[0]))
        return self.load(songs[0])

    def choose_file(self):
        """
        选文件。

        在桌面上照旧用系统文件框（能打字、能粘路径，方便）；只有被 Ctrl+F1 从游戏里
        唤起来的时候才换成浮层内的文件列表 —— 那种时候多开一个窗口就可能把游戏顶回
        桌面，干脆一个都不开。
        """
        if not self.in_game():
            path, _ = QFileDialog.getOpenFileName(self, '选择 MIDI 文件',
                                                  self.last_dir, MIDI_FILTER)
            if path:
                self.load(path)
            return
        self.open_picker()

    def choose_audio(self):
        """
        选音频（mp3 / wav / flac…）转成单音 MIDI 再读。

        跟选 MIDI 一个规矩：桌面上用系统文件框，游戏里（Ctrl+F1 唤起来的浮层）
        用浮层里的文件列表 —— 那个列表本来就同时列着 MIDI 和音频。
        """
        if audio2midi is None or not edition.has_audio():
            self.log('这一版没带「音频转 MIDI」（精简版不含转谱）；先用别的工具转成 midi 再读进来')
            return
        if self._playing():
            self.log('正在演奏中，先按 F8 停止')
            return
        if not self.in_game():
            path, _ = QFileDialog.getOpenFileName(self, '选择音频（转成单音 MIDI）',
                                                  self.last_dir, AUDIO_FILTER)
            if path:
                self.load(path)
            return
        self.open_picker()

    def open_picker(self, folder=None):
        """
        在浮层里自己列文件。

        跟跟奏面板一个路子：文件列表就是主界面里的一块普通控件，翻目录靠按钮和双击，
        全程不弹任何窗口 —— 会惊动全屏游戏的往往就是那些额外弹出来的窗口（系统文件框、
        它自带的下拉列表都算），一个都不开，游戏那边自然无感。
        """
        if self._choosing is not None:
            return
        panel = QWidget(self.pick_panel)
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        for text, slot in (('上一级', self._pick_up), ('桌面', self._pick_desktop),
                           ('刷新', self._pick_refresh)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        self.pick_path = QLabel('')
        self.pick_path.setObjectName('fieldLabel')
        bar.addWidget(self.pick_path, 1)
        box.addLayout(bar)

        self.pick_list = QListWidget()
        self.pick_list.itemDoubleClicked.connect(self._pick_open)
        self.pick_list.currentItemChanged.connect(self._sync_pick_open)
        box.addWidget(self.pick_list, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.pick_hint = QLabel('')
        self.pick_hint.setObjectName('hint')
        foot.addWidget(self.pick_hint, 1)
        self.pick_online = QPushButton('联网曲库')
        self.pick_online.setToolTip('共享曲库：曲子放在 GitHub 仓库里，程序读一个索引就知道有哪些，\n'
                                    '点「下载并载入」直接下到本地缓存里用；断网也不影响本地曲库。\n'
                                    '（要联网挑歌，所以在游戏里点它只会记一行日志，回桌面再点。）')
        self.pick_online.clicked.connect(self.open_online_library)
        foot.addWidget(self.pick_online)
        self.pick_ok = QPushButton('打开')
        self.pick_ok.setObjectName('primary')
        self.pick_ok.setEnabled(False)
        self.pick_ok.clicked.connect(self._pick_open_selected)
        cancel = QPushButton('取消')
        cancel.clicked.connect(self._close_picker)
        foot.addWidget(self.pick_ok)
        foot.addWidget(cancel)
        box.addLayout(foot)

        self._choosing = panel            # 留个引用，关闭时按它判断还在不在选文件
        self.pick_layout.addWidget(panel)
        self.log_view.hide()
        self.pick_panel.show()
        self._grow_for_picker()
        self._pick_enter(folder or self.last_dir)
        self.log('选文件就在浮层里：鼠标点着找、双击打开（浮层不抢键盘，所以打不了字）')

    def _pick_enter(self, folder):
        """进到某个文件夹，先列子文件夹，再列 MIDI 文件。"""
        if self._choosing is None:
            return
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):
            folder = os.path.dirname(folder)
        while not os.path.isdir(folder):        # 文件夹被删了 / 挪走了就一路往上找
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
        error = ''
        try:
            names = sorted(os.listdir(folder), key=lambda name: name.lower())
        except OSError as exc:
            names, error = [], '这个文件夹打不开：%s' % exc
        self._pick_dir = folder
        self.pick_path.setText(folder)
        self.pick_path.setToolTip(folder)
        self.pick_list.clear()
        folders, files = [], []
        for name in names:
            if name.startswith('.'):
                continue
            full = os.path.join(folder, name)
            if os.path.isdir(full):
                folders.append(name)
            elif name.lower().endswith(MIDI_SUFFIX + AUDIO_SUFFIX):
                files.append(name)
        for name in folders:
            item = QListWidgetItem(name + '/')     # 带斜杠的一眼就是文件夹
            item.setData(Qt.ItemDataRole.UserRole, os.path.join(folder, name))
            item.setData(Qt.ItemDataRole.UserRole + 1, True)
            item.setForeground(QBrush(QColor(theme.c('#8b93a7'))))
            self.pick_list.addItem(item)
        for name in files:
            full = os.path.join(folder, name)
            audio_file = name.lower().endswith(AUDIO_SUFFIX)
            item = QListWidgetItem(name + ('（音频）' if audio_file else ''))
            item.setData(Qt.ItemDataRole.UserRole, full)
            if audio_file:
                item.setToolTip('音频文件：双击会先转成单音 MIDI，再读进来')
                item.setForeground(QBrush(QColor(theme.c('#7fb0ff'))))
            self.pick_list.addItem(item)
        if error:
            self.pick_hint.setText(error)
        elif not folders and not files:
            self.pick_hint.setText('这个文件夹里既没有 MIDI / 音频文件，也没有子文件夹')
        else:
            self.pick_hint.setText('双击文件夹进去；MIDI 直接读，蓝色的音频会先转成单音 MIDI')

    def _pick_open(self, item):
        """双击：文件夹就进去，文件就开始读。"""
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if item.data(Qt.ItemDataRole.UserRole + 1):
            self._pick_enter(path)
        else:
            self._on_file_chosen(path)

    def _pick_open_selected(self):
        """「打开」按钮：读当前选中的那个文件。"""
        if self.pick_list is not None:
            self._pick_open(self.pick_list.currentItem())

    def _sync_pick_open(self, *_):
        """没点中文件的时候「打开」是灰的。"""
        if self._choosing is None or self.pick_ok is None:
            return
        item = self.pick_list.currentItem()
        is_file = item is not None and not item.data(Qt.ItemDataRole.UserRole + 1)
        self.pick_ok.setEnabled(bool(is_file))

    def _pick_up(self):
        self._pick_enter(os.path.dirname(self._pick_dir))

    def _pick_desktop(self):
        self._pick_enter(os.path.join(os.path.expanduser('~'), 'Desktop'))

    def _pick_refresh(self):
        self._pick_enter(self._pick_dir)

    def _grow_for_picker(self):
        """文件列表嵌在主界面里，太矮了不好挑文件，先把它撑高一点。"""
        try:
            self._size_before_pick = self.size()
            screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
            height = max(self.height(), 840)
            if screen is not None:
                height = min(height, screen.availableGeometry().height() - 60)
            self.resize(self.width(), height)
        except Exception:
            pass

    def _close_picker(self):
        """收起文件列表，把界面还原。"""
        panel, self._choosing = self._choosing, None
        if panel is not None:
            self.pick_layout.removeWidget(panel)
            panel.setParent(None)
            panel.deleteLater()
        self.pick_list = None
        self.pick_path = None
        self.pick_hint = None
        self.pick_ok = None
        self.pick_online = None
        self.pick_panel.setVisible(False)
        self.log_view.show()
        size = getattr(self, '_size_before_pick', None)
        if size is not None:
            self._size_before_pick = None
            try:
                self.resize(size)
            except Exception:
                pass

    def _on_file_chosen(self, path):
        self._close_picker()
        if path:
            self.load(path)

    @staticmethod
    def _track_text(track):
        """音轨下拉框里的一行字。"""
        if track.note_count:
            return '[%d] %s｜%d 个音，%s ~ %s' % (
                track.index, track.name or '(无名)', track.note_count,
                midi_analyze.pitch_name(track.pitch_min), midi_analyze.pitch_name(track.pitch_max))
        if track.all_notes:
            return '[%d] %s｜%d 个音（打击乐通道）' % (
                track.index, track.name or '(无名)', track.all_notes)
        return '[%d] %s｜没有音符' % (track.index, track.name or '(无名)')

    def _fill_tracks(self, analysis):
        """把音轨填进下拉框，并把当前用的那条选中。"""
        self.track_box.blockSignals(True)
        self.track_box.clear()
        for track in analysis.tracks:
            row = self.track_box.addItem(self._track_text(track), track.index)
            if not track.all_notes:                      # 没音符的轨不给选
                self.track_box.setItemEnabled(row, False)
        row = self.track_box.findData(analysis.track_index)
        if row >= 0:
            self.track_box.setCurrentIndex(row)
        self.track_box.blockSignals(False)
        self.btn_track.setEnabled(len(analysis.tracks) > 1 and not self._playing())

    def use_selected_track(self):
        """按下拉框里选的音轨重新转换一遍。"""
        index = self.track_box.currentData()
        if self.midi_path is None or index is None:
            return
        if self._playing():
            self.log('正在演奏，先按 F8 停止再换音轨')
            return
        if int(index) == self.track_index:
            self.log('现在用的就是音轨 %d' % int(index))
            return
        self.load(self.midi_path, track_index=int(index))

    def load(self, path, track_index=None):
        """读取 midi -> 分析 -> 写谱面 -> 准备好演奏。track_index 指定音轨时手动转换。"""
        if self.recording:
            # 录制中把文件换掉，录完就会糊到别的曲子上，干脆拦住
            self.log('正在录制，先按 %s 收工再换文件' % self._rec_key())
            return False
        path = os.path.abspath(path)
        self.stop_preview()                  # 换文件了，上一首的试听先停掉
        if is_audio(path):
            return self.convert_audio(path)      # 音频先转成单音 MIDI，转完再回来读
        self.last_dir = os.path.dirname(path)
        self.log('读取 %s' % path)
        try:
            analysis = midi_analyze.analyze(path, track_index=track_index)
            pairs = midi_analyze.build_score(analysis.notes, analysis.tonic)
            score_path = write_score(path, analysis, pairs)
            events = jianpu.parse_score(jianpu.read_score(score_path))
        except midi_analyze.MidiError as exc:
            self.log('转换失败：%s' % exc)
            self.set_status('转换失败', 'error')
            return False
        except Exception as exc:
            self.log('出错了：%s' % exc)
            log_crash(traceback.format_exc())
            self.set_status('出错了', 'error')
            return False

        self.midi_path = path
        self.track_index = analysis.track_index
        self.analysis = analysis
        self.score_path = score_path
        self.score_events = events
        self.btn_preview.setEnabled(True)        # 有谱面了，可以试听
        self.log(midi_analyze.format_report(analysis, score_path))
        self._set_file_label(path)
        track_text = '[%d] %s（%s）' % (
            analysis.track_index, analysis.track_name or '(无名)', analysis.track_reason)
        if analysis.manual and analysis.auto_index >= 0 and analysis.auto_index != analysis.track_index:
            track_text += '　自动推荐 [%d]' % analysis.auto_index
        self.track_label.setText(track_text)
        self._fill_tracks(analysis)
        tonic = analysis.tonic_result
        self.tonic_label.setText('TONIC %d (%s)　升半音 %d 个 · 升降调 %d 个%s' % (
            tonic.tonic, midi_analyze.pitch_name(tonic.tonic), tonic.sharp, tonic.mod,
            ' · 折回八度 %d 个' % tonic.folded if tonic.folded else ''))
        self.score_label.setText('%s　共 %d 个音' % (os.path.basename(score_path), len(events)))
        self.counter.setText('0 / %d' % len(events))
        self.bar.setValue(0)
        self.set_status('已就绪', 'ready')
        self.setWindowTitle('%s - %s' % (APP_TITLE, os.path.basename(path)))
        if self.overlay_on:
            self.overlay.set_ready(len(events))     # 没演奏也要显示「已就绪」
        self._refresh_follow(show=self.follow_on)
        log_event('载入 %s：音轨 %d（%s），tonic %d，%d 个音，谱面 %s' % (
            os.path.basename(path), analysis.track_index, analysis.track_reason,
            analysis.tonic, len(events), os.path.basename(score_path)))
        return True

    # ---------- 音频转 MIDI ----------

    def convert_audio(self, path):
        """
        音频 -> 单音 MIDI（后台线程，转完自动接着读）。

        一首歌要转几十秒，放主线程界面会卡死，所以丢给线程；转完用 audio_done
        信号回到主线程再 load()。
        """
        if self._converting:
            self.log('上一段音频还在转，稍等一下')
            return False
        if audio2midi is None or not edition.has_audio():
            self.log('这一版没带「音频转 MIDI」（精简版不含转谱）')
            return False
        if self._playing():
            self.log('正在演奏中，先按 F8 停止')
            return False
        self._converting = True
        self.btn_audio.setEnabled(False)
        self.set_status('转换音频中', 'play')

        def work():
            try:
                # 探测后端要真去 import（basic-pitch 那一串装齐了得一两秒），
                # 挪到后台线程里算，别把界面卡住
                usable = [name for name, ok in audio2midi.describe_backends().items() if ok]
                self.message.emit('把音频转成 MIDI：%s（能用的后端：%s）'
                                  % (os.path.basename(path), '、'.join(usable)))
                out = audio2midi.convert(
                    path, backend='auto',
                    progress=lambda text: self.message.emit(text))
            except Exception as exc:
                self.audio_done.emit('', str(exc))
                return
            self.audio_done.emit(out, '')

        threading.Thread(target=work, daemon=True).start()
        return True

    def _on_audio_done(self, midi_path, error):
        """音频转换收尾：成功就接着读那个 MIDI，失败就把话说清楚。"""
        self._converting = False
        if self.btn_audio is not None:
            self.btn_audio.setEnabled(True)
        if error or not midi_path:
            self.log('音频转 MIDI 失败：%s' % (error or '没生成文件'))
            if getattr(sys, 'frozen', False):
                self.log('小贴士：打包版自带 basic-pitch，正常不该走到这儿 —— 把日志发出来看看。'
                         '实在不行就先用能读的 midi，或者退回单声部音频（自带 YIN 只认单声部）。')
            else:
                self.log('小贴士：装了 basic-pitch 才能转有伴奏 / 编曲的歌（自带的 YIN 只认单声部）。'
                         '安装见 mp3midi/README.md，国内用清华镜像：'
                         'pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-deps basic-pitch')
            self.set_status('转换失败', 'error')
            return
        if not self.isVisible():
            self.show_window(in_game=self.in_game())
        self.log('音频转好了：%s' % os.path.basename(midi_path))
        self.load(midi_path)
        self.log('按「♪ 试听」听听主旋律抓对没有；不对就换个后端，或者先把人声 / 主旋律分离出来再转')

    # ---------- 试听 ----------

    def toggle_preview(self):
        """
        「♪ 试听」：把当前谱面合成一小段音频放出来，听听旋律抓对没有。

        合成要算几百毫秒，所以丢给后台线程；这段时间状态胶囊上写着「试听准备中」，
        放起来之后变成「试听 0:12 / 1:35」。再按一次（或者开始演奏 / 换文件 / 退出）
        就停 —— 演奏和试听不会同时响。
        """
        if self._previewing:
            self.stop_preview()
            return
        if self._playing():
            self.log('正在演奏中，先停止再试听')
            return
        if not self.score_events or self.analysis is None:
            self.log('先选一个 midi 文件')
            return
        if not self.previewer.available():
            self.log('试听用的是 Windows 自带的 winsound，这台机器上没有')
            return
        events = list(self.score_events)
        self._previewing = True
        self._preview_path = None
        self._preview_base = 0.0
        self._preview_total = preview.total_seconds(events)
        self._set_preview_button(True)
        self.preview_slider.setEnabled(False)      # 合成完才让拨
        self._set_preview_position(0.0)
        self.set_status('试听准备中', 'play')
        self.log('试听：%s（%d 个音，约 %s）'
                 % (os.path.basename(self.score_path or ''), len(events),
                    preview.format_time(self._preview_total)))
        log_event('试听：%s' % os.path.basename(self.score_path or ''))
        threading.Thread(target=self._render_preview,
                         args=(events, self.analysis.tonic), daemon=True).start()

    def _render_preview(self, events, tonic):
        """后台线程：合成整首，合成完用 preview_ready 信号回到主线程再播。"""
        try:
            self.previewer.render(events, tonic)
        except Exception as exc:
            self.preview_ready.emit(str(exc))
            return
        self.preview_ready.emit('')

    def _on_preview_ready(self, error):
        """合成完了：从头放出来。合成期间已经被停掉的话就什么都不做。"""
        if not self._previewing:
            return
        if error:
            self.log('试听合成失败：%s' % error)
            self.stop_preview()
            return
        self._preview_total = self.previewer.total
        self.preview_slider.setEnabled(True)
        if not self._start_preview_at(0.0):
            self.log('试听播放不了（音频可能是空的）')
            self.stop_preview()
            return
        self._preview_tick()

    def _start_preview_at(self, seconds):
        """
        从 seconds 秒开始放，放不了返回 False。

        winsound 不能跳转，所以这里是让 preview 把后面那截另存出来再放，界面只需要
        记住「这一遍是从第几秒开始的」——进度条上的时间就是它加上已经过去的时间。
        """
        started = self.previewer.play_from(seconds)
        if started is None:
            return False
        self._preview_path = self.previewer.path
        self._preview_base = started
        self._preview_t0 = time.time()
        self._set_preview_position(started)
        self.preview_timer.start()
        return True

    def _preview_value_to_seconds(self, value):
        """进度条位置（0~SEEK_RANGE）-> 秒。"""
        return self._preview_total * (float(value) / SEEK_RANGE)

    def _show_preview_value(self, value):
        """按进度条当前位置刷那行「0:12 / 1:35」（手指还按着的时候也照刷）。"""
        self.preview_time.setText(
            '%s / %s' % (preview.format_time(self._preview_value_to_seconds(value)),
                         preview.format_time(self._preview_total)))

    def _set_preview_position(self, seconds):
        """把进度条挪到 seconds 秒（手指还在把手上就别抢，交给它自己）。"""
        total = max(self._preview_total, 0.0)
        if total > 0.0 and not self.preview_slider.scrubbing():
            value = int(round(min(max(seconds, 0.0), total) / total * SEEK_RANGE))
            self.preview_slider.setValue(value)
        self._show_preview_value(self.preview_slider.value())

    def seek_preview(self, value):
        """进度条松手：跳到那个位置接着放。"""
        if not self._previewing:
            return
        seconds = self._preview_value_to_seconds(value)
        if not self._start_preview_at(seconds):
            self.stop_preview(finished=True)
            return
        self.set_status('试听 %s / %s' % (preview.format_time(seconds),
                                         preview.format_time(self._preview_total)), 'play')

    def _preview_tick(self):
        """刷「试听 0:12 / 1:35」和进度条；放完自动收尾（winsound 不会通知我们）。"""
        if not self._previewing or self._preview_t0 is None:
            return
        elapsed = self._preview_base + (time.time() - self._preview_t0)
        if elapsed >= self._preview_total + 0.3:
            self.stop_preview(finished=True)
            return
        self._set_preview_position(elapsed)
        self.set_status('试听 %s / %s' % (preview.format_time(elapsed),
                                         preview.format_time(self._preview_total)), 'play')

    def stop_preview(self, finished=False):
        """停掉试听（开始演奏 / 换文件 / 退出时也会调）。没在试听就什么都不做。"""
        if not self._previewing and self._preview_path is None:
            return
        self._previewing = False
        self._preview_t0 = None
        self._preview_base = 0.0
        try:
            self.preview_timer.stop()
        except Exception:
            pass
        self.previewer.stop()
        if self._preview_path:
            preview.remove(self._preview_path)
            self._preview_path = None
        self._set_preview_button(False)
        try:
            self.preview_slider.setEnabled(False)
            self.preview_slider.setValue(0)
        except Exception:
            pass
        if self._playing():
            return                  # 正在演奏 / 跟奏：状态归它们管，别去动胶囊
        self.set_status('已就绪' if self.analysis is not None else '就绪',
                        'ready' if self.analysis is not None else 'idle')
        self.log('试听结束' if finished else '试听已停止')

    def _set_preview_button(self, on):
        """试听按钮：放着的时候显示成「■ 停止」并染红（换 objectName 要重新 polish）。"""
        try:
            self.btn_preview.setText('■  停止' if on else '♪  试听')
            self.btn_preview.setObjectName('previewOn' if on else '')
            style = self.btn_preview.style()
            style.unpolish(self.btn_preview)
            style.polish(self.btn_preview)
            self.btn_preview.update()
        except Exception:
            pass

    # ---------- 录制 ----------

    def _rec_key(self):
        """录制键的显示写法（精简版没有这一行，给个兜底）。"""
        return hotkeys.pretty_combo(self.keymap.combo_of('record')) or 'F10'

    def toggle_record(self):
        """录制键 / 「录制」按钮：开始或者收工。"""
        if not edition.has_recorder():
            self.log('这一版没带录制功能（精简版不含录制）；要录请用完全版')
            return
        if self.recording:
            self.stop_record()
        else:
            self.start_record()

    def start_record(self):
        """
        开始录制。

        只认游戏里那八个琴键（z x c v b n m ,）和三个鼠标键，别的键一律放过 ——
        所以录制期间照样能跑能跳、热键照样管用，混不进录音里。
        """
        if self._playing():
            self.log('正在演奏，先按 F8 停掉再录')
            return
        self.stop_preview()                     # 试听和录制都会出声，错开
        if not self.recorder.start():
            self.log('已经在录了')
            return
        self.recording = True
        self._rec_tonic = self._record_tonic()   # 出声按哪个主音算：开始录时定下来
        self.rec_timer.start()
        if self.btn_record is not None:
            self.btn_record.setText('⏹  结束录制')
        self.set_status('录制中', 'rec')
        self.overlay.record_begin()
        log_event('开始录制')
        self.log('开始录制：现在弹吧（z x c v b n m , + 鼠标左键降调 / 中键升半音 / 右键升调），'
                 '再按一次 %s 收工' % self._rec_key())
        self._hold_system_sounds()
        if self._monitor_on and notesound.NotePlayer.available():
            self.log('录制时发声：开 —— 按下去就响')
        else:
            self.log('录制时发声：关 —— 录的时候不出声（想开着就勾界面上那个「录制时发声」）')

    def stop_record(self):
        """收工：把录到的东西写成谱面 + midi，并送进编辑器。"""
        self.recording = False
        self.rec_timer.stop()
        if self.btn_record is not None:
            self.btn_record.setText('⏺  录制')
        self.note_sound.stop()                  # 收工了，最后一个音别拖着
        self._release_system_sounds()
        count = self.recorder.stop()
        spans = self.recorder.spans()
        log_event('结束录制，录到 %d 个音' % count)
        if not spans:
            self.log('这次一个音都没录到 —— 录制期间只认 z x c v b n m , 这八个键')
            self.set_status('已就绪', 'ready')
            if self.overlay_on:
                self.overlay.set_ready(len(self.score_events) or None)
            return
        self.save_recording(spans)

    def _rec_tick(self):
        """录制中：状态胶囊和右上角浮窗上的计数跟着走。"""
        if not self.recording:
            return
        count = self.recorder.count()
        self.set_status('录制中 %d 个音' % count, 'rec')
        if self.overlay_on:
            self.overlay.record_count(count)

    def _rec_note(self, token, down):
        """
        录制回调（跑在钩子线程里）：按下就出声，松开就停 —— 按住多久响多久。

        **纯开关**：界面上那个「录制时发声」勾着就响、不勾就哑，不去猜在不在游戏里 ——
        开就是开、关就是关。嫌在游戏里吵就自己关掉。
        这里只读几个变量，绝不出声就立刻返回，免得拖慢钩子（钩子卡住会被系统摘掉）。
        """
        if not self.recording or not self._monitor_on:
            return
        if self.note_sound is None or not notesound.NotePlayer.available():
            return
        rel = jianpu.token_to_rel(token)
        if rel is None:
            return
        pitch = self._rec_tonic + rel
        if down:
            self.note_sound.play(pitch)          # 按下去：开始响（一直响到松开）
        else:
            self.note_sound.release(pitch)       # 松开：掐掉

    def _record_tonic(self):
        """这次录的谱面用哪个主音：跟着现在这首走，没有就用 60（C4）。"""
        analysis = getattr(self, 'analysis', None)
        if analysis is not None:
            try:
                return int(analysis.tonic)
            except Exception:
                pass
        return 60

    def save_recording(self, spans):
        """录制 -> TONIC 谱面 + 单音 midi，并把它接成「当前这首」。"""
        pairs, squeezed = recorder.to_pairs(spans)
        snap_ms = self._snap_ms()
        snapped = 0
        if snap_ms:
            pairs, snapped = recorder.snap_pairs(pairs, snap_ms)
        if not pairs:
            self.log('录到的内容拼不出谱面')
            return False
        tonic = self._record_tonic()
        try:
            os.makedirs(REC_DIR, exist_ok=True)
        except OSError as exc:
            self.log('建不了录音文件夹（%s）：%s' % (REC_DIR, exc))
            return False
        name = '录音 %s' % time.strftime('%Y-%m-%d %H-%M-%S')
        midi_path = os.path.join(REC_DIR, name + '.mid')
        try:
            # 谱面文件名跟别处一个规矩：TONIC<tonic> <midi 名>.txt
            score_path = jianpu.write_score(midi_path, tonic, pairs, out_dir=REC_DIR)
            recorder.write_midi(pairs, midi_path, tonic, bpm=REC_BPM)
        except Exception as exc:
            self.log('存录音失败：%s' % exc)
            log_crash(traceback.format_exc())
            self.set_status('出错了', 'error')
            return False
        total = recorder.total_seconds(pairs)
        played = len(recorder.pairs_to_notes(pairs, tonic))
        self.log('录好了：%d 个音，共 %.1f 秒（谱面 %s）' % (played, total, score_path))
        if squeezed:
            self.log('有 %d 个音跟上一个音压在一起了，后面的把前面的截短了 —— '
                     '想还原就去编辑器里把前一个音拖长' % squeezed)
        if snapped:
            self.log('音长吸附（%d 毫秒）：%d 个时值修到了格子上' % (snap_ms, snapped))
        self.log('单音 midi：%s' % midi_path)
        log_event('录音存好：%s（%d 个音，%.1f 秒，tonic %d，挤短 %d 个）'
                  % (os.path.basename(score_path), played, total, tonic, squeezed))
        self._adopt_recording(midi_path, score_path, pairs, tonic)
        self._open_recording_in_editor(midi_path, pairs, tonic, total)
        return True

    def _adopt_recording(self, midi_path, score_path, pairs, tonic):
        """把刚录的这首接成「当前这首」：F6 直接回放，右上角浮窗显示已就绪。"""
        self.stop_preview()
        self.midi_path = midi_path
        self.score_path = score_path
        self.score_events = jianpu.parse_score(jianpu.dump_score(pairs))
        self.analysis = None                    # 不是从 midi 转来的，没有分析报告
        self.track_index = -1
        self._set_file_label(midi_path)
        self.track_label.setText('刚录的（不是从 midi 转的）')
        self.track_box.clear()
        self.btn_track.setEnabled(False)
        self.tonic_label.setText('TONIC %d (%s)' % (tonic, midi_analyze.pitch_name(tonic)))
        self.score_label.setText('%s　共 %d 个音'
                                 % (os.path.basename(score_path), len(self.score_events)))
        self.counter.setText('0 / %d' % len(self.score_events))
        self.bar.setValue(0)
        self.btn_preview.setEnabled(True)
        self.set_status('已就绪', 'ready')
        self.setWindowTitle('%s - %s' % (APP_TITLE, os.path.basename(midi_path)))
        if self.overlay_on:
            self.overlay.set_ready(len(self.score_events))
        self._refresh_follow(show=self.follow_on)

    def _open_recording_in_editor(self, midi_path, pairs, tonic, total):
        """录完直接摆进编辑器，有瑕疵可以当场拖。"""
        if editor is None or not edition.has_editor():
            return
        try:
            self._make_editor()
            if self.editor is None:
                return
            notes = [editor.Note(start, dur, pitch)
                     for start, dur, pitch in recorder.pairs_to_notes(pairs, tonic)]
            score = editor.Score(notes, tonic=tonic, bpm=REC_BPM, path=midi_path,
                                 track_index=-1, track_name='录音')
            score.dirty = True                  # 刚录的还没存过：让自动保存盯着它
            self.editor.set_score(score, '刚录的：%d 个音，共 %.1f 秒。哪里不对直接拖，'
                                         '改完按「导出 MIDI」会自动载回主程序。'
                                         % (len(notes), total))
        except Exception as exc:
            self.log('编辑器没打开：%s' % exc)
            return
        if self.in_game():
            self.log('回桌面点「简谱编辑器」就能改这段录音')
            return
        self.tabs.setCurrentWidget(self.editor_page)
        self.log('已经切到「简谱编辑器」')

    # ---------- 演奏 ----------

    def _playing(self):
        """正在演奏（或者正在跟奏练习）都算「忙」，这时候不给换文件 / 换音轨。"""
        return self.practice or (self.worker is not None and self.worker.is_alive())

    def _note_len(self):
        """界面上「单音时长」是几秒。"""
        try:
            return self.note_ms.value() / 1000.0
        except Exception:
            return player.NOTE_MS_DEFAULT / 1000.0

    def _note_mode(self):
        try:
            return self.note_mode.currentText()
        except Exception:
            return player.NOTE_MODE_DEFAULT

    def _shaped_events(self):
        """
        按当前的「音长」把谱面整理一遍：演奏和跟奏看到的必须是同一份。

        默认只改「每个音按住多久」（顶到下一个音就自动缩短），时间轴一点不动 —— 节奏
        还是 midi 原速；勾了「整首放慢」才整首等比放慢，倍数记在 self._stretch。
        """
        if not self.score_events:
            self._stretch = 1.0
            return []
        shaped, self._stretch = player.Player.shape(
            self.score_events, self._note_mode(), self._note_len(), self._stretch_on())
        return shaped

    def _stretch_on(self):
        """界面上「整首放慢」勾没勾。"""
        try:
            return bool(self.stretch_box.isChecked())
        except Exception:
            return False

    def _stretch_note(self):
        """放慢倍数大于 1 时给一句解释，没放慢就返回空串。"""
        factor = getattr(self, '_stretch', 1.0)
        if factor <= 1.001:
            return ''
        return '（最短的音不够 %d ms，整首歌放慢了 %.2f 倍，相当于 BPM 降到 %.0f%%）' % (
            self.note_ms.value(), factor, 100.0 / factor)

    def start_play(self):
        if self._playing():
            self.log('正在演奏中，先按 F8 停止')
            return
        if self.recording:
            self.log('正在录制，先按 %s 收工' % hotkeys.pretty_combo(self.keymap.combo_of('record')))
            return
        self.stop_preview()                     # 试听和演奏不同时响
        if not self.score_events:
            self.log('先选一个 midi 文件')
            return
        if self.follow_on and self._practice_pace():
            self.start_practice()
            return
        speed = self._speed()
        events = self._shaped_events()          # 音长模式 / 单音时长在这里生效
        note_mode, note_ms = self._note_mode(), self.note_ms.value()
        self.log('开始演奏：%s（速度 %.2fx，音长 %s %d ms）%s'
                 % (os.path.basename(self.score_path), speed, note_mode, note_ms,
                    self._stretch_note()))
        log_event('开始演奏：%s（速度 %.2fx，音长 %s %d ms，最短按键 %d ms）%s' % (
            os.path.basename(self.score_path), speed, note_mode, note_ms,
            self.hold_ms.value(), self._stretch_note()))
        min_hold = self.hold_ms.value() / 1000.0
        self.bar.setValue(0)
        self.counter.setText('0 / %d' % len(self.score_events))
        self.btn_stop.setEnabled(True)
        self.btn_choose.setEnabled(False)
        self.btn_track.setEnabled(False)
        self.track_box.setEnabled(False)
        self.set_status('演奏中', 'play')
        self.btn_preview.setEnabled(False)
        self._step_aside()
        self.overlay.begin(len(self.score_events))
        window = self._refresh_follow()
        if window is not None:
            window.begin()
        self.worker = threading.Thread(
            target=self._play, args=(events, speed, min_hold), daemon=True)
        self.worker.start()

    def _play(self, events, speed, min_hold):
        ok = False
        try:
            ok = bool(self.player.play(events, speed, on_progress=self.progress.emit,
                                       min_hold=min_hold))
        except Exception as exc:
            self.message.emit('演奏出错：%s' % exc)
            log_crash(traceback.format_exc())
        finally:
            self.finished.emit('done' if ok else 'stop')

    def _on_progress(self, done, total):
        self.bar.setRange(0, total)
        self.bar.setValue(done)
        self.counter.setText('%d / %d' % (done, total))
        self.overlay.set_progress(done, total)

    def _on_finished(self, reason='stop'):
        # 只更新界面和浮窗，绝不把主窗口弹回来（会把全屏游戏顶掉）
        self.btn_stop.setEnabled(False)
        self.btn_choose.setEnabled(True)
        self.btn_preview.setEnabled(self.analysis is not None)
        self.track_box.setEnabled(True)
        self.btn_track.setEnabled(self.analysis is not None and len(self.analysis.tracks) > 1)
        self.set_status('已就绪', 'ready')
        self.overlay.finish('done' if reason == 'done' else 'stop')
        if self.follow is not None and self.follow_on:
            self.follow.finish('done' if reason == 'done' else 'stop')
        log_event('演奏结束（%s）' % ('走完' if reason == 'done' else '中途停止'))

    # ---------- 跟奏练习（等你按对） ----------

    def _practice_pace(self):
        """界面上的「跟奏节奏」是不是选在练习模式。"""
        return (self.follow_pace is not None
                and self.follow_pace.currentText() == follow.PRACTICE_PACE)

    def start_practice(self):
        """
        跟奏练习：程序一个键都不发，音符停在判定线上等你按对，按对了才继续往下走。

        新手练的时候不用被原曲的速度拖着跑。
        """
        window = self._ensure_follow()
        if window is None:
            self.log('跟奏窗口用不了（这个版本没带跟奏，或者窗口创建失败）')
            return
        self.stop_preview()                          # 试听和跟奏不同时响
        if self.follow_box is not None and not self.follow_box.isChecked():
            self.follow_box.setChecked(True)         # 顺手把跟奏窗口显示出来
        window.on_progress = self._on_practice_progress
        window.on_finish = self._on_practice_finish
        # 练习按谱面原时值走（不受倍速影响），但音长设置照旧生效：
        # 长条的长度就是它要按多久，所以必须和演奏时一模一样
        window.set_score(self._shaped_events(), 1.0)
        self.practice = True
        self.bar.setRange(0, len(self.score_events))
        self.bar.setValue(0)
        self.counter.setText('0 / %d' % len(self.score_events))
        self.btn_stop.setEnabled(True)
        self.btn_choose.setEnabled(False)
        self.btn_track.setEnabled(False)
        self.track_box.setEnabled(False)
        self.set_status('跟奏中', 'play')
        self.btn_preview.setEnabled(False)
        self.overlay.begin(len(self.score_events))
        self._step_aside()
        window.begin(wait=True)
        self.log('跟奏练习开始：音符停在判定线上等你按对（程序不帮你按键；F7 暂停，F8 停止）')
        log_event('跟奏练习开始：%s' % os.path.basename(self.score_path or ''))

    def _on_practice_progress(self, done, total):
        """跟奏练习的进度（按对了几个音）。"""
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(done)
        self.counter.setText('%d / %d' % (done, total))
        self.overlay.set_progress(done, total)

    def _on_practice_finish(self, reason='stop'):
        """跟奏练习结束：自己按完了，或者中途按了 F8。"""
        if not self.practice:
            return
        self.practice = False
        self.btn_stop.setEnabled(False)
        self.btn_choose.setEnabled(True)
        self.btn_preview.setEnabled(self.analysis is not None)
        self.track_box.setEnabled(True)
        self.btn_track.setEnabled(self.analysis is not None and len(self.analysis.tracks) > 1)
        self.set_status('已就绪', 'ready')
        self.overlay.finish('done' if reason == 'done' else 'stop')
        self.log('跟奏练习结束' + ('：整首按完了' if reason == 'done' else '（中途停止）'))
        log_event('跟奏练习结束（%s）' % ('按完' if reason == 'done' else '中途停止'))

    def quit_app(self, reason=''):
        """
        退出：该收的收掉，然后直接 os._exit 结束进程。

        不调用 QApplication.quit() 是有意的——Qt 拆窗口 / Python 拆解释器的过程最容易
        崩（崩溃日志里那几次 0xc0000005 就落在 pyside6 里），反正程序本来就要退了，
        干脆跳过整个析构过程，走得干净。
        """
        if self.editor is not None and not self.editor.ask_save():
            return False                  # 编辑器里有没保存的改动，用户反悔了
        for window in list(self.editor_windows):
            if not window.ask_save():
                return False              # 另外开着的编辑器窗口里也有一份没存
        log_event('退出（%s）' % (reason or '未说明'))
        try:
            if getattr(self, 'recorder', None) is not None:
                self.recorder.cancel()          # 正在录就直接扔掉，别留个钩子在系统里
            self._release_system_sounds()       # 系统提示音别一直按着
        except Exception:
            pass
        try:
            self.note_sound.stop()
            self.stop_preview()
            self.player.stop()
            self.player.release_all()
        except Exception:
            pass
        if self.hotkey_timer is not None:
            try:
                self.hotkey_timer.stop()
            except Exception:
                pass
        if self.hotkeys is not None:
            try:
                self.hotkeys.stop()
            except Exception:
                pass
        try:
            self.overlay.shutdown()
        except Exception:
            pass
        if self.follow is not None:
            try:
                self.follow.shutdown()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.hide()        # 让托盘图标立刻消失，别留个影子
            except Exception:
                pass
        try:
            # 正常退出 = 该问的都问过了（上面 ask_save），这些底子留着只会让下次启动
            # 又弹一份出来，直接清掉。意外退出（崩溃 / 被强杀）走不到这儿。
            autosave.clear()
        except Exception:
            pass
        log_event('收尾完成，退出进程')
        os._exit(0)

    def closeEvent(self, event):
        if self.quit_app('关闭窗口') is False:
            event.ignore()                # 编辑器还没存，别关
            return
        event.accept()


def main(argv):
    sys.excepthook = lambda kind, value, tb: log_crash(''.join(traceback.format_exception(kind, value, tb)))

    # 万一发生的是「原生崩溃」（不是 Python 异常），也能在日志里留下当时的 Python 调用栈
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        faulthandler.enable(file=open(LOG_PATH, 'a', encoding='utf-8', buffering=1))
    except Exception:
        pass
    log_event('启动：%s' % (argv,))
    drop_old_autostart()          # 老版本留下的开机自启项：启动时清掉（这个功能已经删了）

    if '--no-admin' not in argv and not is_admin():
        if relaunch_as_admin():
            return 0
        log_crash('提权被拒绝（UAC）')

    initial = None
    for arg in argv:
        if not arg.startswith('--') and os.path.isfile(arg):
            initial = arg
            break

    # 已经有一个在跑了（多半就是双击 .mproj 又起了一个进程）：把文件交给它，自己直接退。
    # 只有打包成 exe 之后才管这一套，源码运行时大家的 exe 都是 python.exe，认不准。
    if single.handoff(initial or ''):
        log_event('已有实例在运行，把「%s」交给它打开' % (initial or '(显示窗口)'))
        return 0

    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_TITLE)
    app.setStyle('Fusion')
    app.setPalette(dark_palette())
    # 配色主题：先把主题文件读进来，再照用户上次选的那套上样式
    theme.load_all()
    theme.set_current(_saved_theme())
    app.setStyleSheet(style_sheet())
    window = MainWindow(initial)
    window.show()
    return app.exec()


if __name__ == '__main__':
    try:
        code = main(sys.argv[1:])
    except Exception:
        log_crash(traceback.format_exc())
        code = 1
    os._exit(code)
