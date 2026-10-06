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
import re
import shutil
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
    from PySide6.QtCore import (QEvent, QPoint, QRectF, QSettings, QSize, Qt, QTimer, QUrl,
                                Signal)
    from PySide6.QtGui import (QBrush, QColor, QCursor, QDesktopServices, QFont, QIcon,
                               QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut)
    from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                                   QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                                   QListWidget, QListWidgetItem, QMenu, QProgressBar,
                                   QPushButton, QSlider,
                                   QScrollArea, QSizePolicy, QSpinBox, QSystemTrayIcon,
                                   QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                                   QTabWidget, QTextEdit, QVBoxLayout, QWidget)
except ImportError:                                   # 装了 PyQt6 也行
    from PyQt6.QtCore import (QEvent, QPoint, QRectF, QSettings, QSize, Qt, QTimer, QUrl,
                              pyqtSignal as Signal)
    from PyQt6.QtGui import (QBrush, QColor, QCursor, QDesktopServices, QFont, QIcon,
                             QKeySequence, QPainter, QPalette, QPen, QPixmap, QShortcut)
    from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
                                 QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QMenu, QProgressBar,
                                 QPushButton, QSlider,
                                 QScrollArea, QSizePolicy, QSpinBox, QSystemTrayIcon,
                                 QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                                 QTabWidget, QTextEdit, QVBoxLayout, QWidget)

import audiowatch
import autosave
import edition
import errorreport
import fileassoc
import notice as notice_mod
import uiicons
try:                                  # 精简版不带简谱编辑器
    import editor
except Exception:                     # pragma: no cover
    editor = None
import follow
import hotkeys
import jianpu
import judgeicons
import library
import manual
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
import rhythm
import update as update_mod


# 精简版没有录制，索性把这一行从键位表 / 提示 / 对话框里整个摘掉，
# 免得界面上挂着一个按下去只会说「这一版没有」的键。
if not edition.has_recorder():
    hotkeys.ACTION_ORDER = tuple(a for a in hotkeys.ACTION_ORDER if a != 'record')
    hotkeys.DEFAULT_BINDINGS = dict((a, c) for a, c in hotkeys.DEFAULT_BINDINGS.items()
                                    if a != 'record')

APP_TITLE = 'MIDI 简谱自动演奏' + ('（精简版）' if edition.LITE else '')
APP_VERSION = '1.1.0'
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
# 「同音重复」敏感度滑块：0 档 = 标准参数，越往右连着弹的同一个音越容易被切成好几个音
# （档位表在 mp3midi/audio2midi.py 的 REPEAT_LEVELS 里）。默认 0 档。
REPEAT_LEVEL_DEFAULT = 0
# 老版本那个「同音重复更敏感」勾选框，等于现在滑块上这一档（设置从旧版升上来的用）
REPEAT_LEVEL_SENSITIVE = 3
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

# 标题行 / 动作条上所有控件的统一高度。以前「已就绪」胶囊 28、圆钮 32、主题下拉
# 自适应，一行里三个高度，看着就散。现在全部按这个来。
# 右侧分两组：左边「已就绪 + 主题」是圆角矩形（ROUND_W 宽），右边
# 「键位 / 最小化 / 最大化 / 关闭」是正方形（BAR_H × BAR_H，直角）。
BAR_H = 32

# 「显示演奏状态」（屏幕右上角那个演奏进度浮窗）的默认值：默认显示（勾上）。
# 调试时嫌它挡视线就在选项里关掉，关掉之后演奏时也不弹。
OVERLAY_DEFAULT = True

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
/* 新拟态（Neumorphism）：所有圆钮都是「同一块底色上挤出来的一小块」——
   上边亮、下边暗（qlineargradient 冒充凸起），按下去把明暗倒过来（像一个坑）。
   Qt 的 QSS 没有 box-shadow，真正的柔光投影交给 QGraphicsDropShadowEffect（见 _soft_shadow）。 */
/* 标题行最右边那一组：键位 / 最小化 / 最大化 / 关闭 —— 四个**直角方块**，
   尺寸在代码里统一 setFixedSize(BAR_H, BAR_H)；QSS 这边 padding 必须给 0，
   不然 32px 的方格里光 padding 就占掉 30px，单字会被裁掉。 */
QPushButton#coverClose, QPushButton#minButton, QPushButton#maxButton, QPushButton#iconButton {
    padding: 0; border-radius: 0; font-size: 15px;
    min-height: @BARHIN@px; max-height: @BARHIN@px; min-width: @BARHIN@px; max-width: @BARHIN@px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #242d40, stop:1 #1b2231);
    border: 1px solid #232b3d; color: #b9c1d1; }
QPushButton#coverClose { color: #8b93a7; }
QPushButton#maxButton { font-weight: 600; color: #bcd8ff; }
/* 鼠标移上去：边框亮起来，一眼看出「这一块能按」（关闭单独走红，别搞混） */
QPushButton#minButton:hover, QPushButton#maxButton:hover, QPushButton#iconButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #242c3c, stop:1 #1b2231);
    border-color: #7fb0ff; color: #ffffff; }
QPushButton#coverClose:hover { background: #b0413e; border-color: #b0413e; color: #ffffff; }
QPushButton#minButton:pressed, QPushButton#maxButton:pressed, QPushButton#iconButton:pressed,
QPushButton#coverClose:pressed { background: #181e2b; }
QPushButton#maxButton[maxed="true"] { background: #2f6fd0; border-color: #9ec9ff;
                                      color: #ffffff; }
QLabel { color: #e6e9ef; }
QLabel#title { font-size: 19px; font-weight: 600; }
QLabel#subtitle { color: #8b93a7; font-size: 12px; }
/* 「已就绪」胶囊：圆角矩形（跟主题下拉一排），高度跟标题行其它控件对齐 */
QLabel#pill { border-radius: 8px; min-height: @BARH@px; max-height: @BARH@px; }
QLabel#fieldLabel { color: #8b93a7; font-size: 12px; }
QLabel#value { color: #dfe4ee; }
QLabel#counter { color: #8b93a7; font-size: 12px; }
QLabel#hint { color: #6f7787; font-size: 12px; }
/* 卡片 / 面板：新拟态的面 —— 比窗口底色亮一档，上亮下暗的柔光渐变 + 中性描边 */
QFrame#card { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1b2231, stop:1 #151b27);
              border: 1px solid #232b3d; border-radius: 14px; }
/* min-height 是「内容最小高度」：样式表里的 padding 不算进 Qt 的最小尺寸，
   不写这一句，窗口一矮，布局就会把按钮压到 26px 高 —— 按钮上的字被上下切掉。
   加上它，布局的最小高度就是真实需要的高度，宁可让窗口长高也不裁字。 */
QPushButton { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #242d40, stop:1 #1b2231);
              border: 1px solid #232b3d; border-radius: 10px;
              padding: 6px 15px; min-height: 17px; color: #dfe4ee; }
/* 通用按钮悬停：背景亮一档 + 边框亮起来（「选择 MIDI 文件 / 音频转 MIDI / 曲库 /
   联网曲库 / 简谱编辑器 / 应用 …」都统一有这道边缘高光） */
QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #242c3c, stop:1 #1b2231);
                    border-color: #4b8ef8; }
QPushButton:pressed { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #141a26, stop:1 #1b2231); }
QPushButton:disabled { background: #171b24; border-color: #222836; color: #5c6478; }
/* 曲库切换那种「一排里选一个」的按钮：选中的那个染成主色 */
QPushButton#siteBtn:checked { background: #3b82f6; border-color: #3b82f6; color: #ffffff;
                              font-weight: 600; }
QPushButton#siteBtn:checked:hover { background: #4b8ef8; }
QPushButton#primary { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #7fb0ff, stop:1 #3b82f6);
                      border-color: #3b82f6; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #9ec9ff, stop:1 #4b8ef8);
                            border-color: #bcd8ff; }
QPushButton#primary:pressed { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2f6fd0, stop:1 #24405f); }
QPushButton#primary:disabled { background: #24405f; border-color: #24405f; color: #7d8ea0; }
/* 正在试听时按钮染红，一眼能看出「再按一下就是停」 */
QPushButton#previewOn { background: #3a2226; border-color: #6b2f33; color: #f0a0a0; }
QPushButton#previewOn:hover { background: #46282d; }
QComboBox { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #151b27, stop:1 #1b2231);
            border: 1px solid #232b3d; border-radius: 10px;
            padding: 6px 10px; min-height: 17px; color: #dfe4ee; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { background: #1b2231; border: 1px solid #232b3d;
                              selection-background-color: #3b82f6; outline: none; }
/* 自绘下拉框：候选列表是主窗口里的子控件，不开新窗口 —— 游戏不会被顶回桌面 */
/* 主题下拉：跟「已就绪」胶囊并排，做成同高的**圆角矩形**（尺寸代码里定死 BAR_H） */
QPushButton#combo { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #151b27, stop:1 #1b2231);
                    border: 1px solid #232b3d; border-radius: 8px;
                    min-height: @BARHIN@px; max-height: @BARHIN@px;
                    padding: 0 10px; color: #dfe4ee; text-align: left; }
QPushButton#combo:hover { background: #242c3c; border-color: #4b8ef8; }
QPushButton#combo[open="true"] { border-color: #3b82f6; background: #1b2231; }
QFrame#comboPanel { background: #12161f; border: 1px solid #232b3d; border-radius: 12px; }
QWidget#comboInner { background: transparent; }
QPushButton#comboRow { background: transparent; border: none; border-radius: 6px;
                       padding: 2px 8px; color: #cbd3e1; font-size: 12px; text-align: left; }
QPushButton#comboRow:hover { background: #232b3a; color: #ffffff; }
QPushButton#comboRow[current="true"] { background: #1d3555; color: #cfe1ff; font-weight: 600; }
QPushButton#comboRow:disabled { background: transparent; color: #565e70; }
QSpinBox { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #141a26, stop:1 #1b2231);
           border: 1px solid #232b3d; border-radius: 10px;
           padding: 5px 8px; min-height: 17px; color: #dfe4ee; }
QSpinBox:focus { border-color: #3b82f6; }
QSpinBox::up-button, QSpinBox::down-button { background: #232a38; border: none; width: 16px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #2c3547; }
QSpinBox::up-arrow { width: 0; height: 0; border-left: 3px solid transparent;
                     border-right: 3px solid transparent; border-bottom: 4px solid #9aa6ba; }
QSpinBox::down-arrow { width: 0; height: 0; border-left: 3px solid transparent;
                       border-right: 3px solid transparent; border-top: 4px solid #9aa6ba; }
QLineEdit { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #141a26, stop:1 #1b2231);
            border: 1px solid #232b3d; border-radius: 10px;
            padding: 6px 10px; min-height: 17px; color: #dfe4ee; }
QLineEdit:focus { border-color: #3b82f6; }
QLabel#warn { color: #f0a0a0; background: #3a2226; border: 1px solid #6b2f33;
              border-radius: 10px; padding: 8px 10px; }
/* 「公开曲库」那种要好好说的事：主色打底，比红字温和 */
QLabel#notice { color: #bcd8ff; background: #1d3555; border: 1px solid #24405f;
                border-radius: 10px; padding: 8px 10px; }
QCheckBox { color: #dfe4ee; spacing: 8px; }
QCheckBox::indicator { width: 15px; height: 15px; border-radius: 5px;
                       border: 1px solid #232b3d; background: #151b27; }
QCheckBox::indicator:hover { border-color: #3b82f6; }
QCheckBox::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QProgressBar { background: #141a26; border: none; border-radius: 4px; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7fb0ff, stop:1 #3b82f6);
                      border-radius: 4px; }
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
QTextEdit { background: #141a26; border: 1px solid #232b3d; border-radius: 12px;
            color: #b9c1d1; font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px; padding: 8px; }
QListWidget { background: #141a26; border: 1px solid #232b3d; border-radius: 12px;
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
/* ============ 新拟态：顶部那几个方按钮 / 面板 ============ */
/* 「公告 / 更新 / B站主页 / GitHub」那一条。它不套卡片，直接贴在窗口上 */
QFrame#actionStrip { background: transparent; border: none; }
QPushButton#neuIcon { border-radius: 10px;
                      min-height: @BARHIN@px; max-height: @BARHIN@px; padding: 0 12px;
                      background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #242d40, stop:1 #1b2231);
                      border: 1px solid #232b3d; color: #dfe4ee; }
QPushButton#neuIcon:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #242c3c, stop:1 #1b2231);
                            border-color: #3b82f6; color: #ffffff; }
QPushButton#neuIcon:pressed { background: #181e2b; border-color: #3b82f6; }
QPushButton#neuIcon:disabled { background: #171b24; border-color: #222836; color: #5c6478; }
QLabel#versionTag { color: #8b93a7; font-size: 12px; }
QLabel#versionTag[stale="true"] { color: #e0b341; }
/* 公告 / 更新这种贴在窗口里的浮层面板 */
QFrame#neuPanel { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1b2231, stop:1 #151b27);
                  border: 1px solid #232b3d; border-radius: 14px; }
QLabel#panelTitle { font-size: 15px; font-weight: 600; color: #e6e9ef; }
QLabel#panelMeta { color: #8b93a7; font-size: 12px; }
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


def asset_path(name):
    """找一个打包进来的资源文件（pay.jpg 这种）：安装目录 / _internal / 源码目录。"""
    roots = [app_dir(), os.path.join(app_dir(), '_internal'),
             os.path.dirname(os.path.abspath(__file__))]
    for root in roots:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    return ''


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


# 窗口的设计尺寸（照 1440 逻辑高度以上的屏幕定的）。屏幕小就按比例缩 ——
# 1K 屏 + 系统缩放 125% / 150% 时逻辑高度只剩 720~860，照 700 开出去会顶到屏幕外面，
# 点「曲库」撑高之后连标题栏都跑出去，窗口就拖不回来了。
BASE_WINDOW_W = 780
BASE_WINDOW_H = 700
PICKER_H = 840                 # 「曲库 / 选文件」那一页撑到多高


def window_size_for(screen_w, screen_h):
    """按屏幕可用区域算一个合适的窗口尺寸（780×700 封顶，屏幕小就缩）。"""
    try:
        screen_w = int(screen_w)
        screen_h = int(screen_h)
    except (TypeError, ValueError):
        return BASE_WINDOW_W, BASE_WINDOW_H
    if screen_w <= 0 or screen_h <= 0:
        return BASE_WINDOW_W, BASE_WINDOW_H
    width = max(min(700, screen_w - 40), min(BASE_WINDOW_W, int(screen_w * 0.62)))
    height = max(min(560, screen_h - 40), min(BASE_WINDOW_H, int(screen_h * 0.86)))
    return int(width), int(height)


def log_crash(text):
    """出错时把完整的 traceback 记进同一个日志，顺手往曲库仓库上报一份（可关）。"""
    log_event('出错了：\n%s' % text)
    try:
        errorreport.send(text, where='程序')
    except Exception:
        pass


def _safe_size(path):
    """文件多大（字节）；拿不到就记 -1。"""
    try:
        return os.path.getsize(path)
    except Exception:
        return -1


def report_convert_failure(source, exc, stage, detail=''):
    """
    音频转 MIDI 失败：往曲库仓库上报一条（脱敏）。

    以前这个异常只进运行日志 + 状态栏 —— 用户不主动把日志发出来，谁也不知道
    他们那边到底缺什么。上报内容：异常类型 + traceback（路径会被换成 <path>）、
    卡在哪一步、音频后缀和大小、打包版还是源码、同音重复档位、三个后端的探测
    结果（含 import 失败原因）。文件名和完整路径都不传，只传「路径里有没有中文」；
    note 里那份拼出来的说明也过一遍 sanitize，防止后端的 import 报错里夹着路径。
    """
    try:
        text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        bits = ['阶段=%s' % stage,
                '后缀=%s' % (os.path.splitext(str(source or ''))[1].lower() or '无'),
                '字节=%s' % _safe_size(source),
                '打包=%s' % ('是' if getattr(sys, 'frozen', False) else '否'),
                '路径中文=%s' % ('是' if errorreport.has_chinese(source) else '否'),
                '敏感度=%s' % (detail or '标准')]
        if audio2midi is not None:
            try:
                bits.append('后端=' + '；'.join(
                    '%s%s' % (name, '可用' if info['ok']
                              else '失败[%s]' % (info['error'] or '原因不明'))
                    for name, info in audio2midi.backend_probe().items()))
            except Exception:
                pass
        errorreport.send('音频转 MIDI 失败（%s）\n%s' % (type(exc).__name__, text),
                         where='音频转MIDI',
                         # note 也过一遍脱敏：后端那条 import 报错里万一带着
                         # 「C:\Users\某某\…\onnx.dll」这种路径，别漏出去
                         note=errorreport.sanitize(' '.join(bits))[:1200])
    except Exception:
        pass


_BACKEND_ALERT = {'sent': False}


def report_backend_missing(probe):
    """
    打包版应该自带 basic-pitch（有伴奏的歌全靠它）。真用不了 = 打包 / 运行库
    出了问题，报一条；一次运行只报一条，别每次转换都刷屏。
    """
    if _BACKEND_ALERT['sent']:
        return
    _BACKEND_ALERT['sent'] = True
    try:
        lines = ['打包版缺转换后端（basic-pitch 本该自带）',
                 '版本=%s' % APP_VERSION,
                 '打包=%s' % ('是' if getattr(sys, 'frozen', False) else '否')]
        for name, info in probe.items():
            lines.append('%s：%s' % (name, '可用' if info['ok']
                                     else '不可用（%s）' % (info['error'] or '原因不明')))
        errorreport.send('\n'.join(lines), where='转换后端')
    except Exception:
        pass


def _thread_excepthook(args):
    """后台线程没接住的异常：跟前台一样记日志 + 上报。"""
    try:
        log_crash('线程 %s 出错：\n%s'
                  % (getattr(args.thread, 'name', '?'),
                     ''.join(traceback.format_exception(args.exc_type, args.exc_value,
                                                        args.exc_traceback))))
    except Exception:
        pass


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


def allow_lower_privilege_drop(widget):
    """
    尽量让「以管理员身份运行」时也能接收从资源管理器拖过来的文件。

    Windows 的完整性级别（UIPI）默认禁止把东西从普通权限的资源管理器拖进提了权的
    窗口：光标会变成一个禁止的圆圈，而且**事件根本不会送进程序**（所以程序里没法
    自己检测、也没法提示）。官方给的办法是给窗口放过这几条消息
    （WM_DROPFILES / WM_COPYDATA / WM_COPYGLOBALDATA）。

    实话说：能不能成取决于系统怎么发起这次拖拽 —— 走 OLE 那条路时 Windows 就是
    不放行，程序只能改用「选择文件」按钮 / 复制进曲库文件夹。真正想稳，就得让程序
    以**普通权限**启动（见 README「拖拽导入」一节）。
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(int(widget.winId()))
        if not hwnd:
            return False
        for message in (0x0233, 0x004A, 0x0049):   # WM_DROPFILES / COPYDATA / COPYGLOBALDATA
            try:
                user32.ChangeWindowMessageFilterEx(hwnd, ctypes.c_uint(message),
                                                   ctypes.c_uint(1), None)   # MSGFLT_ALLOW
            except Exception:
                pass
        return True
    except Exception:
        return False


def set_taskbar_window(widget, on=True):
    """
    把窗口临时变成「任务栏认得的那种普通窗口」（on=True），或者换回浮层（on=False）。

    最小化之前为什么非要这一步：浮层配方给窗口挂了 WS_EX_NOACTIVATE（点它不抢游戏
    焦点）。微软文档写得很明白 —— 带这个位的窗口**默认不进任务栏**。没有任务栏按钮，
    最小化动画就没有落点，Windows 会把窗口最后那幅画面丢在桌面上（实测：屏幕左边会
    留一条标题栏残影，点别的窗口都不一定擦得掉），任务栏里也找不到它 —— 用户点了
    最小化会觉得「程序没了」。

    所以最小化之前摘掉 NOACTIVATE、补上 APPWINDOW；还原的时候再换回去（浮层还是
    别在任务栏里占位）。两个位一次 SetWindowLongW 改完，中间不会闪一下。
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = ctypes.c_void_p(int(widget.winId()))
        if not hwnd:
            return False
        gwl_exstyle, ws_ex_noactivate, ws_ex_appwindow = -20, 0x08000000, 0x00040000
        user32.GetWindowLongW.restype = ctypes.c_long
        style = user32.GetWindowLongW(hwnd, gwl_exstyle)
        if on:
            style = (style | ws_ex_appwindow) & ~ws_ex_noactivate
        else:
            style = (style | ws_ex_noactivate) & ~ws_ex_appwindow
        user32.SetWindowLongW(hwnd, gwl_exstyle, style)
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
    # @BARH@ 是标题行控件的统一高度：QSS 里的 min-height / max-height 会盖掉
    # 代码里的 setFixedSize，两边必须写同一个值，不然按钮就会「32 宽 19 高」。
    # 另外 QSS 的 height 算的是**内容高度**，padding 和边框还要另外加：
    # 所以带 1px 边框的控件用 @BARHIN@（= BAR_H - 2），外高才正好是 BAR_H。
    text = STYLE.replace('@BARHIN@', str(BAR_H - 2)).replace('@BARH@', str(BAR_H))
    return theme.paint(text)


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


class InfoWindow(QWidget):
    """
    「公告 / 更新」的独立窗口。

    以前是贴在主窗口里的一块面板 —— 窗口一窄就把上面的卡片挤扁，而且「再点一次
    收起」还得用户自己发现。现在改成正常窗口（跟简谱编辑器一个规矩）：有标题栏、
    能最小化、不置顶。内容是标题 + 可滚动的正文 + 右下角几个按钮。

    注意：主窗口在游戏里是「不抢焦点」的浮层，而这是个**普通顶层窗口**，会正常
    抢焦点 —— 所以在游戏里点「公告」会把游戏顶回桌面。这是用户确认过的取舍：
    宁可顶一下，也不要一块挤在主界面里的面板。
    """

    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('root')
        self.setWindowTitle('%s — 公告 / 更新' % APP_TITLE)
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.WindowSystemMenuHint
                            | Qt.WindowType.WindowMinimizeButtonHint
                            | Qt.WindowType.WindowCloseButtonHint)
        self.resize(520, 460)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)
        card = QFrame()
        card.setObjectName('neuPanel')
        outer.addWidget(card)
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(10)
        self.caption = QLabel('')
        self.caption.setObjectName('panelTitle')
        box.addWidget(self.caption)
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.TextBrowserInteraction)
        box.addWidget(self.view, 1)
        self.foot = QHBoxLayout()
        self.foot.setSpacing(8)
        self.foot.addStretch(1)
        box.addLayout(self.foot)
        self._buttons = []

    def set_content(self, title, body, actions):
        """铺一遍内容：标题 + 正文 + 按钮（按钮是 (文字, 槽, 是不是主按钮)）。"""
        self.caption.setText(str(title))
        self.view.setPlainText(str(body))
        self._set_actions(actions)
        self.setWindowTitle('%s — %s' % (APP_TITLE, title))

    def set_markdown(self, title, body, actions=()):
        """跟 set_content 一样，只是正文按 Markdown 渲染（「快速上手」手册用它）。"""
        self.caption.setText(str(title))
        self.view.setMarkdown(str(body))
        self.view.verticalScrollBar().setValue(0)          # 每次从头看
        self._set_actions(actions)
        self.setWindowTitle('%s — %s' % (APP_TITLE, title))

    def _set_actions(self, actions):
        for button in self._buttons:            # 上一次那几个按钮清掉
            self.foot.removeWidget(button)
            button.deleteLater()
        self._buttons = []
        for text, slot, primary in (actions or ()):
            button = QPushButton(text)
            if primary:
                button.setObjectName('primary')
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            self.foot.addWidget(button)
            self._buttons.append(button)

    def set_body(self, body):
        """只换正文（按钮不动）—— 下载进度那种要反复刷的地方用它。"""
        self.view.setPlainText(str(body))

    def closeEvent(self, event):
        self.closed.emit()                      # 让主窗口把「现在开着哪一页」清掉
        super().closeEvent(event)


class RewardWindow(QWidget):
    """
    「打赏作者」小窗口：一句话 + 收款码图片。

    跟 InfoWindow 一个规矩 —— 普通顶层窗口（能最小化、能关、不置顶），不往主界面里
    塞面板。图片 pay.jpg 打包进 exe，离线也能看。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('root')
        self.setWindowTitle('%s — 打赏作者' % APP_TITLE)
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.WindowSystemMenuHint
                            | Qt.WindowType.WindowMinimizeButtonHint
                            | Qt.WindowType.WindowCloseButtonHint)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)
        card = QFrame()
        card.setObjectName('neuPanel')
        outer.addWidget(card)
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)

        caption = QLabel('未成年人请不要打赏')
        caption.setObjectName('panelTitle')
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(caption)

        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loaded = False
        path = asset_path('pay.jpg')
        if path:
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                shown = pixmap.scaled(320, 420, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation)
                self.picture.setPixmap(shown)
                self.picture.setFixedSize(shown.size())
                loaded = True
        if not loaded:
            self.picture.setText('（没找到收款码图片 pay.jpg）')
        box.addWidget(self.picture, 0, Qt.AlignmentFlag.AlignCenter)

        tip = QLabel('感谢支持，请量力而行')
        tip.setObjectName('panelMeta')
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(tip)

        foot = QHBoxLayout()
        foot.addStretch(1)
        close = QPushButton('关闭')
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close)
        foot.addWidget(close)
        box.addLayout(foot)


class RhythmResultWindow(QWidget):
    """
    音游结算窗口：分数 + 星星 + 各判定数量 + 名字 + 存本机 / 传联网 + 排行榜。

    普通顶层窗口（能最小化、能关、不置顶）。排行榜**按曲子分开** —— 曲子的身份就是
    文件名（国内曲库和 GitHub 曲库同名，两边能合起来比）。
    """

    uploaded = Signal(str, str)          # 上传完了：(网页地址或分数, 出错信息)
    online_ready = Signal(object, str)   # 联网成绩拉回来了：(列表, 出错信息)
    library_ready = Signal(object, str)  # 「这首在不在联网曲库里」查完了：(True/False/None, 说明)
    name_saved = Signal(str)             # 存过 / 传过了，主窗口拿它记住名字

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('root')
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.WindowSystemMenuHint
                            | Qt.WindowType.WindowMinimizeButtonHint
                            | Qt.WindowType.WindowCloseButtonHint)
        self.resize(430, 620)
        self.song = ''
        self.summary = {}
        self.online = []
        self._busy = False
        self._online_busy = False     # 联网榜单正在读（挡重复请求，别一点再点）
        self.library = None           # 这首歌在不在联网曲库里：True / False / None（还没查）
        self.library_why = ''
        self._library_busy = False
        self.uploaded.connect(self._on_uploaded)
        self.online_ready.connect(self._on_online)
        self.library_ready.connect(self._on_library)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(0)
        card = QFrame()
        card.setObjectName('neuPanel')
        outer.addWidget(card)
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)

        self.caption = QLabel('音游结算')
        self.caption.setObjectName('panelTitle')
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.caption)
        self.song_label = QLabel('')
        self.song_label.setObjectName('panelMeta')
        self.song_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.song_label.setWordWrap(True)
        box.addWidget(self.song_label)
        self.stars = QLabel()
        self.stars.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.stars)
        self.score = QLabel('')
        self.score.setObjectName('panelTitle')
        self.score.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.score)
        self.detail = QLabel('')
        self.detail.setObjectName('panelMeta')
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        box.addWidget(self.detail)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(QLabel('名字'))
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(rhythm.NAME_LIMIT)
        self.name_edit.setPlaceholderText('留个名字（最多 %d 个字）' % rhythm.NAME_LIMIT)
        name_row.addWidget(self.name_edit, 1)
        box.addLayout(name_row)

        # 这首歌在不在联网曲库里 —— 不在就不给上传（本地自己转的曲子只能本机排名）
        self.library_hint = QLabel('')
        self.library_hint.setObjectName('panelMeta')
        self.library_hint.setWordWrap(True)
        box.addWidget(self.library_hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.save_btn = QPushButton('存到本机')
        self.save_btn.clicked.connect(self.save_local)
        self.upload_btn = QPushButton('上传到联网记录')
        self.upload_btn.setObjectName('primary')
        self.upload_btn.clicked.connect(self.upload_online)
        self.reload_btn = QPushButton('刷新排行榜')
        self.reload_btn.clicked.connect(self.refresh_all)
        for button in (self.save_btn, self.upload_btn, self.reload_btn):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        box.addLayout(row)

        self.hint = QLabel('')
        self.hint.setObjectName('panelMeta')
        self.hint.setWordWrap(True)
        box.addWidget(self.hint)

        board = QLabel('排行榜（只跟同一首曲子比）')
        board.setObjectName('panelMeta')
        box.addWidget(board)
        self.board = QTextEdit()
        self.board.setReadOnly(True)
        self.board.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        box.addWidget(self.board, 1)

        foot = QHBoxLayout()
        foot.addStretch(1)
        close = QPushButton('关闭')
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close)
        foot.addWidget(close)
        box.addLayout(foot)

    # ---------- 内容 ----------

    def show_result(self, song, summary, name=''):
        """铺一遍这次的结果，顺手把本机 / 联网的排行榜拉出来。"""
        self.song = str(song or '未知曲子')
        self.summary = dict(summary or {})
        self.name_edit.clear()            # 名字不预填，每次自己写
        stars = int(self.summary.get('stars') or 0)
        rainbow = int(self.summary.get('rainbow') or 0)
        self.stars.setPixmap(self._stars_pixmap(stars, rainbow))
        self.score.setText('%d 分' % int(self.summary.get('score') or 0))
        self.song_label.setText(self.song)
        self.detail.setText(
            'PERFECT %d ・ GOOD %d ・ MISS %d（差一点 %d）・ WRONG %d ・ 最大连击 %d'
            % (int(self.summary.get('perfect') or 0), int(self.summary.get('good') or 0),
               int(self.summary.get('miss') or 0), int(self.summary.get('plain') or 0),
               int(self.summary.get('wrong') or 0), int(self.summary.get('max_combo') or 0)))
        self.save_btn.setEnabled(True)
        self.upload_btn.setEnabled(False)      # 确认在联网曲库里才放行（见 check_library）
        self.library = None
        self.library_why = ''
        self.library_hint.setText('正在确认这首歌在不在联网曲库里…')
        self.load_local()
        self.load_online()
        self.check_library()

    def _stars_pixmap(self, stars, rainbow, size=34):
        """几颗星画成一张图：前 rainbow 颗是炫彩的。"""
        gap = 8
        count = max(int(stars), 1)
        pixmap = QPixmap(count * size + (count - 1) * gap, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for index in range(int(stars)):
            star = judgeicons.star(size, rainbow=index < int(rainbow))
            painter.drawPixmap(QRectF(index * (size + gap), 0, size, size), star,
                               QRectF(star.rect()))
        painter.end()
        return pixmap

    def _entry(self):
        return rhythm.entry_of(self.summary, self.name_edit.text(), APP_VERSION)

    def _need_name(self):
        name = rhythm.clean_name(self.name_edit.text())
        if not name:
            self.hint.setText('先在上面填个名字吧 —— 排行榜要按名字排。')
            return ''
        return name

    def save_local(self):
        """存到本机记录（一首歌留最高分的前 50 条）。"""
        if not self.summary:
            return
        if not self._need_name():
            return
        self.hint.setText('')
        items = rhythm.save_local(self.song, self._entry())
        self.name_saved.emit(self.name_edit.text())
        self.board.setPlainText(self._board_text(items, self.online))
        self.hint.setText('已经存到本机了。')

    def upload_online(self):
        """传到曲库仓库（一条成绩一个文件，不会跟别人撞车）。"""
        if not self.summary or self._busy:
            return
        if self.library is not True:
            self.library_hint.setText(
                '这首歌不在联网曲库里，先不能上线上排名。\n'
                '想上榜：打开「联网曲库」→「上传 / 整理曲库…」把这首传上去，再回来点'
                '「刷新排行榜」。本地自己转的 mp3 / 自己做的曲子只能存本机记录。')
            return
        if not self._need_name():
            return
        entry = self._entry()
        self._busy = True
        self.upload_btn.setEnabled(False)
        self.hint.setText('正在上传…')

        def work():
            url, why = rhythm.upload(self.song, entry)
            try:
                self.uploaded.emit(url or ('%d 分' % int(entry.get('score') or 0)), why)
            except RuntimeError:
                pass                      # 窗口已经关了

        threading.Thread(target=work, daemon=True).start()

    def _on_uploaded(self, url, why):
        self._busy = False
        self.upload_btn.setEnabled(self.library is True)
        if why:
            self.hint.setText('上传失败：%s\n（可以先「存到本机」，联网了再传）' % why)
            return
        self.hint.setText('已经传上去了（%s）—— 正在刷新排行榜…' % url)
        self.name_saved.emit(self.name_edit.text())
        self.load_online()

    def refresh_all(self):
        """「刷新排行榜」：联网成绩和「在不在曲库里」一起重查。"""
        self.check_library()
        self.load_online()

    def check_library(self):
        """后台问一次：这首歌在不在联网曲库里（不在就不给上传成绩）。"""
        if not self.song or self._library_busy:
            return
        self._library_busy = True
        song = self.song
        self.library_hint.setText('正在确认这首歌在不在联网曲库里…')

        def work():
            try:
                ok, why = rhythm.in_library(song)
            except Exception as exc:
                ok, why = None, str(exc)
            try:
                self.library_ready.emit(ok, why or '')
            except RuntimeError:
                pass                      # 窗口已经关了

        threading.Thread(target=work, daemon=True).start()

    def _on_library(self, ok, why):
        self._library_busy = False
        self.library = ok
        self.library_why = why or ''
        self.upload_btn.setEnabled(ok is True and not self._busy)
        if ok is True:
            self.library_hint.setText('这首在联网曲库里，成绩可以参加线上排名。')
        elif ok is False:
            self.library_hint.setText(
                '本地曲子（不在联网曲库里）：音游随便玩、本机记录照存，'
                '但不能上线上排名。\n想上榜：打开「联网曲库」→「上传 / 整理曲库…」'
                '把这首传上去。')
        else:
            self.library_hint.setText(
                '没能确认这首歌在不在联网曲库里（请检查网络连接）%s\n'
                '确认不了就先不能上传成绩；好了点「刷新排行榜」重试。'
                % ('：%s' % why if why else ''))

    def load_local(self):
        self.board.setPlainText(self._board_text(rhythm.load_local(self.song), self.online))

    def load_online(self):
        """联网排行榜：后台线程去读，读完用信号回来铺界面。"""
        if not self.song or self._online_busy:
            return
        self._online_busy = True
        self.reload_btn.setEnabled(False)
        self.hint.setText('正在读联网成绩…（连不上等几秒就好，不会一直试）')
        song = self.song

        def work():
            try:
                items, why = rhythm.fetch(song)
            except Exception as exc:
                items, why = [], '读联网成绩出错：%s' % exc
            try:
                self.online_ready.emit(items, why)
            except RuntimeError:
                pass                      # 窗口已经关了

        threading.Thread(target=work, daemon=True).start()

    def _on_online(self, items, why):
        self._online_busy = False
        self.reload_btn.setEnabled(True)
        if not why:
            self.online = list(items or [])
        self.board.setPlainText(self._board_text(rhythm.load_local(self.song), self.online))
        if why:
            self.hint.setText('没读到联网成绩 —— 请检查网络连接，'
                              '好了再点「刷新排行榜」重试。\n（本机记录不受影响）')
        elif not self.online:
            self.hint.setText('联网记录里这首歌还没人传过 —— 你可以是第一个。')
        else:
            self.hint.setText('联网成绩一共 %d 条。' % len(self.online))

    def _board_text(self, local, online):
        """排行榜文本：本机一段、联网一段。"""
        def block(title, items):
            lines = [title, '-' * 40]
            if not items:
                lines.append('（还没有记录）')
            for index, item in enumerate(items[:20], 1):
                lines.append('%2d. %-10s %6d 分  ★%-3s %s'
                             % (index, str(item.get('name') or '?')[:10],
                                int(item.get('score') or 0),
                                str(item.get('stars') or 0),
                                str(item.get('time') or '')[5:16]))
            return lines
        lines = block('本机记录（%d 条）' % len(local), local)
        lines.append('')
        lines += block('联网记录（%d 条）' % len(online), online)
        return '\n'.join(lines)


class SearchDelegate(QStyledItemDelegate):
    """
    曲库列表的搜索高亮。

    命中关键字的**那一段**换个底色 + 深色字（选中 / 悬停那套底色照旧由 Qt 画，
    只把文字抠出来自己重画）。关键字默认按「包含」匹配，勾上「正则」就按正则来。
    """

    HIGHLIGHT = QColor('#ffd166')          # 命中段的底色
    HIGHLIGHT_TEXT = QColor('#4a2f00')     # 命中段的字色

    def __init__(self, parent=None):
        super().__init__(parent)
        self.term = ''
        self.use_regex = False
        self._cache = {}

    def set_term(self, term, use_regex=False):
        self.term = str(term or '')
        self.use_regex = bool(use_regex)
        self._cache = {}

    def matches(self, text):
        """这段文字里命中的区间 [(起, 止), ...]；没命中就是空表。"""
        text = str(text or '')
        if not self.term:
            return []
        if text in self._cache:
            return self._cache[text]
        out = []
        try:
            if self.use_regex:
                for match in re.finditer(self.term, text, re.IGNORECASE):
                    if match.end() > match.start():
                        out.append((match.start(), match.end()))
                    if len(out) >= 100:
                        break
            else:
                low = text.lower()
                term = self.term.lower()
                start = 0
                while len(out) < 100:
                    at = low.find(term, start)
                    if at < 0:
                        break
                    out.append((at, at + len(term)))
                    start = at + max(1, len(term))
        except re.error:
            out = []                       # 正则写了一半（还没写完）就当没命中
        self._cache[text] = out
        return out

    def paint(self, painter, option, index):
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or '')
        hits = self.matches(text)
        if not hits:
            super().paint(painter, option, index)
            return
        # 先把背景 / 选中 / 悬停那套原样画了（文字留空），再自己按命中段画文字
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ''
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        if rect.width() <= 4:
            return
        painter.save()
        painter.setClipRect(rect)
        painter.setFont(opt.font)
        metrics = painter.fontMetrics()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        base = opt.palette.color(QPalette.ColorRole.HighlightedText if selected
                                 else QPalette.ColorRole.Text)
        line = metrics.height()
        top = rect.top() + max(0, (rect.height() - line) // 2)
        y = top + metrics.ascent()
        x = rect.left()
        pos = 0
        for start, end in hits:
            if start > pos:
                chunk = text[pos:start]
                painter.setPen(base)
                painter.drawText(x, y, chunk)
                x += metrics.horizontalAdvance(chunk)
            chunk = text[start:end]
            width = metrics.horizontalAdvance(chunk)
            if selected:
                painter.setPen(base)
            else:
                painter.fillRect(QRectF(x - 1, top, width + 2, line), self.HIGHLIGHT)
                painter.setPen(self.HIGHLIGHT_TEXT)
            painter.drawText(x, y, chunk)
            x += width
            pos = end
        if pos < len(text):
            painter.setPen(base)
            painter.drawText(x, y, text[pos:])
        painter.restore()


def plan_patches(plan):
    """更新计划里要依次盖上去的差分包列表（只有单个 patch 的老清单也认）。"""
    items = [dict(item) for item in ((plan or {}).get('patches') or [])
             if isinstance(item, dict) and item.get('url')]
    if items:
        return items
    single = (plan or {}).get('patch') or {}
    return [dict(single)] if isinstance(single, dict) and single.get('url') else []


class MainWindow(QWidget):
    message = Signal(str)          # 后台线程 -> 界面 的日志
    progress = Signal(int, int)    # 演出进度
    finished = Signal(str)         # 演奏结束：'done' 正常走完 / 'stop' 中途停了
    hotkey = Signal(str)           # 全局热键 -> 主线程动作
    audio_done = Signal(str, str)  # 音频转 MIDI 结束：(MIDI 路径, 出错信息)
    preview_ready = Signal(str)    # 试听合成结束：出错信息（空串 = 好了）
    diag_sound = Signal(str)       # 「谁在响」听到一声：一句话
    diag_done = Signal(str)        # 「谁在响」听完了：总结
    online_done = Signal(str, str) # 联网曲库下载完了：(本地路径, 出错信息)；空路径 = 失败
    online_index = Signal(int, object, str)  # 联网曲库的歌单拉回来了：(第几次, 歌单, 出错信息)
    news_index = Signal(object, object)      # 公告 / 版本信息拉回来了：(版本信息, 公告表)
    update_index = Signal(object, str)       # 增量更新清单拉回来了：(清单, 出错信息)
    update_plan = Signal(object)             # 更新计划算好了：dict（见 update.plan）
    update_progress = Signal(int, int)       # 差分包下载进度：(下了多少, 一共多少)
    update_done = Signal(str, str)           # 更新包准备完了：(说明, 出错信息)
    manual_ready = Signal(str, str)          # 快速上手手册拉回来了：(正文, 提示)

    def __init__(self, initial=None, parent=None):
        super().__init__(parent)
        self.setObjectName('root')
        self.setWindowTitle('%s v%s' % (APP_TITLE, APP_VERSION))
        self._fit_window_size(force=True)          # 按屏幕大小定尺寸（见 window_size_for）
        self.setWindowIcon(make_icon())
        self.setAcceptDrops(True)          # 拖 midi / mp3 到窗口里导入：见 dropEvent
        _app = QApplication.instance()
        if _app is not None:
            _app.installEventFilter(self)  # 松手落在列表 / 输入框上也照样算数

        self.settings = QSettings('AutoPlay', 'AutoPlay')
        self.player = player.Player(log=self.message.emit)
        self.analysis = None
        self.score_path = None
        self.score_events = []
        self.worker = None
        self.tray = None
        self.hotkeys = None
        self.hotkey_timer = None
        self._reward_window = None         # 「打赏作者」那个小窗口（懒创建）
        self._audio_queue = []             # 一次拖进来好几段音频时排队转
        self._audio_out = None             # 拖进来的音频转出来的 midi 存哪儿
        self._keyboard_borrowed = False    # 曲库那一页开着的时候临时借了键盘（见 _borrow_keyboard）
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
        self.rhythm_run = False        # 正在音游模式（同样不走演奏器，多了判定计分）
        self._rhythm_result = None     # 上一次音游的结算（见 rhythm.Session.summary）
        self._rhythm_shown = False     # 这一次结算窗口开过没有（防每帧重开）
        self._result_window = None     # 音游结算窗口（懒创建）
        self._manual_window = None     # 「快速上手」手册窗口（懒创建）
        self._manual_body = ''         # 这次拉到的（或内置的）手册正文
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
        self.pick_status = None        # 同一块浮层面板：联网曲库那一页的状态行
        self._pick_mode = 'file'       # 面板里现在贴的是「选文件」还是「联网曲库」
        self._fitted = False           # 第一次露头时按内容量过一次高度没
        self.log_action = None         # 托盘里「运行日志（控制台）」那一项（没托盘时是 None）
        self.tray = None               # 托盘图标本身（系统托盘不可用时是 None）
        self.hotkey_actions = {}       # 托盘菜单里的键位项（同上）
        self.overlay_active = False    # 现在是不是「浮层形态」（贴着游戏的那种窗口）
        self._maximized = False        # 主窗口现在最大化了没（编辑工程时铺满屏幕用）
        self._ghost_rect = None        # 最小化前窗口占的那块屏幕：最小化完要擦一下，见 _erase_ghost
        self._overlay_hidden_by_minimize = False   # 最小化的时候是不是顺手把进度浮窗收起来了
        self._minimized_visual_done = False        # 这次最小化的收尾做过了没（见 _enter_minimized_visual_state）
        self._overlay_suspended_for_minimize = False   # 为了最小化，临时把浮层形态切成普通窗口了没
        self._minimize_busy = False                # 正在为最小化换形态（这时别理窗口状态事件）
        self._summoned_in_game = False # 这次是 Ctrl+F1 从游戏里唤起来的（决定用哪种选文件）
        self._game_hwnd = 0            # 唤起前占着全屏的那个窗口（游戏）：抢了前台要还给它
        self._watch_left = 0           # 前台兜底还能出手几次
        self._last_in_game = None      # 上一次判断「在不在游戏里」的结果（变了就要跟界面同步）
        self.desk_tips = {}            # 浮层形态下会被临时改文案的按钮，记着原本的提示
        self._converting = False       # 正在把音频转成 MIDI
        self._downloading = False      # 正在从联网曲库下曲子（后台线程里下）
        # 公告 / 版本信息：启动后台拉一次（拉不到不影响任何功能），拉到才更新界面
        self.remote_version = {}       # version.json 的内容
        self.remote_notices = []       # notice.json 里的公告
        self.remote_update = {}        # update.json 的内容（增量更新清单）
        self._update_why = ''          # 上面那份清单是拉到的还是没拉到
        self._update_plan = None       # 算好的更新计划（update.plan 的返回值）
        self._update_hop = (1, 1, '', '')  # 多跳更新：现在下的是第几个包
        self._update_plan_ver = ''     # 这个计划是针对哪个版本的
        self._update_busy = False      # 正在准备 / 下载更新（防重入）
        self._update_body = ''         # 更新窗口里那段正文（下载进度在它上面刷）
        self._update_header_lines = [] # 上面那段「当前版本 / 最新版本 / 更新说明」
        self._update_tail = []         # 正文里跟着状态变的那几行
        self._update_buttons_now = []  # 当前这场面摆的是哪几个按钮
        self._news_started = False     # 只拉一次（手动「重新检查」会再拉）
        self._news_tried = 0           # 拉过几次（日志里说清楚）
        self._notices_read = set()     # 已经看过的公告 id（存在设置里）
        self._update_seen = ''         # 已经「打过招呼」的新版本号（更新按钮的红点看它）
        self.editor = None             # 「主」编辑器窗口（第一次点「简谱编辑器…」才建）
        self._building_editor = False  # 正在建编辑器（防止标签页来回切时重入）
        self.editor_windows = []       # 另外开出来的编辑器窗口（外部双击工程文件时用）
        # 试听：合成走后台线程，播放交给 preview.Preview（winsound 异步放）
        self.previewer = preview.Preview(log=self.message.emit)
        self._previewing = False       # 正在试听（包括还在合成的那一小会儿）
        self._preview_path = None      # 这次试听用的 wav
        self._preview_t0 = None        # 从哪一刻开始放的（自己数秒算进度）
        self._preview_base = 0.0       # 这一遍是从第几秒开始放的（拨过进度条就不是 0 了）
        self._preview_total = 0.0      # 试听总长（秒）
        self._preview_seek = 0.0       # 合成完从第几秒开始放（换速度重合成时接着放）
        self._preview_speed = 1.0      # 这次试听用的倍速（换速度时按它换算位置）
        self._preview_token = 0        # 合成序号：换速度重合成时，旧的合成结果作废
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
        self._repeat_level = REPEAT_LEVEL_DEFAULT   # 转谱时同音重复切多细（滑块的档位）
        self._audio_source = None                   # 上次转谱用的音频（「应用」拿它重转）
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
        self.online_done.connect(self._on_online_downloaded)
        self.online_index.connect(self._on_online_index)
        self.news_index.connect(self._on_news_index)
        self.update_index.connect(self._on_update_index)
        self.update_plan.connect(self._on_update_plan)
        self.update_progress.connect(self._on_update_progress)
        self.update_done.connect(self._on_update_done)
        self.manual_ready.connect(self._on_manual_ready)
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
        # 公告 / 版本信息：等界面先露头，再悄悄去拉（拉不到就安静地算了）
        # 启动第一件事就是它：拉到就知道有没有新公告 / 新版本，按钮上点红点提醒
        QTimer.singleShot(0, self.fetch_news)

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
        # 副标题是这一行里最长的文字：默认它会把「最小宽度」顶得很宽，窗口一窄
        # 就把右边的「已就绪 / 主题 / 键位…」挤出去、甚至压在一起（1.0.7 修的）。
        # 设成 Ignored：宽度不够时让这句先被裁掉，而不是把整行顶宽 —— 完整文字
        # 放进 tooltip，鼠标停上去还能看全。
        subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        subtitle.setToolTip('选一个 midi，点开始演奏，然后切回游戏')
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
        self.status.setFixedHeight(BAR_H)      # 跟这一行其它控件一样高
        # 标题行右侧分两组：左边「已就绪 + 主题」是圆角矩形，右边「键位 / 最小化 /
        # 最大化 / 关闭」是直角方块。两组之间空开一点，一眼看得出是两拨东西。
        head.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)

        # 配色主题：主界面 / 右上角浮窗 / 跟奏 / 编辑器一起换（主题是 json，见 theme.py）
        # 下拉框最后一条是「线上主题」占位（预埋：现在是灰的，点不了）。
        self.theme_combo = InlineCombo()
        self.theme_combo.setFixedSize(104, BAR_H)
        self.theme_combo.setToolTip('配色主题：主界面、右上角进度浮窗、跟奏面板、编辑器一起换。\n'
                                    '主题是 json，放在：\n%s\n'
                                    '改完在托盘图标右键 →「配色主题」→「重新载入主题文件」。\n'
                                    '（最后那条「线上主题」是以后从曲库仓库拉的，现在还没上线。）'
                                    % theme_folder())
        self.theme_combo.currentTextChanged.connect(self.on_theme_changed)
        head.addWidget(self.theme_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addSpacing(12)

        # 快捷键设置入口：一个小方块（原来在按钮行最右边，那一行被它撑到 640px 宽，
        # 窗口一窄就把「开始演奏 / 停止」的字裁掉）。说明在 tooltip 里。
        self.hotkey_btn = QPushButton('⌨')
        self.hotkey_btn.setObjectName('iconButton')
        self.hotkey_btn.setFixedSize(BAR_H, BAR_H)
        self.hotkey_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hotkey_btn.setToolTip('快捷键设置：点某一行的按键，再直接按下想用的组合键就录进去了\n'
                                   '（默认 F6 / F7 / F8 / F10 / Ctrl+F1 / Ctrl+F2；托盘图标右键里也能开）')
        self.hotkey_btn.clicked.connect(self.open_hotkey_dialog)
        head.addWidget(self.hotkey_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        # 最小化：正常的窗口最小化（任务栏里看得见）
        self.min_button = QPushButton('—')
        self.min_button.setObjectName('minButton')
        self.min_button.setFixedSize(BAR_H, BAR_H)
        self.min_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.min_button.setToolTip('最小化（任务栏里还看得见）')
        self.min_button.clicked.connect(self.minimize_window)
        head.addWidget(self.min_button, 0, Qt.AlignmentFlag.AlignVCenter)

        # 最大化 / 还原：无边框窗口没有标题栏，自己给一个（也可以按 F11）
        self.max_button = QPushButton('□')
        self.max_button.setObjectName('maxButton')
        self.max_button.setFixedSize(BAR_H, BAR_H)
        self.max_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.max_button.setToolTip('最大化 / 还原（也可以按 F11）')
        self.max_button.clicked.connect(self.toggle_maximize)
        head.addWidget(self.max_button, 0, Qt.AlignmentFlag.AlignVCenter)

        # 关闭：收进托盘后台 —— 窗口藏起来（任务栏不占位），演奏 / 热键照常跑。
        # 想再打开点托盘图标或者按 Ctrl+F1；真要退出走托盘右键「退出」。
        self.cover_close = QPushButton('×')
        self.cover_close.setObjectName('coverClose')
        self.cover_close.setFixedSize(BAR_H, BAR_H)
        self.cover_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover_close.setToolTip('关闭：收进托盘后台（演奏 / 热键照常，点托盘图标能叫回来）')
        self.cover_close.clicked.connect(self.hide_cover)
        head.addWidget(self.cover_close, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(head)

        # 动作条（标题行下面这一行）：公告 / 更新 / B站主页 / GitHub。
        # 四个按钮都是「图标 + 文字」，图标是 uiicons.py 用代码画的（跟主题一起换色）。
        # 单独占一行，窗口再窄也不会跟标题、状态胶囊抢地方（不重叠）。
        strip = QHBoxLayout()
        strip.setSpacing(8)
        self.notice_btn = self._neu_button(
            'announce', '公告', self.open_notice,
            '看公告：内容放在曲库仓库的 notice.json 里，程序启动时联网拉一次。\n'
            '有新公告没看过的，按钮上的小铃铛会点一个红点。')
        self.update_btn = self._neu_button(
            'update', '更新', self.open_update,
            '检查更新：跟曲库仓库里的 version.json 比一下版本号。\n'
            '已经是最新就说一句「已经是最新版本」；有新版本可以点「去下载」用浏览器打开下载页（B站主页）。')
        self.bili_btn = self._neu_button(
            'bilibili', 'B站主页', lambda: self.open_link('bilibili'),
            '用系统默认浏览器打开 B 站主页：https://space.bilibili.com/341688158\n'
            '（游戏里点会把游戏切到后台 —— 回桌面再看更稳。）')
        self.github_btn = self._neu_button(
            'github', 'GitHub', lambda: self.open_link('github'),
            '用系统默认浏览器打开 GitHub 仓库：\n'
            'https://github.com/xXjuanneysXx/Auto-Midi-Player')
        self.reward_btn = self._neu_button(
            'reward', '打赏作者', self.open_reward,
            '觉得好用就打赏作者一杯奶茶 —— 请量力而行，未成年人请不要打赏。\n'
            '点开是一个小窗口，里面有收款码。')
        self.manual_btn = self._neu_button(
            'quickstart', '快速上手', self.open_manual,
            '第一次用先看这个：怎么选曲、怎么开弹、跟奏和音游怎么玩、出错了怎么看。\n'
            '内容放在曲库仓库里（联网拉最新的），拉不到就用程序自带的那份。')
        for button in (self.notice_btn, self.update_btn, self.bili_btn, self.github_btn,
                       self.reward_btn, self.manual_btn):
            strip.addWidget(button, 0)
        strip.addStretch(1)
        self.version_tag = QLabel('v%s' % APP_VERSION)
        self.version_tag.setObjectName('versionTag')
        self.version_tag.setFixedHeight(BAR_H)
        self.version_tag.setToolTip('当前版本 v%s' % APP_VERSION)
        strip.addWidget(self.version_tag, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(strip)
        self.set_status('就绪', 'idle')

        card = QFrame()
        card.setObjectName('card')
        body = QVBoxLayout(card)
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(10)

        pick = QHBoxLayout()
        pick.setSpacing(8)          # 这一行有 5 个按钮，紧一点免得把字挤掉
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
                                  '加上联网曲库下载过的 —— 都在浮层列表里，双击就读进来。\n'
                                  '上面有「内置曲库」「已下载」两个快捷入口。'
                                  % SONG_DIR)
        self.btn_songs.clicked.connect(self.open_library)
        self.btn_online = QPushButton('联网曲库')
        self.btn_online.setToolTip('共享曲库（跟上面那个「曲库」是两回事）：曲子放在 Gitee / GitHub\n'
                                   '仓库里（默认国内 Gitee），程序读一个索引就知道有哪些。\n'
                                   '歌单直接贴在浮层里，一个窗口都不弹 —— 在游戏里点也能用；\n'
                                   '挑一首双击（或点「下载并载入」）下到本地缓存里直接读进来，\n'
                                   '连不上能一键换另一套。断网不影响本地曲库。')
        self.btn_online.clicked.connect(self.open_online_library)
        self.btn_edit = None
        if edition.has_editor():         # 精简版不带编辑器
            self.btn_edit = QPushButton('简谱编辑器…')
            self.btn_edit.setToolTip('另开一个编辑器窗口（跟主界面分开的普通窗口，能最小化、不置顶），\n'
                                     '把当前这首铺成钢琴卷帘手动改：双击加音、右键删音、拖着改长短，\n'
                                     '空格播放 / 暂停，Ctrl+Z 撤销、Ctrl+S 保存工程，改完导出 midi 会自动载回来')
            self.btn_edit.clicked.connect(self.open_editor)
        self.file_label = QLabel('还没有选择文件')
        self.file_label.setObjectName('value')
        self.file_label.setWordWrap(True)
        self.file_label.setMinimumWidth(70)
        pick.addWidget(self.btn_choose, 0)
        for button in (self.btn_audio, self.btn_songs, self.btn_online, self.btn_edit):
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
        self.speed.currentTextChanged.connect(self.on_speed_changed)   # 试听跟着一起换
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
            self.follow_pace.setToolTip('「等我按对」是练习模式：程序不发按键，音符停在判定线上，\n'
                                        '琴键和鼠标组合都按对了才继续走 —— 新手不用被原曲速度拖着跑。\n'
                                        '「原速跟奏」是按原曲的速度自动走，只看提示。\n'
                                        '「音游模式」是原速下落 + 判定计分：321 倒计时之后全靠你自己弹，\n'
                                        '漏了算 miss、结算给星星（详见「快速上手」）。')
            self.follow_pace.currentTextChanged.connect(self.on_follow_pace)
            opts.addWidget(self.follow_pace)
        opts.addStretch(1)
        body.addLayout(opts)

        # 「同音重复」敏感度单独占一行：跟奏那几个控件也在上面那一行，挤一起会被压扁。
        # 只跟转谱有关，精简版没有转谱功能，这一行就不露出来。滑块是分档的（0 档 = 标准
        # 参数），右边的「应用」拿当前档位把那一段音频重转一遍。
        self.repeat_slider = None
        self.repeat_value = None
        self.repeat_apply = None
        if edition.has_audio() and getattr(audio2midi, 'REPEAT_LEVELS', None):
            repeat_row = QHBoxLayout()
            repeat_row.setSpacing(8)
            repeat_row.addWidget(QLabel('同音重复'))
            self.repeat_slider = QSlider(Qt.Horizontal)
            self.repeat_slider.setRange(0, len(audio2midi.REPEAT_LEVELS) - 1)
            self.repeat_slider.setSingleStep(1)
            self.repeat_slider.setPageStep(1)
            self.repeat_slider.setTickPosition(QSlider.TicksBelow)
            self.repeat_slider.setTickInterval(1)       # 一档一格，拖不出档外的值
            self.repeat_slider.setFixedWidth(96)
            self.repeat_slider.setToolTip(
                '转谱（音频转 MIDI）时，连着弹好几下同一个音要多容易被切成好几个音：\n'
                '越往右越容易切开（标准 → 稍敏感 → 中等 → 较敏感 → 最敏感）。\n'
                '改完点右边的「应用」，程序会自动用新档位把刚才那段音频重转一遍。\n'
                '代价：颤音 / 抖音多的曲子可能被切得偏碎（命令行上还能自己微调，见\n'
                'mp3midi/README.md 里的 --split-min）。')
            self.repeat_slider.valueChanged.connect(self.on_repeat_level)
            repeat_row.addWidget(self.repeat_slider)
            self.repeat_value = QLabel()
            self.repeat_value.setMinimumWidth(48)
            repeat_row.addWidget(self.repeat_value)
            self.repeat_apply = QPushButton('应用')
            self.repeat_apply.setToolTip('用现在的敏感度，把刚才那段音频重转一遍')
            self.repeat_apply.clicked.connect(self.on_repeat_apply)
            self.repeat_apply.setEnabled(False)     # 还没转过音频，没什么可「应用」的
            repeat_row.addWidget(self.repeat_apply)
            repeat_row.addStretch(1)
            self._refresh_repeat_label()
            body.addLayout(repeat_row)

        # 再一行：跟「录制」有关的开关 + 音长吸附。
        # 这一行原来把「录制 / 关联 / 谁在响 / 显示演奏状态 / 出错自动上报」全塞在
        # 一起，七八个控件挤在一行里，窗口一窄就互相压住、字被裁掉（1.0.7 修的
        # 「最下面一行太挤」）。现在拆成两行：这一行只放录制相关的，
        # 下面 opts3 放界面 / 工具类的开关。
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
        if opts2.count():
            opts2.addStretch(1)
            body.addLayout(opts2)

        # 界面 / 工具类的那几个开关单独一行，别跟录制那行挤
        opts3 = QHBoxLayout()
        opts3.setSpacing(8)
        self.assoc_box = None
        # 关联要写注册表指向「本 exe」；源码运行时没有 exe 可指，这个勾选框就不露出来
        if edition.has_editor() and fileassoc.exe_of():
            self.assoc_box = QCheckBox('双击 .mproj 工程文件用本程序打开')
            self.assoc_box.setToolTip('把 .mproj（简谱工程文件）关联到本程序：以后在资源管理器里双击它，\n'
                                      '程序会直接带着这个工程进「简谱编辑器」，不用先开程序再打开。\n'
                                      '只写在当前用户（HKCU）里，不需要管理员权限；取消勾选就撤掉。')
            self.assoc_box.toggled.connect(self.on_assoc_toggled)
            opts3.addWidget(self.assoc_box)
        self.diag_button = QPushButton('🔔 谁在响')
        self.diag_button.setToolTip('录制时蹦出一声提示音，不知道谁干的？点它，然后正常按你的键：\n'
                                    '接下来 %d 秒里只要有程序出声，就把它的名字记到下面日志里。\n'
                                    '（再点一次就提前收工）' % int(DIAG_SECONDS))
        self.diag_button.clicked.connect(self.on_diag)
        opts3.addWidget(self.diag_button)
        # 「显示演奏状态」：**屏幕右上角那个演奏进度浮窗**（「已就绪 / 演奏中 12/345」）。
        # 默认显示；调试的时候嫌它挡视线，取消勾选就整个不弹出来（演奏、热键照常）。
        # 托盘菜单里的「显示 / 隐藏进度浮窗」跟这是同一个开关。
        self.overlay_box = QCheckBox('显示演奏状态')
        self.overlay_box.setToolTip('勾上（默认）：演奏时屏幕右上角显示「已就绪 / 演奏中 12/345」那个进度浮窗。\n'
                                    '调试时嫌它挡视线就取消勾选，整个浮窗不再弹出来（演奏照常）。\n'
                                    '跟托盘菜单里的「显示 / 隐藏进度浮窗」是同一个开关。')
        self.overlay_box.setChecked(self.overlay_on)
        self.overlay_box.toggled.connect(self.on_overlay_toggled)
        opts3.addWidget(self.overlay_box)
        # 「出错自动上报」：默认开。关掉之后出错只写本机日志，不往曲库仓库传。
        self.error_report_box = QCheckBox('出错自动上报')
        self.error_report_box.setToolTip(
            '程序没接住的异常，自动打包成一份 json 传到曲库仓库的「错误报告」目录，\n'
            '方便作者修 bug。\n'
            '只传：版本、系统信息、异常类型和调用栈、路径里有没有中文。\n'
            '不传：用户名、完整路径、歌名这些能定位到个人的东西（路径会被替换成 <path>）。\n'
            '一次运行最多传 3 条；不想传就取消勾选（本机日志照旧写）。')
        self.error_report_box.setChecked(errorreport.enabled())
        self.error_report_box.toggled.connect(self.on_error_report_toggled)
        opts3.addWidget(self.error_report_box)
        opts3.addStretch(1)
        body.addLayout(opts3)

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

        # 公告 / 更新：改成**独立窗口**（见 InfoWindow）—— 贴在主窗口里会把上面的卡片
        # 挤小。窗口按需创建、存在 self.info_window 里，重复点就复用同一个。
        self.info_window = None
        self._info_kind = ''            # 这个窗口现在装的是「公告」还是「更新」

        self.hint = QLabel('')
        self.hint.setObjectName('hint')
        self.hint.setWordWrap(True)      # 键位提示会随自定义变长，换行别把窗口撑宽
        root.addWidget(self.hint)

        # 兜一道宽度：窗口**最小宽度**不许小于「任何一行真正需要的宽度」，免得
        # 被拖窄之后控件互相压住、文字被裁（1.0.7 修的「底部一行挤在一起」就是
        # 这个毛病）。按当前字体的真实 measure 算 —— 高分屏、系统缩放 125/150%
        # 的时候自动跟着变宽。上限 880：再宽就太占屏幕了，剩下的交给控件自己收缩。
        need = root.minimumSize().width() + 6
        self.setMinimumWidth(int(max(700, min(need, 880))))

        # 编辑器改成独立窗口了（见 open_editor）：点「简谱编辑器…」直接开一个新窗口，
        # 跟主界面互不打扰、能最小化、不置顶。所以主界面不再有那一页，标签栏只剩
        # 「演奏」一条，孤零零挂着不好看，藏掉。
        self.tabs.tabBar().setVisible(False)

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

    # ---------- 公告 / 版本 / 外链（顶部动作条那几个按钮）----------

    def _icon_color(self):
        """图标用什么颜色画（跟标题行小圆钮的文字色一致，换主题跟着换）。"""
        return theme.c('#b9c1d1')

    def _icon_bg(self):
        """图标底下那块面是什么颜色（画眼睛 / 鼻子用，看着像挖空）。"""
        return theme.c('#1b2231')

    def _set_button_icon(self, button, icon_name, dot=False):
        try:
            button.setIcon(uiicons.make_icon(icon_name, self._icon_color(), 17,
                                             dot=dot, bg=self._icon_bg()))
        except Exception as error:                 # 图标画不出来就只留文字，别把界面搞崩
            log_event('图标画不出来：%s' % error)

    def _neu_button(self, icon_name, text, slot, tip):
        """动作条上那种「图标 + 文字」的方按钮（新拟态那套样式见 QPushButton#neuIcon）。"""
        button = QPushButton(text)
        button.setObjectName('neuIcon')
        button.setFixedHeight(BAR_H)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(17, 17))
        button.setToolTip(tip)
        button._base_tip = tip            # 记着原提示：有新版本时要在前面加一句（见 _refresh_icons）
        button.clicked.connect(slot)
        self._set_button_icon(button, icon_name)
        return button

    def _refresh_icons(self):
        """换主题 / 拉到新公告：把动作条上的图标重画一遍（颜色和红点都要跟上）。"""
        for button, icon_name in ((getattr(self, 'notice_btn', None), 'announce'),
                                  (getattr(self, 'update_btn', None), 'update'),
                                  (getattr(self, 'bili_btn', None), 'bilibili'),
                                  (getattr(self, 'github_btn', None), 'github'),
                                  (getattr(self, 'reward_btn', None), 'reward'),
                                  (getattr(self, 'manual_btn', None), 'quickstart')):
            if button is None:
                continue
            if icon_name == 'announce':
                dot = self._has_unread_notice()
            elif icon_name == 'update':
                dot = self._has_update_notice()
                base = str(getattr(button, '_base_tip', '') or '')
                latest = str((self.remote_version or {}).get('latest') or '')
                button.setToolTip(('有新版本 v%s 可以更新。\n%s' % (latest, base)) if dot else base)
            else:
                dot = False
            self._set_button_icon(button, icon_name, dot=dot)

    def _has_unread_notice(self):
        for item in (self.remote_notices or []):
            nid = str(item.get('id') or '')
            if nid and nid not in self._notices_read:
                return True
        return False

    def _has_update_notice(self):
        """「更新」按钮上要不要点红点：仓库说有新版，而且这个版本号还没跟用户打过招呼。

        拉到 version.json / update.json 之后就知道了最新版是多少（启动时后台拉，见
        fetch_news），不用用户去点「更新」才发现。点开更新窗口就算「打过招呼」，
        红点收掉 —— 跟公告「看过就不再提醒」是一个道理，免得红点一直挂着像坏了。
        """
        latest = str((self.remote_version or {}).get('latest') or '')
        if not latest or not notice_mod.is_newer(latest, APP_VERSION):
            return False
        return latest != str(getattr(self, '_update_seen', '') or '')

    def _mark_update_seen(self, latest):
        """这个新版本号已经跟用户打过招呼了（记在设置里），顺手把红点收掉。"""
        latest = str(latest or '')
        if not latest or latest == str(getattr(self, '_update_seen', '') or ''):
            return
        self._update_seen = latest
        try:
            self.settings.setValue('update_seen', latest)
        except Exception:
            pass
        self._refresh_icons()

    def _load_notices_read(self):
        """哪些公告已经看过了（记在设置里，按公告的 id）。"""
        try:
            raw = str(self.settings.value('notice_read', '') or '')
        except Exception:
            raw = ''
        self._notices_read = {part for part in raw.split(',') if part}

    def _save_notices_read(self):
        try:
            self.settings.setValue('notice_read', ','.join(sorted(self._notices_read)))
        except Exception:
            pass

    def _load_update_seen(self):
        """上次已经「打过招呼」的新版本号（顶部「更新」按钮的红点靠它消掉）。"""
        try:
            self._update_seen = str(self.settings.value('update_seen', '') or '')
        except Exception:
            self._update_seen = ''

    def fetch_news(self):
        """去曲库仓库拉 version.json / notice.json（后台线程，几秒超时，失败静默）。"""
        if getattr(self, '_news_busy', False):
            return
        self._news_busy = True
        self._news_why = ('', '')
        threading.Thread(target=self._fetch_news_worker, daemon=True).start()

    def _fetch_news_worker(self):
        version, why_v = {}, ''
        items, why_n = [], ''
        try:                                      # 顺手把错误上报的配置拉一份（谁都能改）
            errorreport.refresh()
        except Exception:
            pass
        try:                                      # 音游成绩存哪个仓库也是可配的
            rhythm.refresh()
        except Exception:
            pass
        try:
            version, why_v = notice_mod.version_info()
        except Exception as error:
            why_v = str(error)
        try:
            items, why_n = notice_mod.notice_info()
        except Exception as error:
            why_n = str(error)
        self._news_why = (why_v, why_n)
        try:
            self.news_index.emit(version or {}, items or [])
        except RuntimeError:                      # 窗口已经关了
            pass
        try:                                      # 增量更新清单（version.json 说叫什么名）
            data, why_u = notice_mod.update_info(version=version or {})
        except Exception as error:
            data, why_u = {}, str(error)
        self._update_why = why_u
        try:
            self.update_index.emit(data or {}, why_u)
        except RuntimeError:                      # 窗口已经关了
            pass

    def _on_news_index(self, version, items):
        """后台把公告 / 版本信息拉回来了（在主线程里跑）。"""
        self._news_busy = False
        self._news_tried = getattr(self, '_news_tried', 0) + 1
        self.remote_version = dict(version or {})
        self.remote_notices = list(items or [])
        self._refresh_icons()
        self._refresh_version_tag()
        latest = str(self.remote_version.get('latest') or '')
        if latest:
            if notice_mod.is_newer(latest, APP_VERSION):
                self.log('版本检查：发现新版本 v%s（当前 v%s），点顶部「更新」看看'
                         % (latest, APP_VERSION))
            else:
                self.log('版本检查：已经是最新版本 v%s' % APP_VERSION)
        else:
            why_v, why_n = getattr(self, '_news_why', ('', ''))
            self.log('公告 / 版本信息没拉到：%s' % (why_v or why_n or '网络不通'))
        if self._info_visible():                  # 窗口正开着：换个新的内容进去
            if self._info_kind == 'notice':
                self.open_notice(force=True)
            elif self._info_kind == 'update':
                self.open_update(force=True)

    def _refresh_version_tag(self):
        """右下角那个版本标签：有新版本就写成「v1.0.1 → v1.0.2」并染成提醒色。"""
        latest = str(self.remote_version.get('latest') or '')
        newer = bool(latest) and notice_mod.is_newer(latest, APP_VERSION)
        if newer:
            text = 'v%s → v%s' % (APP_VERSION, latest)
        elif latest:
            text = 'v%s · 已是最新' % APP_VERSION
        else:
            text = 'v%s' % APP_VERSION
        self.version_tag.setText(text)
        self.version_tag.setToolTip(
            '当前版本 v%s\n最新版本 %s（来自曲库仓库的 version.json）\n点「更新」看详情'
            % (APP_VERSION, latest or '还没拉到'))
        self.version_tag.setProperty('stale', 'true' if newer else 'false')
        self.version_tag.style().unpolish(self.version_tag)
        self.version_tag.style().polish(self.version_tag)

    def open_link(self, key, url=None):
        """用系统默认浏览器打开一个链接（不内嵌浏览器）。"""
        links = notice_mod.links(self.remote_version)
        target = str(url or links.get(key) or '').strip()
        if not target:
            self.log('这个链接还没有地址（version.json 的 links 里没写）')
            return
        self.log('用浏览器打开：%s' % target)
        QDesktopServices.openUrl(QUrl(target))

    def open_reward(self):
        """「打赏作者」：开一个独立小窗口（能最小化、能关；主窗口置顶时它也跟着置顶）。"""
        window = self._reward_window
        if window is None:
            window = RewardWindow()
            self._reward_window = window
        self._present_popup(window)

    def _popup_on_top(self):
        """主窗口现在是「置顶浮层」吗？是的话弹窗也得跟着置顶，不然会被它压住。"""
        try:
            if getattr(self, 'overlay_active', False):
                return True
            return bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        except Exception:
            return False

    def _present_popup(self, window):
        """
        把独立小窗口（公告 / 更新 / 打赏）真的顶到最前面。

        主窗口默认就是「总在最前 + 不抢焦点」的浮层，普通弹窗只会被它压在下面 ——
        所以主窗口置顶时，弹窗也跟着置顶，并且走 force_window_front（AttachThreadInput
        那一套）抢前台；不然游戏在前面时弹窗压根冒不出来。
        """
        on_top = self._popup_on_top()
        try:
            window.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on_top)
        except Exception:
            pass
        window.show()                     # setWindowFlag 会把它藏一下，show 回来
        if on_top:
            force_window_front(window)    # 置顶 + 抢前台：游戏在前面也压得住
        else:
            window.raise_()
            window.activateWindow()
        window.raise_()

    def _show_info_panel(self, kind, title, body, actions):
        """公告 / 更新：开一个独立窗口（不贴在主界面里，免得把上面的卡片挤小）。"""
        if self._choosing is not None:            # 曲库那一页开着就先收掉，别叠在一起
            self._close_picker()
        window = getattr(self, 'info_window', None)
        if window is None:
            window = InfoWindow()                 # 顶层窗口：不挂在主窗口上，独立、不置顶
            window.closed.connect(self._on_info_closed)
            self.info_window = window
        window.set_content(title, body, actions)
        self._info_kind = kind
        self._present_popup(window)

    def _info_visible(self):
        """公告 / 更新那个窗口现在是不是开着。"""
        window = getattr(self, 'info_window', None)
        return bool(window is not None and window.isVisible())

    def _on_info_closed(self):
        """用户直接按窗口的 ✕ 关掉：把「现在开着哪一页」清掉。"""
        self._info_kind = ''

    def _hide_info_panel(self):
        window = getattr(self, 'info_window', None)
        if window is not None:
            window.hide()
        self._info_kind = ''

    def open_notice(self, force=False):
        """公告：点顶部「公告」把这页切出来（再点一次收起来）。"""
        if self._info_visible() and self._info_kind == 'notice' and not force:
            self._hide_info_panel()
            return
        items = list(self.remote_notices or [])
        blocks = []
        for item in items:
            title = str(item.get('title') or '公告').strip()
            date = str(item.get('date') or '').strip()
            body = str(item.get('body') or '').strip()
            blocks.append('%s%s\n%s' % (title, ('    %s' % date) if date else '', body))
        if blocks:
            text = '\n\n'.join(blocks)
            if getattr(self, '_news_why', ('', ''))[1]:   # 这次没连上：用的是上次拉到的那份
                text += ('\n\n（这次没连上曲库仓库：%s）\n'
                         '上面是上次拉到的公告，连上网再点「重新检查」。'
                         % self._news_why[1])
        elif getattr(self, '_news_tried', 0):
            why_v, why_n = getattr(self, '_news_why', ('', ''))
            text = ('现在拉不到公告。\n原因：%s\n\n联网之后点「重新检查」再试一次。'
                    % (why_n or why_v or '网络不通'))
        else:
            text = '正在拉公告…'
        for item in items:                        # 看过了：把铃铛上的红点去掉
            nid = str(item.get('id') or '')
            if nid:
                self._notices_read.add(nid)
        if items:
            self._save_notices_read()
            self._refresh_icons()
        self._show_info_panel('notice', '公告', text,
                              [('重新检查', self.fetch_news, False),
                               ('关闭', self._hide_info_panel, False)])

    def open_update(self, force=False):
        """
        更新：跟仓库里的 version.json 比版本号，一样就说「已经是最新版本」。

        有新版本的时候再看一眼「能不能增量」：曲库仓库里的 update.json 描述了
        每个文件该是什么样、以及从哪一版升到哪一版有差分包。能走差分包就把
        「立即更新」摆出来（只下变了的文件，几十 MB）；走不通（老版本没带更新器、
        差分包没传、文件对不上）就退回「去下载」完整安装包 —— 老规矩。
        """
        if self._info_visible() and self._info_kind == 'update' and not force:
            self._hide_info_panel()
            return
        if self._update_busy and force:
            return                            # 正在下更新包，别拿新内容把进度顶掉
        version = dict(self.remote_version or {})
        latest = str(version.get('latest') or '')
        newer = bool(latest) and notice_mod.is_newer(latest, APP_VERSION)
        if newer:
            self._mark_update_seen(latest)     # 打开展示过了：更新按钮上的红点收掉
        lines = ['当前版本：v%s' % APP_VERSION]
        if latest:
            lines.append('最新版本：v%s%s' % (latest, '        （有新版本！）' if newer else ''))
        published = str(version.get('published') or '').strip()
        if published:
            lines.append('发布时间：%s' % published)
        notes = str(version.get('notes') or '').strip()
        if notes:
            lines.append('')
            lines.append(notes)
        why_v, _why_n = getattr(self, '_news_why', ('', ''))
        if latest and why_v:                  # 这次没连上：用的是上次拉到的那份
            lines.append('')
            lines.append('（这次没连上曲库仓库：%s）' % why_v)
            lines.append('上面是上次拉到的版本信息，连上网再点「重新检查」。')
        self._update_header_lines = lines
        if not latest:
            if getattr(self, '_news_tried', 0):
                why_v, why_n = getattr(self, '_news_why', ('', ''))
                tail = ['拉不到版本信息：%s' % (why_v or why_n or '网络不通'),
                        '联网之后点「重新检查」再试一次。']
            else:
                tail = ['正在检查更新…']
            self._render_update(tail, self._update_buttons())
        elif not newer:
            self._render_update(['已经是最新版本，不用更新。'], self._update_buttons())
        elif not getattr(sys, 'frozen', False):
            self._render_update(['源码运行没法自己换文件 —— 点「去下载」拿完整安装包。'],
                                self._update_buttons('full'))
        elif self._update_plan is not None and self._update_plan_ver == latest:
            self._show_update_plan(self._update_plan)
        else:
            self._render_update(['正在看有没有增量更新包…'], self._update_buttons())
            self.start_update_plan()

    # ---------- 更新（增量 / 完整包） ----------

    def _update_buttons(self, mode='', plan=None):
        """更新窗口右下角那几个按钮（mode 是 'patch' / 'full' / ''）。"""
        actions = [('重新检查', self.fetch_news, False)]
        if mode == 'patch':
            actions.append(('立即更新', lambda: self.start_incremental_update(plan), True))
        elif mode == 'full':
            actions.append(('去下载', lambda: self.open_link('download_page'), True))
        actions.append(('关闭', self._hide_info_panel, False))
        return actions

    def _render_update(self, tail, actions):
        """把「开头那几行版本信息 + 当前状态」铺进窗口，并记住正文（下载进度刷它）。"""
        self._update_tail = list(tail) if isinstance(tail, (list, tuple)) else [str(tail)]
        self._update_buttons_now = list(actions)
        header = list(getattr(self, '_update_header_lines', []))
        self._update_body = '\n'.join(header + [''] + self._update_tail)
        self._show_info_panel('update', '更新', self._update_body, actions)

    def start_update_plan(self):
        """后台算一遍「我该走差分包还是完整包」（要逐文件核 sha256，别卡界面）。"""
        if self._update_busy:
            return
        self._update_busy = True
        threading.Thread(target=self._run_update_plan,
                         args=(dict(self.remote_update or {}),), daemon=True).start()

    def _run_update_plan(self, data):
        try:
            plan = update_mod.plan(APP_VERSION, app_dir(), data=data or None)
        except Exception as error:
            plan = {'mode': 'error', 'why': str(error)}
        try:
            self.update_plan.emit(plan)
        except RuntimeError:                  # 窗口已经关了
            pass

    def _on_update_index(self, data, why):
        """增量更新清单拉回来了（在主线程里跑）。"""
        self.remote_update = dict(data or {})
        self._update_why = why or ''
        latest = str(self.remote_update.get('latest') or '')
        if latest and notice_mod.is_newer(latest, str(self.remote_version.get('latest') or '')):
            # update.json 说还有更新的版本：以它为准（version.json 没跟上也不耽误更新）
            self.remote_version['latest'] = latest
            self._refresh_version_tag()
            self._refresh_icons()          # 更新按钮的红点跟着起来
        if self._info_visible() and self._info_kind == 'update' and not self._update_busy:
            self.open_update(force=True)

    def _on_update_plan(self, plan):
        """更新计划算好了（在主线程里跑）：把「立即更新」或者「去下载」摆出来。"""
        self._update_busy = False
        plan = dict(plan or {})
        self._update_plan = plan
        self._update_plan_ver = str(plan.get('latest') or '')
        if self._info_visible() and self._info_kind == 'update':
            self._show_update_plan(plan)

    def _show_update_plan(self, plan):
        mode = str(plan.get('mode') or '')
        latest = str(plan.get('latest') or self._update_plan_ver or '')
        if mode == 'patch':
            patches = plan_patches(plan)
            size = sum(float(patch.get('size') or 0) for patch in patches) / 1048576.0
            if len(patches) > 1:
                route = ' → '.join(['v%s' % str(patch.get('from') or '?')
                                    for patch in patches] + ['v%s' % latest])
                head = ('可以分 %d 步增量更新（%s）：只下各版之间变化的文件（一共 %.1f MB），'
                        '不用重下整个安装包。' % (len(patches), route, size))
            else:
                head = ('可以增量更新：只下 v%s 新改的那些文件（%.1f MB），不用重下整个安装包。'
                        % (latest, size))
            self._render_update(
                [head, '',
                 '点「立即更新」：下好之后程序会自己退出、换好文件，再自己开回来。'],
                self._update_buttons('patch', plan))
        elif mode == 'none':
            noticed = str((self.remote_version or {}).get('latest') or '')
            if notice_mod.is_newer(noticed, APP_VERSION):
                # version.json 说有新版，但 update.json 里的版本号还是旧的 —— 清单没跟上。
                # 拿不到增量包不等于没有更新，退回「去下载」这条路（红点也是这么来的）。
                self._render_update(
                    ['版本信息说已经有 v%s 了，但更新清单里还没有这一版（清单还没跟上）。' % noticed,
                     '这一版没有能直接用的增量包，点「去下载」用浏览器打开 B站主页'
                     '（下载链接发在那儿）；下载完直接装（装的时候会覆盖旧版）。'],
                    self._update_buttons('full'))
            else:
                self._render_update(['已经是最新版本，不用更新。'], self._update_buttons())
        elif mode == 'full':
            self._render_update(
                ['这一版没有能直接用的增量包（差得太多，或者差分包还没传上来）。',
                 '点「去下载」用浏览器打开 B站主页（下载链接发在那儿）；'
                 '下载完直接装（装的时候会覆盖旧版）。'],
                self._update_buttons('full'))
        else:
            why = str(plan.get('why') or self._update_why or '拉不到更新清单')
            self._render_update(
                ['增量更新暂时用不了：%s' % why,
                 '可以点「去下载」拿完整安装包，或者过会儿点「重新检查」。'],
                self._update_buttons('full'))

    def start_incremental_update(self, plan=None):
        """开始正经更新：后台下差分包 —— 下完让程序退出，剩下的交给更新器。"""
        plan = dict(plan or self._update_plan or {})
        if str(plan.get('mode') or '') != 'patch':
            self.log('增量更新：现在没有可用的差分包')
            return
        if self._update_busy:
            return
        self._update_busy = True
        self._render_update(['正在准备下载…'], [('关闭', self._hide_info_panel, False)])
        threading.Thread(target=self._run_incremental_update, args=(plan,),
                         daemon=True).start()

    def _run_incremental_update(self, plan):
        # 这份清单可能是启动那会儿拉的旧货（CDN 缓存 / 在内存里放了很久）：真下载之前
        # 按最新清单再算一遍。不然会出现「清单里写的大小跟下下来的包对不上」——
        # 差分包地址是同一个，仓库那边换了新包，旧清单就校验不过（实测踩过）。
        try:
            fresh, _why = notice_mod.update_info(version=dict(self.remote_version or {}))
        except Exception:
            fresh = {}
        if fresh:
            again = update_mod.plan(APP_VERSION, app_dir(), data=fresh)
            if str(again.get('mode') or '') == 'none':
                self.update_done.emit('', '刷新了一下更新清单：现在不需要更新了。')
                return
            if str(again.get('mode') or '') == 'patch' and plan_patches(again):
                plan = again
                self.remote_update = dict(fresh)
        patches = plan_patches(plan)
        if not patches:
            self.update_done.emit('', '清单里没有能用的差分包地址。')
            return
        latest = str(plan.get('latest') or '')
        folder = update_mod.patch_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as error:
            self.update_done.emit('', '建不了临时目录：%s' % error)
            return
        paths = []
        count = len(patches)
        for index, patch in enumerate(patches, 1):
            url = str(patch.get('url') or '')
            name = (os.path.basename(url.split('?')[0])
                    or ('AutoPlay-patch-%s-%d.zip' % (latest, index)))
            self._update_hop = (index, count, str(patch.get('from') or ''),
                                str(patch.get('to') or ''))
            path, why = update_mod.download(
                url, os.path.join(folder, name),
                progress=lambda got, total: self.update_progress.emit(got, total),
                expect_size=patch.get('size') or 0,
                expect_sha=patch.get('sha256') or '')
            if why:
                hint = ''
                if ('大小不对' in why) or ('sha256' in why):
                    hint = '（这份清单可能不是最新的：点「重新检查」刷新一下再试）'
                if count > 1:
                    why = '第 %d/%d 个包：%s' % (index, count, why)
                self.update_done.emit('', why + hint)
                return
            paths.append(path)
        _runner, why = update_mod.launch_updater(paths, app_dir(), latest,
                                                 pid=os.getpid(), relaunch=True)
        if why:
            self.update_done.emit('', why)
            return
        if len(paths) > 1:
            note = ('%d 个更新包已经下好了（%s）。程序这就退出，更新器会按顺序换好文件，'
                    '再把它开回来。' % (len(paths),
                                        '、'.join(os.path.basename(item) for item in paths)))
        else:
            note = ('更新包已经下好了（%s）。程序这就退出，更新器会换好文件再把它开回来。'
                    % os.path.basename(paths[0]))
        self.update_done.emit(note, '')

    def _on_update_progress(self, got, total):
        """下载差分包：把进度刷在更新窗口的正文上。"""
        index, count, src, dst = getattr(self, '_update_hop', (1, 1, '', ''))
        prefix = '正在下载更新包'
        if count > 1:
            prefix = '正在下载第 %d/%d 个更新包（%s → %s）' % (index, count, src, dst)
        if total:
            text = '%s… %.1f / %.1f MB（%d%%）' % (
                prefix, got / 1048576.0, total / 1048576.0,
                int(got * 100.0 / max(1, total)))
        else:
            text = '%s… 已下 %.1f MB' % (prefix, got / 1048576.0)
        self._update_tail = [text, '', '下载完程序会自动退出、换好文件再自己开回来。']
        window = getattr(self, 'info_window', None)
        if window is None or not (self._info_visible() and self._info_kind == 'update'):
            return
        header = list(getattr(self, '_update_header_lines', []))
        self._update_body = '\n'.join(header + [''] + self._update_tail)
        window.set_body(self._update_body)

    def _on_update_done(self, note, why):
        """差分包下好了（或者哪儿出错了）—— 在主线程里跑。"""
        self._update_busy = False
        if why:
            self.log('增量更新没成：%s' % why)
            self._render_update(
                [why, '', '可以点「去下载」拿完整安装包（装的时候会覆盖旧版）。'],
                self._update_buttons('full'))
            return
        self.log(note)
        self._render_update([note], [])
        QTimer.singleShot(1200, self._finish_for_update)

    def _finish_for_update(self):
        """真的要退出了：更新器在等着换文件，退完它换好会自己把程序开回来。"""
        if self.quit_app('增量更新') is False:
            self._render_update(
                ['更新器已经在后台等着了：把编辑器里没保存的东西存一下、关掉编辑器窗口，'
                 '程序退出去的时候就会自动换上。'],
                [('关闭', self._hide_info_panel, False)])

    def set_status(self, text, kind='idle'):
        self._status_last = (text, kind)      # 换主题时要用它把这一颗重上一遍
        foreground, alpha = STATUS_COLORS.get(kind, STATUS_COLORS['idle'])
        foreground = theme.c(foreground)
        background = _rgba(foreground, alpha)
        self.status.setText(text)
        self.status.updateGeometry()          # 文字变长了要重新排一次，别等下一帧才跟上
        self.status.setStyleSheet(
            'background: %s; color: %s; border-radius: 8px; padding: 0 10px; font-size: 12px;'
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
        self.overlay_action = self.tray_menu.addAction('显示 / 隐藏进度浮窗', self.toggle_overlay)
        self.overlay_action.setCheckable(True)
        self.overlay_action.setChecked(self.overlay_on)
        self.overlay_action.setToolTip('屏幕右上角那个演奏进度浮窗；'
                                       '跟界面上的「显示演奏状态」是同一个开关')
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

    def _screen_area(self):
        """窗口现在在哪块屏幕上（拿不到就用主屏，再拿不到就算了）。"""
        try:
            screen = self.screen() or QApplication.primaryScreen()
            return screen.availableGeometry() if screen is not None else None
        except Exception:
            return None

    def _keep_on_screen(self):
        """窗口别跑到屏幕外面 —— 改完大小顺手把位置拉回来。"""
        if getattr(self, 'cover_mode', False) or getattr(self, 'overlay_active', False):
            return                       # 浮层形态的位置是算好的，别动它
        area = self._screen_area()
        if area is None:
            return
        frame = self.frameGeometry()
        left = min(max(frame.left(), area.left()),
                   max(area.right() - frame.width() + 1, area.left()))
        top = min(max(frame.top(), area.top()),
                  max(area.bottom() - frame.height() + 1, area.top()))
        try:
            if (left, top) != (frame.left(), frame.top()):
                self.move(int(left), int(top))
        except Exception:
            pass

    def _fit_window_size(self, force=False):
        """按屏幕可用区域定窗口大小（1K 屏 / 系统缩放也不许顶出屏幕）。"""
        area = self._screen_area()
        if area is None:
            self.setMinimumSize(700, 560)
            self.resize(BASE_WINDOW_W, BASE_WINDOW_H)
            return
        width, height = window_size_for(area.width(), area.height())
        self.setMinimumSize(min(700, width), min(430, height))
        if force or not self.isVisible():
            self.resize(width, height)
        elif (self.width() > area.width() - 20) or (self.height() > area.height() - 20):
            self.resize(min(self.width(), width), min(self.height(), height))
        self._keep_on_screen()

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

    def toggle_overlay(self, checked=None):
        """托盘菜单：显示 / 隐藏右上角的演奏进度浮窗（跟勾选框同一个开关）。"""
        want = (not self.overlay_on) if checked is None else bool(checked)
        self.set_overlay_visible(want)

    def _overlay_after_finish(self):
        """浮窗把结果亮完之后：有谱面就切回「已就绪」并继续显示，否则收起来。"""
        if self.overlay_on and self.score_events and not self.isMinimized():
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
            set_topmost(self, True)
            self._topmost_on = True
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
            borrowed = (bool(getattr(self, '_keyboard_borrowed', False))
                        and self.overlay_active)
            flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
            if self.overlay_active:
                if not borrowed:           # 曲库那一页还开着：键盘是借来的，别把焦点锁死
                    flags |= Qt.WindowType.WindowDoesNotAcceptFocus
                # 浮层永远置顶：它就是贴着游戏的一小块。编辑器已经是独立窗口了，
                # 不用再为它让路（见 open_editor）。
                flags |= Qt.WindowType.WindowStaysOnTopHint
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not borrowed)
                self.setWindowOpacity(self._overlay_opacity())
            else:
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
                self.setWindowOpacity(OVERLAY_OPACITY_EDITOR)
            self.setWindowFlags(flags)
            self._topmost_on = None          # 标志重设过了，置顶状态得重新算一遍
            # setWindowFlags 会重建原生窗口：拖放目标（OLE drop site）跟着一起重挂一遍，
            # 不然拖文件进窗口会变成「禁止」光标 —— 明明 setAcceptDrops 过。
            self.setAcceptDrops(True)
            allow_lower_privilege_drop(self)
            # 再手动补一层：Qt 万一没认这个位，咱自己保证它一定在。
            set_no_activate(self, self.overlay_active and not borrowed)
            # 关闭 / 最小化 / 最大化这三个（以及键位）现在常显：桌面用着也能一键
            # 收进托盘后台，不用回托盘菜单。
            self.setProperty('cover', 'true' if self.cover_mode else 'false')
            self.style().unpolish(self)      # 动态属性改了要重新套一遍样式
            self.style().polish(self)
            if visible:                    # setWindowFlags 会把窗口藏起来，得重新显示
                # 一定要走 Qt 自己的 show()：setWindowFlags 重建窗口之后 Qt 认为这个
                # 窗口是「藏起来」的，只用 Win32 的 ShowWindow 把它显示出来，Qt 那边
                # 不认 —— 界面不画、isVisible() 还是 False。从任务栏点回来的时候就是
                # 这个毛病：系统明明把窗口还原了（IsIconic=0），屏幕上却什么都没有。
                # 浮层形态靠上面那行 WA_ShowWithoutActivating 保证 show() 也不抢游戏前台。
                self.show()
                if self.overlay_active:
                    show_no_activate(self)  # 再补一刀：确保置顶 + 不激活
                self.move(pos)
                if self._maximized and not self.overlay_active:
                    self.showMaximized()   # 换个形态别把最大化弄丢了
            self._sync_game_ui()
            self._sync_overlay_topmost()
        except Exception as exc:
            self.log('切换窗口模式出错：%s' % exc)

    def _overlay_opacity(self):
        """浮层形态下，整个窗口该有多透明（留一点透明，好看见底下的游戏）。"""
        return OVERLAY_OPACITY

    def _sync_overlay_opacity(self):
        """换页之后把浮层不透明度补上（编辑器页要变回不透明）。"""
        if not getattr(self, 'overlay_active', False):
            return
        if self.isMinimized():
            return          # 最小化时是刻意做成不透明的，别又把 layered 加回去
        try:
            self.setWindowOpacity(self._overlay_opacity())
        except Exception:
            pass

    def _sync_overlay_topmost(self):
        """
        换页 / 进出游戏之后，把「置顶」这个状态补对。

        浮层形态就一律置顶（编辑器是独立窗口，不掺和这块）。

        这里只动 Win32 的 topmost 位（SetWindowPos），不去 setWindowFlags ——
        重设窗口标志会把窗口藏一下再显示，编辑到一半闪一下、焦点还可能丢，不划算。
        """
        if not getattr(self, 'overlay_active', False):
            return
        want = True
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
                              '游戏里不开简谱编辑器（编辑器是个新窗口，会把游戏顶回桌面）；回桌面再用')):
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
        # 选文件面板跟着一起收掉：不然「选文件」还挂在半路（_choosing 不为空），
        # 下次唤起来再点「曲库」会被当成重入直接吞掉，看着就是点了没反应。
        self._close_picker()
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

    def minimize_window(self):
        """
        最小化：正常的窗口最小化，任务栏里还看得见（跟「关闭」不一样）。

        这里要绕一个 Windows 的坑：浮层形态的窗口是**半透明**的（setWindowOpacity），
        半透明会让系统给它加一层 layered 窗口；这种窗口最小化的时候，系统不一定把
        留在桌面上的那一层擦掉，屏幕上就会剩一块「残影」，得点一下别的窗口才消失。
        所以最小化之前先把不透明度恢复成 1.0（等于把那层拆掉），窗口回来之后再补上；
        顺手把原来占的那块屏幕区域标脏，让系统重画一遍兜底。

        还有一个更阴的坑：浮层窗口带着 WS_EX_NOACTIVATE，**系统不给它任务栏按钮**，
        最小化的飞行动画就没有落点，Windows 会把窗口最后那幅画面留在桌面上，任务栏里
        也找不到它 —— 所以最小化前会先把窗口切成普通窗口（见
        _enter_minimized_visual_state）。

        另外把右上角那个进度浮窗一起收起来 —— 窗口都进任务栏了，桌上再留一颗
        「已就绪」看着就像没关干净（正在演奏时不收：那是唯一能看进度的地方）。
        """
        try:
            self._enter_minimized_visual_state()
            log_event('主窗口最小化')
        except Exception as exc:
            self.log('最小化失败：%s' % exc)

    def _enter_minimized_visual_state(self):
        """
        最小化的时候要做的收尾（「—」、Win+D、任务栏右键都走这儿）：
        记下要擦的屏幕区域 → 收起进度浮窗 → **换成普通窗口** → 拆掉半透明那层 →
        最小化 → 过一会儿擦残影。

        为什么要「换成普通窗口」：浮层配方给窗口挂了 WS_EX_NOACTIVATE（点它不抢游戏
        焦点），而带这个位的窗口**在窗口创建的那一刻就被系统判成「不进任务栏」**，
        事后补 WS_EX_APPWINDOW 也补不回来（实测：任务栏那一行像素从头到尾一模一样，
        UIA 里也找不到按钮）。没有任务栏按钮，最小化动画就没有落点，Windows 会把窗口
        最后那幅画面丢在桌面上（实测：屏幕左边留一条标题栏残影），任务栏里也找不到它。
        所以最小化前先用 _apply_window_mode(False) 把窗口**重建成普通窗口**（任务栏
        这时候才认它），还原的时候再切回浮层。

        一次最小化只做一遍：Windows 有时候会连着扔好几个状态事件过来，重复做不但
        白费劲，还会把「浮窗是这次收起来的」这个标记洗掉，还原之后浮窗就回不来了。
        （_minimized_visual_done 在 _restore_after_minimize 里清掉，下次最小化照常。）
        """
        if self._minimized_visual_done:
            return
        self._minimized_visual_done = True
        self._capture_ghost_rect()
        self._hide_overlay_for_minimize()
        self._minimize_busy = True
        try:
            if getattr(self, 'overlay_active', False):
                # 先藏起来再换：setWindowFlags 会重建窗口，藏着换完屏幕上不会闪
                self._overlay_suspended_for_minimize = True
                self.hide()
                self._apply_window_mode(False)
            try:
                self.setWindowOpacity(1.0)      # 拆掉 layered，免得留下残影
            except Exception:
                pass
            set_taskbar_window(self, True)      # 任务栏得有它，最小化动画才有落点
            self.showMinimized()
            # Qt 在「窗口刚重建过」的时候不一定真把窗口缩下去（setWindowState 只在
            # 窗口可见时才转成 ShowWindow(SW_MINIMIZE)）。直接问系统一句，没缩就补一刀。
            try:
                user32 = ctypes.windll.user32
                hwnd = ctypes.c_void_p(int(self.winId()))
                if hwnd and not user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 6)      # SW_MINIMIZE
            except Exception:
                pass
        finally:
            self._minimize_busy = False
        QTimer.singleShot(60, self._erase_ghost)   # 等缩下去的动画走完再擦那块

    def _capture_ghost_rect(self):
        """
        记下最小化前「窗口 + 进度浮窗」一起占掉的屏幕区域。

        残留不止主窗口自己那一块：右上角那个进度浮窗是独立置顶窗口，主窗口缩下去
        之后它还会留在桌面上，所以两块都要擦。这里把两个矩形并成一个再存。
        """
        try:
            rect = self._window_rect_for_ghost()
            if self.overlay.isVisible():
                rect = rect.united(self.overlay.frameGeometry())
            self._ghost_rect = (rect.left(), rect.top(), rect.right() + 1, rect.bottom() + 1)
        except Exception:
            self._ghost_rect = None

    def _window_rect_for_ghost(self):
        """
        主窗口「正常时候」占的屏幕区域。

        注意：窗口已经缩下去之后 frameGeometry() 在 Windows 上会变成 (-32000, -32000)
        这种占位坐标，拿它去擦残影没用。所以一旦发现是这种离谱坐标，就改用
        normalGeometry()（Qt 记着的「没最大化也没最小化时」的位置）。
        """
        rect = self.frameGeometry()
        if rect.left() < -30000 or rect.top() < -30000:
            rect = self.normalGeometry()
        return rect

    def _hide_overlay_for_minimize(self):
        """
        最小化时把进度浮窗收起来（正在演奏就不动，那是唯一能看进度的地方）。

        收起来了就把标记立起来，还原的时候好按标记把它补回去；已经立着就直接返回，
        免得第二次调用把标记又抹平。
        """
        if getattr(self, '_overlay_hidden_by_minimize', False):
            return
        try:
            if self.overlay_on and self.overlay.isVisible() and not self._playing():
                self.overlay.shutdown()
                self._overlay_hidden_by_minimize = True
        except Exception:
            pass

    def _restore_after_minimize(self):
        """窗口从最小化回来了：把「任务栏形态」、不透明度和进度浮窗都补回该有的样子。"""
        if self.isMinimized():
            return          # 还在最小化（换形态时也会来状态事件），这会儿别急着复原
        self._minimized_visual_done = False
        self._erase_ghost()
        if getattr(self, '_overlay_suspended_for_minimize', False):
            self._overlay_suspended_for_minimize = False
            self._minimize_busy = True
            try:
                # 之前为了最小化切成了普通窗口，现在按用户原来的设置切回去（浮层 / 普通）
                self._apply_window_mode(self.cover_mode)
            finally:
                self._minimize_busy = False
            set_taskbar_window(self, not self.overlay_active)
        try:
            self.setWindowOpacity(self._overlay_opacity() if self.overlay_active
                                  else OVERLAY_OPACITY_EDITOR)
        except Exception:
            pass
        if getattr(self, '_overlay_hidden_by_minimize', False):
            self._overlay_hidden_by_minimize = False
            if self.overlay_on and self.score_events:
                try:
                    self.overlay.set_ready(len(self.score_events))
                except Exception:
                    pass

    def _erase_ghost(self):
        """把最小化前那块屏幕标脏，让 Windows 重画 —— 擦掉 layered 窗口留下的残影。"""
        rect = self._ghost_rect
        self._ghost_rect = None
        if not rect:
            return
        try:
            class _RECT(ctypes.Structure):
                _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                            ('right', ctypes.c_long), ('bottom', ctypes.c_long)]
            ctypes.windll.user32.RedrawWindow(
                None, ctypes.byref(_RECT(*rect)), None,
                0x0001 | 0x0004 | 0x0080 | 0x0100)   # 标脏|擦背景|连子窗口|立刻重画
        except Exception:
            pass

    def _refresh_max_button(self):
        """右上角那个按钮：按状态换字、换配色和提示。"""
        if getattr(self, 'max_button', None) is not None:
            self.max_button.setText('▣' if self._maximized else '□')
            self.max_button.setToolTip('还原窗口（也可以按 F11）' if self._maximized
                                       else '最大化 / 还原（也可以按 F11）')
            self.max_button.setProperty('maxed', bool(self._maximized))
            self.max_button.style().unpolish(self.max_button)
            self.max_button.style().polish(self.max_button)
        # 编辑器窗口自己也有一个一样的按钮，跟着一起变（别一个亮一个暗）
        editor_window = getattr(self, 'editor', None)
        if editor_window is not None:
            try:
                editor_window._refresh_max_button()
            except Exception:
                pass

    def changeEvent(self, event):
        """盯着窗口状态：按钮上的字跟着变；从最小化回来要把不透明度和浮窗补上。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._maximized = bool(self.windowState() & Qt.WindowState.WindowMaximized)
            self._refresh_max_button()
            if getattr(self, '_minimize_busy', False):
                return          # 正在为最小化换形态：中间那几次状态事件别当真的用
            if self.windowState() & Qt.WindowState.WindowMinimized:
                # 不是点「—」最小化的（Win+D、任务栏右键这些）也走这里补一刀：
                # 半透明窗口最小化容易留残影，先把那层拆掉再擦一遍。
                self._enter_minimized_visual_state()
            else:
                self._restore_after_minimize()

    def _drop_topmost(self):
        """唤起之后过一阵：窗口已经不在用了就撤掉「总在最前」，别一直挡着游戏。"""
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
            if self.practice or self.rhythm_run:
                paused = self.follow.toggle_pause()
                running = '音游中' if self.rhythm_run else '跟奏中'
                self.set_status('已暂停' if paused else running,
                                'pause' if paused else 'play')
                self.overlay.set_state('pause' if paused else 'play')
                log_event(('音游' if self.rhythm_run else '练习') + ('暂停' if paused else '继续'))
                return
            self.player.toggle_pause()
            if self.player.running.is_set():
                paused = self.player.pause_flag.is_set()
                self.set_status('已暂停' if paused else '演奏中', 'pause' if paused else 'play')
                self.overlay.set_state('pause' if paused else 'play')
                self._sync_follow_state('pause' if paused else 'play')
                log_event('暂停' if paused else '继续')
        elif action == 'stop':
            if self.practice or self.rhythm_run:
                log_event('停止音游' if self.rhythm_run else '停止跟奏练习')
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
        self._load_notices_read()      # 哪些公告已经看过（记在设置里，顶部铃铛的红点看它）
        self._load_update_seen()       # 哪个新版本已经打过招呼（顶部「更新」的红点看它）
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
        # 演奏状态浮窗（屏幕右上角那个）显不显示（默认显示；调试时能关掉）
        want_overlay = bool(self.settings.value('overlay_on', OVERLAY_DEFAULT, type=bool))
        if getattr(self, 'overlay_box', None) is not None:
            self.overlay_box.blockSignals(True)
            self.overlay_box.setChecked(want_overlay)
            self.overlay_box.blockSignals(False)
        self.set_overlay_visible(want_overlay, save=False)
        theme.load_all()
        want_theme = theme.set_current(str(self.settings.value('theme', theme.DEFAULT_NAME)))
        self._fill_theme_combo(want_theme)
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
        # 同音重复敏感度（转谱用）
        if self.repeat_slider is not None:
            self._repeat_level = self._load_repeat_level()
            self.repeat_slider.blockSignals(True)
            self.repeat_slider.setValue(self._repeat_level)
            self.repeat_slider.blockSignals(False)
            self._refresh_repeat_label()
        # 游戏内覆盖
        self.cover_mode = bool(self.settings.value('cover', True, type=bool))
        if self.cover_box is not None:
            self.cover_box.blockSignals(True)
            self.cover_box.setChecked(self.cover_mode)
            self.cover_box.blockSignals(False)
        self._apply_window_mode()
        self._refresh_hotkey_labels()

    # ---------- 演奏状态浮窗（屏幕右上角那个）：显示 / 隐藏 ----------

    def on_overlay_toggled(self, enabled):
        """「显示演奏状态」勾选框：记进设置，并且立刻显示 / 隐藏那个浮窗。"""
        self.set_overlay_visible(enabled)

    def on_error_report_toggled(self, enabled):
        """「出错自动上报」勾选框：记在本机（默认开），下次出错就照它办。"""
        errorreport.set_enabled(bool(enabled))
        self.log('出错自动上报：%s' % ('开' if enabled else '关'))

    def set_overlay_visible(self, enabled, save=True):
        """
        屏幕右上角那个演奏进度浮窗（「已就绪 / 演奏中 12/345」）开不开。

        关掉之后**演奏时也不弹**（演奏、热键、试听照常，只是没那个小窗）——
        调试的时候不挡视线。选择会记进设置；托盘菜单里那一项跟这个同步。
        """
        enabled = bool(enabled)
        self.overlay_on = enabled
        if save:
            try:
                self.settings.setValue('overlay_on', enabled)
            except Exception:
                pass
        box = getattr(self, 'overlay_box', None)
        if box is not None and box.isChecked() != enabled:
            box.blockSignals(True)
            box.setChecked(enabled)
            box.blockSignals(False)
        action = getattr(self, 'overlay_action', None)
        if action is not None and action.isChecked() != enabled:
            action.blockSignals(True)
            action.setChecked(enabled)
            action.blockSignals(False)
        if not enabled:
            self.overlay.shutdown()
        elif self._playing():
            # 演奏到一半才打开：接着显示当前进度（别退回「已就绪」）
            total = len(self.score_events) or 1
            self.overlay.begin(total)
            try:
                self.overlay.set_progress(self.bar.value(), total)
            except Exception:
                pass
        elif self.score_events:
            # 没在演奏但有谱面：显示「已就绪」（跟以前一样，载入曲子才亮）
            self.overlay.set_ready(len(self.score_events))
        log_event('演奏状态浮窗：%s' % ('显示' if enabled else '隐藏'))

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

    def _load_repeat_level(self):
        """设置里存的敏感度档位。老版本存的是「同音重复更敏感」那个 bool，一起认。"""
        level = self.settings.value('repeat_level', None)
        if level is None:
            old = bool(self.settings.value('repeat_sensitive', False, type=bool))
            return REPEAT_LEVEL_SENSITIVE if old else REPEAT_LEVEL_DEFAULT
        try:
            level = int(level)
        except (TypeError, ValueError):
            return REPEAT_LEVEL_DEFAULT
        return max(0, min(level, self.repeat_slider.maximum()))

    def _refresh_repeat_label(self):
        """把当前档位名写到滑块右边那个标签上。"""
        if self.repeat_value is None:
            return
        name, _extra = audio2midi.repeat_level(self._repeat_level)
        self.repeat_value.setText(name)

    def on_repeat_level(self, value):
        """拖了「同音重复」滑块：档位名跟着变，也记进设置（下次转谱就用它）。"""
        self._repeat_level = int(value)
        self.settings.setValue('repeat_level', self._repeat_level)
        self._refresh_repeat_label()

    def on_repeat_apply(self):
        """「应用」：用当前档位把刚才那段音频重转一遍。"""
        name, extra = audio2midi.repeat_level(self._repeat_level)
        self.log('同音重复敏感度：%s%s' % (
            name,
            ('（%s）' % '，'.join('%s=%s' % item for item in sorted(extra.items())))
            if extra else '（标准参数）'))
        if self._audio_source is None:
            self.log('还没转过音频 —— 先按「音频转 MIDI…」选一段，之后改敏感度点'
                     '「应用」就能直接重转')
            return
        if self._converting:
            self.log('上一段音频还在转，稍等一下')
            return
        self.convert_audio(self._audio_source)

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
        self._apply_style_now()

    def _fill_theme_combo(self, current=None):
        """
        把主题下拉框填一遍：本地那几套 + 最后一条「线上主题」占位。

        「线上主题」是预埋：现在灰着点不了（真做了以后就是从这里选线上配色，见
        theme.ONLINE_FILE）。
        """
        combo = getattr(self, 'theme_combo', None)
        if combo is None:
            return
        names = theme.names_for_combo()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        combo.setItemEnabled(len(names) - 1, False)     # 线上那条先灰着
        combo.setCurrentText(current or theme.current_name())
        combo.blockSignals(False)

    def _apply_style_now(self):
        """把当前主题的样式表真正套一遍（refresh_style 的内胆）。"""
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style_sheet())
        # 编辑器窗口有自己的样式表（跟主界面不是一张），得挨个补一遍
        for window in [getattr(self, 'editor', None)] + list(getattr(self, 'editor_windows', [])):
            if window is None:
                continue
            try:
                window.apply_style()
            except Exception:
                pass
        last = getattr(self, '_status_last', None)
        if last is not None and getattr(self, 'status', None) is not None:
            self.set_status(*last)              # 状态胶囊那点颜色也要跟着换
        # 顶部动作条那几个图标是代码画的，颜色得按新主题重画一遍（公告的红点也在里面）
        self._refresh_icons()
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
        self._fill_theme_combo(theme.current_name())
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
            if self.practice or self.rhythm_run:
                if self.follow is not None:
                    self.follow.cancel()       # 练习 / 音游中途关掉跟奏 = 结束
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

    def _borrow_keyboard(self, on):
        """
        浮层形态的窗口**收不到键盘**（点它不抢游戏焦点，这是刻意设计的）—— 所以贴在里面
        的搜索框、输入框都打不了字。

        打开「曲库 / 选文件」这一页时，如果人不在游戏里，就临时把窗口变回「能接收键盘」：
        桌面上抢焦点没有任何副作用。人真在游戏里就不动（在那儿本来也不该打字），搜索框会
        置灰并写明白。关掉这一页时原样还回去（浮层：不抢焦点 + 置顶）。
        """
        on = bool(on)
        self._keyboard_borrowed = on
        if not getattr(self, 'overlay_active', False):
            return                          # 普通窗口本来就能打字
        try:
            visible = self.isVisible()
            flags = self.windowFlags()
            if on:
                flags &= ~Qt.WindowType.WindowDoesNotAcceptFocus
            else:
                flags |= Qt.WindowType.WindowDoesNotAcceptFocus
            self.setWindowFlags(flags)
            self.setAcceptDrops(True)       # 标志重设后拖放目标重挂一遍
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not on)
            set_no_activate(self, not on)
            if visible:
                self.show()                 # setWindowFlags 会把窗口藏一下
            if on:
                force_window_front(self)    # 桌面上：抢前台，键盘才进得来
            else:
                set_topmost(self, True)     # 还回浮层：置顶 + 不激活
                show_no_activate(self)
                self._topmost_on = True
            log_event('搜索键盘模式：%s' % ('开（桌面上临时让窗口能接收键盘）' if on else '关'))
        except Exception as exc:
            log_event('切换搜索键盘模式出错：%s' % exc)

    def _add_search_row(self, box, list_widget, placeholder):
        """
        在列表上面插一条搜索栏：放大镜图标 + 关键字 + 「正则」勾选框。

        命中的条目留下、没命中的藏起来，命中的那一段换个底色（见 SearchDelegate）。
        关键字空着就显示全部。
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setClearButtonEnabled(True)
        edit.setToolTip('按曲名 / 歌手过滤；命中的那一段会高亮。\n'
                        '勾「正则」就按正则表达式匹配（例如 ^钢琴|piano$）。')
        can_type = (not getattr(self, 'overlay_active', False)
                    or bool(getattr(self, '_keyboard_borrowed', False)))
        if not can_type:
            # 游戏里的浮层不抢焦点、收不到键盘 —— 与其让人点了打不出字，不如置灰说清楚
            edit.setEnabled(False)
            edit.setPlaceholderText('游戏里没法打字：回桌面再用搜索')
            edit.setToolTip('浮层模式（刻意不抢游戏焦点）下窗口收不到键盘，所以游戏里搜不了。\n'
                            '回桌面再点「曲库」，搜索栏就能打字了。')
        try:
            edit.addAction(uiicons.make_icon('search', self._icon_color(), 15,
                                             bg=self._icon_bg()),
                           QLineEdit.ActionPosition.LeadingPosition)
        except Exception:
            pass
        regex_box = QCheckBox('正则')
        regex_box.setToolTip('勾上：关键字按正则表达式匹配（例如 ^钢琴|piano$）；\n'
                             '不勾：普通关键字，曲名 / 歌手里包含它就算命中。')
        row.addWidget(edit, 1)
        row.addWidget(regex_box, 0)
        if not can_type:
            regex_box.setEnabled(False)
        box.addLayout(row)

        delegate = SearchDelegate(list_widget)
        list_widget.setItemDelegate(delegate)

        def apply(*_args):
            if list_widget is not getattr(self, 'pick_list', None):
                return                     # 面板已经收掉了
            term = edit.text()
            delegate.set_term(term, regex_box.isChecked())
            total, hits = 0, 0
            for index in range(list_widget.count()):
                item = list_widget.item(index)
                total += 1
                ok = bool(delegate.matches(item.text())) if term else True
                item.setHidden(not ok)
                if term and ok:
                    hits += 1
            try:
                list_widget.viewport().update()
            except Exception:
                pass
            hint = getattr(self, 'pick_hint', None)
            if hint is not None:
                if term:
                    if not getattr(self, '_pick_hint_saved', False):
                        self._pick_hint_base = hint.text()
                        self._pick_hint_saved = True
                    hint.setText('命中 %d 个（共 %d 个）' % (hits, total))
                elif getattr(self, '_pick_hint_saved', False):
                    # 关键字清空：把原来那句提示还回去（不是留一句「命中 0 个」）
                    if getattr(self, '_pick_hint_base', ''):
                        hint.setText(self._pick_hint_base)
                    self._pick_hint_saved = False
            if (self._pick_mode == 'online'
                    and getattr(self, 'pick_ok', None) is not None):
                self.pick_ok.setEnabled(any(not list_widget.item(i).isHidden()
                                            for i in range(list_widget.count())))

        edit.textChanged.connect(apply)
        regex_box.toggled.connect(apply)
        self._pick_search_apply = apply
        return edit, regex_box

    # ---------- 联网曲库 ----------

    def open_online_library(self):
        """
        联网曲库：歌单在 Gitee / GitHub 上，程序拉一个索引就知道有哪些曲子。

        跟「曲库」是两回事（那是本机的那份），入口也各是各的。歌单直接贴在浮层里，
        **一个窗口都不弹** —— 在游戏里（Ctrl+F1 唤起来的浮层）点它也能用，不会把游戏
        顶回桌面。两套曲库：国内（Gitee，默认）和 GitHub（备用），拉不到就点上面另一套。

        下载的曲子落在本地缓存目录里（%LOCALAPPDATA%\\AutoPlay\\library\\），
        双击（或点「下载并载入」）下完直接读进来。
        """
        if self._choosing is not None:
            self._close_picker()          # 上次没收干净就先收拾掉
        self._hide_info_panel()           # 公告 / 更新那页开着就先收掉，别叠在一起
        self._pick_mode = 'online'
        self._borrow_keyboard(not self.in_game())   # 桌面上让窗口能接收键盘，搜索框才打得进去
        panel = QWidget(self.pick_panel)
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel('曲库'))
        site_buttons = {}
        for key in library.SITE_ORDER:
            button = QPushButton(library.SITE_LABELS.get(key, key))
            button.setObjectName('siteBtn')
            button.setCheckable(True)
            button.setToolTip('用这套曲库：%s' % library.default_index_url(key))
            button.setChecked(key == library.source_name())
            bar.addWidget(button)
            site_buttons[key] = button
        bar.addStretch(1)
        folder_button = QPushButton('打开本地曲库文件夹')
        folder_button.setToolTip('下载的曲子都放在 %s\n'
                                 '想把本机的曲子放进来，直接复制进去再点「刷新」。'
                                 % library.cache_dir())
        bar.addWidget(folder_button)
        upload_button = QPushButton('上传 / 整理曲库…')
        upload_button.setToolTip('把本机的 midi 传到曲库（Gitee 和 GitHub 两套都传），或者按仓库里\n'
                                 '现有的文件重新生成一份 library.json（索引）。传上去就代表同意分享。\n'
                                 '（要开窗口填标题、挑文件，所以游戏里点它只会记一行日志，回桌面再点。）')
        bar.addWidget(upload_button)
        box.addLayout(bar)

        self.pick_list = QListWidget()
        self._add_search_row(box, self.pick_list, '搜索曲名 / 歌手（命中的会高亮）')
        box.addWidget(self.pick_list, 1)

        self.pick_hint = QLabel('')
        self.pick_hint.setObjectName('hint')
        self.pick_hint.setWordWrap(True)
        box.addWidget(self.pick_hint)
        self.pick_status = QLabel('')
        self.pick_status.setObjectName('hint')
        self.pick_status.setWordWrap(True)
        box.addWidget(self.pick_status)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        refresh = QPushButton('刷新')
        self.pick_ok = QPushButton('下载并载入')
        self.pick_ok.setObjectName('primary')
        self.pick_ok.setEnabled(False)
        close = QPushButton('关闭')
        foot.addWidget(refresh)
        foot.addStretch(1)
        foot.addWidget(self.pick_ok)
        foot.addWidget(close)
        box.addLayout(foot)

        self._choosing = panel            # 跟选文件面板共用一份「正在挑东西」的状态
        self.pick_layout.addWidget(panel)
        self.log_view.hide()
        self.pick_panel.show()
        self._grow_for_picker()

        def fill(songs, message):
            self.pick_list.clear()
            self._pick_hint_saved = False      # 新的一批，搜索栏那句「命中 N 个」重新算
            for song in songs:
                bits = [str(song.get('title') or ''), str(song.get('artist') or '')]
                size = int(song.get('size') or 0)
                if size:
                    bits.append('%.0f KB' % (size / 1024.0))
                if library.is_cached(song):
                    bits.append('已下载')
                item = QListWidgetItem('　'.join(bit for bit in bits if bit))
                item.setData(Qt.ItemDataRole.UserRole, song)
                self.pick_list.addItem(item)
            self.pick_hint.setText(message)
            self.pick_ok.setEnabled(bool(self.pick_list.count()))
            if getattr(self, '_pick_search_apply', None) is not None:
                self._pick_search_apply()       # 搜索框里还留着字就照着再筛一遍

        def reload(_checked=False):
            if self.pick_list is None:            # 拉之前面板已经被关掉了
                return
            # 拉歌单也放后台：网不好的时候这一步要等好几秒，不能让整个界面僵住
            self._online_seq = getattr(self, '_online_seq', 0) + 1
            self._online_fill = fill
            self.pick_status.setText('正在拉曲库…（最多等十几秒，拉的时候界面不会卡住）')
            threading.Thread(target=self._fetch_online_index,
                             args=(self._online_seq,), daemon=True).start()

        def choose_site(key):
            """换一套曲库：记住选择，马上重新拉歌单。"""
            for other, button in site_buttons.items():
                button.setChecked(other == key)
            library.set_source_name(key)
            self.log('联网曲库切到：%s' % library.SITE_LABELS.get(key, key))
            reload()

        def open_cache(_checked=False):
            """打开本地曲库文件夹：用户直接往里复制曲子就行。"""
            folder = library.cache_dir()
            try:
                os.makedirs(folder, exist_ok=True)
                os.startfile(folder)
                self.pick_status.setText('本地曲库文件夹：%s' % folder)
            except Exception as exc:
                self.pick_status.setText('打不开这个文件夹（%s）：%s' % (folder, exc))

        def download_current(_checked=False):
            if getattr(self, '_downloading', False):
                self.pick_status.setText('上一首还在下，等它下完')
                return
            item = self.pick_list.currentItem() if self.pick_list is not None else None
            if item is None:
                self.pick_status.setText('先在上面挑一首')
                return
            song = item.data(Qt.ItemDataRole.UserRole) or {}
            # 下载放到后台线程：库里万一有大文件 / 网卡住，界面也不会整个僵住
            # （最多等 library.SONG_TIMEOUT 秒，见那边）。
            self._downloading = True
            self._downloading_seq = getattr(self, '_online_seq', 0)
            self.pick_ok.setEnabled(False)
            self.pick_status.setText('正在下载 %s…（最多等 %d 秒，界面不会卡住）'
                                     % (song.get('title', ''), int(library.SONG_TIMEOUT)))
            threading.Thread(target=self._download_online, args=(song,), daemon=True).start()

        refresh.clicked.connect(reload)
        self.pick_ok.clicked.connect(download_current)
        self.pick_list.itemDoubleClicked.connect(download_current)
        self.pick_list.currentItemChanged.connect(self._sync_pick_open)
        close.clicked.connect(self._close_picker)
        for key, button in site_buttons.items():
            button.clicked.connect(lambda _checked=False, k=key: choose_site(k))
        folder_button.clicked.connect(open_cache)
        upload_button.clicked.connect(lambda: self.open_upload_dialog())
        self.log('联网曲库就在浮层里：挑一首，双击（或点「下载并载入」）下到本地再读进来')
        # 先把面板亮出来（上面那行「正在拉曲库…」），再去拉网络 —— 不然点下去会愣一下
        QTimer.singleShot(0, reload)

    def _download_online(self, song):
        """后台线程：下联网曲库的一首，下完用信号回主线程（别在子线程碰界面）。"""
        try:
            path, why = library.download(song)
        except Exception as exc:                 # 后台线程里绝不能把异常漏出去
            path, why = '', str(exc)
        self.online_done.emit(path or '', why or ('' if path else '没下下来'))

    def _fetch_online_index(self, seq):
        """后台线程：拉一次联网曲库的歌单，拉完用信号回主线程。"""
        try:
            songs, why = library.fetch_index()
        except Exception as exc:
            songs, why = [], str(exc)
        self.online_index.emit(int(seq), songs or [], why or '')

    def _on_online_index(self, seq, songs, why):
        """歌单拉回来了（主线程）：铺到「联网曲库」那一页上。"""
        if seq != getattr(self, '_online_seq', 0) or self._pick_mode != 'online':
            return                               # 又点了一次刷新 / 面板已经关了
        fill = getattr(self, '_online_fill', None)
        if fill is None or self.pick_list is None or self.pick_status is None:
            return
        if songs:
            fill(songs, '共 %d 首，本地已经有 %d 首。（%s）'
                 % (len(songs), len(library.installed_songs()),
                    library.SITE_LABELS.get(library.source_name(), '')))
            self.pick_status.setText('')
            return
        cached, _when = library.cached_index()
        other = (library.SITE_GITHUB if library.source_name() == library.SITE_GITEE
                 else library.SITE_GITEE)
        tip = '连不上就点上面的「%s」换一套曲库试试。' % library.SITE_LABELS.get(other)
        if cached:
            fill(cached, '这次没连上（%s）。下面是上次拉到的歌单，下过的还能直接用。' % why)
            self.pick_status.setText(tip)
        else:
            fill([], '没能拉到歌单：%s' % why)
            if library.repo_of()[0]:
                self.pick_status.setText('%s\n这个仓库里没有 midi，或者这台机器连不上它。\n'
                                         '（仓库里只放 midi 就行，不用额外准备索引文件；\n'
                                         '地址想换一个就写进 %s）'
                                         % (tip, library.source_file()))
            else:
                self.pick_status.setText('设置方法：把曲库地址写进 %s（一行地址就行），\n'
                                         '或者填在 library_source.py 里再重新打包。'
                                         % library.source_file())

    def _on_online_downloaded(self, path, why):
        """联网曲库那首下完了（主线程）。下成就直接读进来，失败就在列表里说一声。"""
        self._downloading = False
        if getattr(self, '_downloading_seq', 0) != getattr(self, '_online_seq', 0):
            # 下的时候已经把面板关了 / 又点了刷新：别突然把这首塞进来，
            # 文件已经在本地了，等会儿去「曲库 -> 已下载」里拿
            if path:
                self.log('联网曲库下好了：%s（收在「曲库 -> 已下载」里）' % path)
            return
        if path:
            self.log('联网曲库下好了：%s' % path)
            self.last_dir = os.path.dirname(path)
            self._close_picker()
            self.load(path)
            return
        self.log('联网曲库没下下来：%s' % why)
        if self.pick_status is not None and self._pick_mode == 'online':
            self.pick_status.setText('没下下来：%s' % why)
        if self.pick_ok is not None:
            self.pick_ok.setEnabled(True)


    def open_upload_dialog(self, url=''):
        """
        往曲库传曲子 / 重排索引。

        曲库是大家一起用的公开仓库，所以开头先把这件事说清楚：传上去就等于
        分享出去了，所有人都能下。按不按「上传」还是你说了算。

        曲子会**两套曲库都传**（国内 Gitee + GitHub）—— 传一次两边都有。
        Gitee / GitHub 的令牌都是**内置在程序里**的（见 library.py /
        gitee_token_local.py / github_token_local.py），界面上没有这一项 ——
        打开就能传，不用填任何东西。
        """
        if self.in_game():
            self.log('上传要开窗口填标题、挑文件，回桌面再点（游戏里弹窗会把你顶出全屏）')
            return
        # 中转（老版本才有）只认 GitHub，所以仓库坐标从 GitHub 那套拿
        owner, repo, branch, why = library.source_repo(library.SITE_GITHUB)
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

        repos = []
        for key in library.SITE_ORDER:
            _owner, _repo, _branch, _why = library.source_repo(key)
            repos.append('%s　%s' % (library.SITE_LABELS.get(key, key),
                                     ('%s/%s @%s' % (_owner, _repo, _branch)) if _owner
                                     else (_why or '认不出仓库')))
        where = QLabel('上传到：\n  ' + '\n  '.join(repos))
        where.setObjectName('hint')
        where.setWordWrap(True)
        layout.addWidget(where)

        # 走中转还是走内置令牌：这里说清楚，省得用户以为「要填令牌才能传」
        via = QLabel(relay.describe() if relay.has_url() else
                     '上传通道：程序里内置的令牌（Gitee / GitHub 各一份）\n'
                     '曲子两套曲库都传 —— 国内用户走 Gitee，海外 / Gitee 挂了还有 GitHub。')
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
            上传一首曲子：配了中转就走中转（老版本才有，只到 GitHub），
            没配就把两套曲库都传一遍（Gitee + GitHub）。
            """
            if relay.has_url():
                return relay.upload(path, title, artist, owner, repo, branch)
            return library.upload_song_all(path, title, artist)

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
            path = self._upload_path
            if not path or not os.path.isfile(path):
                status.setText('先选一个 midi 文件')
                return
            busy('正在上传 %s（两套曲库都传）…' % os.path.basename(path))
            told, bad = upload_one(path, title_edit.text().strip(),
                                   artist_edit.text().strip())
            if bad:
                status.setText('没传成：%s' % bad)
                self.log('上传到曲库失败：%s' % bad)
                return
            status.setText('%s\n（索引可能要过一两分钟才在镜像上生效。）' % told)
            self.log('上传到曲库：%s' % told)

        def do_reindex(_checked=False):
            busy('正在重排索引…')
            if relay.has_url():
                told, bad = relay.reindex(owner, repo, branch)
            else:
                told, bad = library.refresh_index_all()
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
        """
        打开「曲库」：内置的那份 + 联网曲库下载下来的，都在浮层列表里。

        两边合在一起是有意的：联网曲库下完一首，点「曲库」就能看见它、直接再放一遍，
        不用去别的地方翻。列表上面有「内置曲库」「已下载」两个快捷入口；桌面上的系统
        文件框也没丢，换成了列表里的「系统文件框…」按钮。
        """
        builtin = library_songs()
        downloaded = library.downloaded_files()
        if not builtin and not downloaded:
            self.log('没找到曲库（内置的 %s 是空的，也没下载过曲子）' % SONG_DIR)
            return
        self.log('曲库：内置 %d 首，联网曲库下载的 %d 首'
                 % (len(builtin), len(downloaded)))
        last = getattr(self, 'last_dir', '') or ''
        self.open_picker(last if os.path.isdir(last) else SONG_DIR)

    def open_editor(self):
        """
        打开简谱编辑器：直接开一个**独立窗口**，跟主界面平级。

        独立窗口是有意的 —— 它是个正常窗口：能最小化、不置顶，浮层那套「不接受焦点」
        也管不到它，所以键盘（空格播放 / 暂停、Ctrl+Z、Ctrl+S…）都正常。
        """
        if not edition.has_editor():
            self.log('这一版没带简谱编辑器（精简版不含编辑器）；要改谱请用完全版')
            return
        if self.in_game():
            self.log('游戏里先不开编辑器：它会开一个新窗口，可能把游戏顶回桌面；回桌面再点')
            return
        window = self._editor_window()
        if window is None:
            return
        # 编辑器里已经是这首了就别再读一遍 —— 会把这半天改的东西冲掉。
        # 换了别的曲子才重新铺（旧那份改过的话，自动保存已经留了底）。
        same = False
        try:
            same = bool(self.midi_path) and bool(window.score.path) and (
                os.path.normcase(os.path.abspath(self.midi_path))
                == os.path.normcase(os.path.abspath(window.score.path)))
        except Exception:
            same = False
        if self.midi_path and not same:
            if window.score.notes and window.score.dirty:
                self.log('编辑器里那份（%s）有没保存的改动，自动保存留了底；现在换成当前这首'
                         % os.path.basename(window.score.path or '未命名'))
            window.open_path(self.midi_path, ask=False)
        self._show_editor_window(window)
        log_event('打开简谱编辑器窗口')

    def _show_editor_window(self, window):
        """把编辑器窗口摆到前面（普通窗口：不置顶、能最小化）。"""
        window.show()
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
        window.raise_()
        window.activateWindow()

    def _editor_window(self):
        """
        「主」编辑器窗口：还没有就建一个，已经有了就直接用。

        点「简谱编辑器…」、打开工程、录完音改谱都往这个窗口里塞；双击别的工程文件
        会另开窗口（见 open_project_window），互不打扰。
        """
        if editor is None or not edition.has_editor():
            return None
        window = getattr(self, 'editor', None)
        if window is not None:
            try:
                window.windowTitle()          # 碰一下：底层的窗口还在吗
                return window
            except RuntimeError:
                self.editor = None            # 已经关掉了，重新建一个
        self._building_editor = True
        try:
            window = editor.EditorWindow()
            window.setWindowIcon(make_icon())
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            window.destroyed.connect(lambda _obj=None, w=window: self._forget_editor_window(w))
            window.on_export = self._on_editor_export
            self.editor = window
            self.editor_windows.append(window)
        except Exception as exc:
            self.log('开编辑器窗口失败：%s' % exc)
            log_crash(traceback.format_exc())
            return None
        finally:
            self._building_editor = False
        return window

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
        """打开一个简谱工程文件：开个（或复用）编辑器窗口把工程铺上。"""
        if not edition.has_editor() or editor is None:
            self.log('这是个简谱工程文件（%s），但精简版不带编辑器；要改谱请用完全版'
                     % os.path.basename(path))
            return False
        log_event('打开工程文件：%s' % os.path.basename(path))
        window = self._editor_window()
        if window is None:
            return False
        # 先打开、再亮窗口：工程读不动（文件坏了 / 被删了）就别把空窗口亮出来
        if not window.open_project(path):
            return False
        if not self.in_game():
            self._show_editor_window(window)
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
        """把自动保存的那一份摆进「主」编辑器窗口（没有就开一个）。"""
        try:
            window = self._editor_window()
            if window is None:
                return False
            if not window.open_project(path, unsaved=True):
                return False
        except Exception as exc:
            self.log('自动保存打开失败：%s' % exc)
            log_crash(traceback.format_exc())
            return False
        if not self.in_game():
            self._show_editor_window(window)
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
        if getattr(self, 'editor', None) is window:
            self.editor = None

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
            self._close_picker()      # 上次没收干净（比如直接收起了浮层），先收拾掉再开
        self._hide_info_panel()       # 公告 / 更新那页开着就先收掉，别叠在一起
        self._pick_mode = 'file'
        self._borrow_keyboard(not self.in_game())   # 桌面上让窗口能接收键盘，搜索框才打得进去
        panel = QWidget(self.pick_panel)
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        for text, slot in (('上一级', self._pick_up), ('内置曲库', self._pick_songs),
                           ('已下载', self._pick_downloaded), ('桌面', self._pick_desktop),
                           ('刷新', self._pick_refresh),
                           ('打开文件夹', self._pick_open_folder)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        if not self.in_game():
            # 桌面上留个老办法：系统文件框能打字、能粘路径，用不惯列表的还能走这条
            browse = QPushButton('系统文件框…')
            browse.setToolTip('弹系统的选文件框（能打字、能粘路径）')
            browse.clicked.connect(self._pick_browse_dialog)
            bar.addWidget(browse)
        self.pick_path = QLabel('')
        self.pick_path.setObjectName('fieldLabel')
        bar.addWidget(self.pick_path, 1)
        box.addLayout(bar)

        self.pick_list = QListWidget()
        self.pick_list.itemDoubleClicked.connect(self._pick_open)
        self.pick_list.currentItemChanged.connect(self._sync_pick_open)
        self._add_search_row(box, self.pick_list, '搜索这个文件夹里的文件（命中的会高亮）')
        box.addWidget(self.pick_list, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.pick_hint = QLabel('')
        self.pick_hint.setObjectName('hint')
        foot.addWidget(self.pick_hint, 1)
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
        if self._choosing is None or self._pick_mode != 'file':
            return
        self._pick_hint_saved = False          # 换了文件夹，搜索栏那句「命中 N 个」重新算
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
            self.pick_hint.setText('双击文件夹进去；MIDI 直接读，蓝色的音频会先转成单音 MIDI。'
                                   '上面「内置曲库」「已下载」能一键跳到自带的曲库和联网曲库下好的曲子')
        if getattr(self, '_pick_search_apply', None) is not None:
            self._pick_search_apply()          # 搜索框里还留着字就照着再筛一遍

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

    def _pick_songs(self):
        """跳到内置曲库（exe 旁边那份）。"""
        self._pick_enter(SONG_DIR)

    def _pick_downloaded(self):
        """跳到联网曲库下载下来的地方（%LOCALAPPDATA%\\AutoPlay\\library）。"""
        folder = library.downloaded_root()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        self._pick_enter(folder)
        if self.pick_list is not None and self.pick_list.count() == 0:
            self.pick_hint.setText('这里还没有下载过曲子。去「联网曲库」下一首，或者把本机的 '
                                   'MIDI 直接复制进 %s 再来刷新。' % folder)

    def _pick_browse_dialog(self):
        """桌面上的老办法：弹系统文件框挑一个。"""
        path, _ = QFileDialog.getOpenFileName(self, '打开曲子',
                                              self._pick_dir or self.last_dir, MIDI_FILTER)
        if path:
            self._on_file_chosen(path)

    def _pick_desktop(self):
        self._pick_enter(os.path.join(os.path.expanduser('~'), 'Desktop'))

    def _pick_refresh(self):
        self._pick_enter(self._pick_dir)

    def _pick_open_folder(self):
        """
        在资源管理器里打开当前这个文件夹 —— 方便直接把本机的曲子复制进曲库，
        或者把下载好的曲子从缓存里拷出来。
        """
        folder = self._pick_dir or self.last_dir
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
            self.log('文件夹已打开：%s' % folder)
        except Exception as exc:
            self.log('打不开文件夹（%s）：%s' % (folder, exc))

    def _grow_for_picker(self):
        """
        文件列表嵌在主界面里，太矮了不好挑文件，先把它撑高一点。

        撑高之后必须保证整个窗口还在屏幕里：屏幕矮的时候原来会顶出屏幕上边，
        标题栏跑出去、窗口就拖不回来了。所以上限按屏幕算，超出就把窗口整体上移。
        """
        try:
            self._size_before_pick = self.size()
            area = self._screen_area()
            height = max(self.height(), PICKER_H)
            if area is not None:
                height = min(height, area.height() - 60)
            self.resize(self.width(), height)
            self._keep_on_screen()
        except Exception:
            pass

    def _close_picker(self):
        """收起文件列表，把界面还原。"""
        # 拉歌单 / 下载还在后台跑的话，让它们回来时认不出这局面（见 _on_online_index）
        self._online_seq = getattr(self, '_online_seq', 0) + 1
        self._online_fill = None
        panel, self._choosing = self._choosing, None
        if panel is not None:
            self.pick_layout.removeWidget(panel)
            panel.setParent(None)
            panel.deleteLater()
        self.pick_list = None
        self.pick_path = None
        self.pick_hint = None
        self.pick_ok = None
        self.pick_status = None
        self._pick_mode = 'file'
        self._borrow_keyboard(False)      # 把键盘还回浮层（不抢焦点、仍然置顶）
        self.pick_panel.setVisible(False)
        # 按用户的设置还原（默认是关着的）—— 别一收面板就把控制台顶出来
        self.log_view.setVisible(getattr(self, '_log_visible', LOG_VISIBLE_DEFAULT))
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

    # ---------- 拖文件进窗口：导入本地曲库 / 自动转谱 ----------

    def eventFilter(self, obj, event):
        """
        把 midi / 音频拖进窗口就算导入（松手才动手）。

        拖拽事件会先落到鼠标底下那个子控件（列表、输入框…）上，所以挂一个应用级
        过滤器统一接住 —— 只认「属于本窗口、拖的又是本地文件」的那种，别管别的窗口。
        """
        kind = event.type()
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            try:
                inside = isinstance(obj, QWidget) and obj.window() is self
                if inside and self._drop_urls(event):
                    if kind == QEvent.Type.Drop:
                        event.acceptProposedAction()
                        self._import_dropped(
                            [url.toLocalFile() for url in self._drop_urls(event)])
                        return True
                    event.acceptProposedAction()
                    return True
            except Exception as exc:
                log_event('拖拽事件处理出错：%s' % exc)
        return super().eventFilter(obj, event)

    @staticmethod
    def _drop_urls(event):
        """这次拖拽里的本地文件（不是本地文件的丢掉）。"""
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        return [url for url in mime.urls() if url.isLocalFile()]

    @staticmethod
    def _unique_path(path):
        """目标文件重名就加序号，绝不覆盖用户已有的曲子。"""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        for index in range(1, 1000):
            candidate = '%s (%d)%s' % (base, index, ext)
            if not os.path.exists(candidate):
                return candidate
        return path

    def _import_dropped(self, paths):
        """
        松手之后：midi 复制进本地曲库；音频自动开转（转出来的 midi 也放曲库）；
        认不出的格式说一声。
        """
        folder = library.downloaded_root()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        files = [path for path in (paths or []) if os.path.isfile(path)]
        if not files:
            self.log('拖进来的不是文件（文件夹 / 网页链接之类）；拖 midi、mp3 这类文件才行')
            return
        midis, audios, others = [], [], []
        for path in files:
            low = path.lower()
            if low.endswith(MIDI_SUFFIX):
                midis.append(path)
            elif low.endswith(AUDIO_SUFFIX):
                audios.append(path)
            else:
                others.append(path)
        moved = 0
        for path in midis:
            dest = self._unique_path(os.path.join(folder, os.path.basename(path)))
            try:
                shutil.copy2(path, dest)
                moved += 1
                self.log('拖进来的 midi 已放进曲库：%s' % dest)
            except Exception as exc:
                self.log('复制失败（%s）：%s' % (os.path.basename(path), exc))
        if audios and (audio2midi is None or not edition.has_audio()):
            self.log('拖进来 %d 个音频，但这一版没带「音频转 MIDI」（精简版不含转谱）'
                     % len(audios))
            audios = []
        for path in audios:
            base = os.path.splitext(os.path.basename(path))[0] + '.mid'
            self._audio_queue.append((path, self._unique_path(os.path.join(folder, base))))
        if others:
            self.log('这些文件不认识，跳过了：%s'
                     % '、'.join(os.path.basename(path) for path in others[:5]))
        if moved:
            self.set_status('已导入 %d 首' % moved, 'ready')
            self._refresh_downloaded()
        if self._audio_queue and not self._converting:
            self._convert_next_queued()

    def _refresh_downloaded(self):
        """「曲库 -> 已下载」列表正开着的话刷一下，刚导入的才看得到。"""
        try:
            if self.pick_list is not None and self._pick_dir == library.downloaded_root():
                self._pick_enter(self._pick_dir)
        except Exception:
            pass

    def _convert_next_queued(self):
        """一次拖进来好几段音频：排队一段一段转（convert_audio 一次只干一个）。"""
        if self._converting or not self._audio_queue:
            return
        path, out = self._audio_queue.pop(0)
        self.log('开始转换拖进来的音频：%s' % os.path.basename(path))
        if not self.convert_audio(path, out=out):
            self._convert_next_queued()

    # ---------- 音频转 MIDI ----------

    def convert_audio(self, path, out=None):
        """
        音频 -> 单音 MIDI（后台线程，转完自动接着读）。

        一首歌要转几十秒，放主线程界面会卡死，所以丢给线程；转完用 audio_done
        信号回到主线程再 load()。

        同音重复切多细由界面上那个滑块（self._repeat_level）决定；顺手记下音频路径，
        好让「应用」按钮之后拿它重转。

        out 是转出来的 midi 存哪儿（拖进来的音频要放进曲库）；None = 跟音频同目录。
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
        if out is not None:
            self._audio_out = out
        elif path != getattr(self, '_audio_source', None):
            self._audio_out = None           # 新选的音频：默认跟音频放一起
        self._audio_source = path            # 「应用」以后拿它重转
        self.btn_audio.setEnabled(False)
        if self.repeat_apply is not None:
            self.repeat_apply.setEnabled(False)
        self.set_status('转换音频中', 'play')

        def work():
            stage = '探测后端'
            detail = ''
            try:
                # 探测后端要真去 import（basic-pitch 那一串装齐了得一两秒），
                # 挪到后台线程里算，别把界面卡住
                probe = audio2midi.backend_probe()
                usable = [name for name, info in probe.items() if info['ok']]
                self.message.emit('把音频转成 MIDI：%s（能用的后端：%s）'
                                  % (os.path.basename(path), '、'.join(usable)))
                # 打包版该自带 basic-pitch，缺了就是打包 / 运行库的问题：报一条
                if getattr(sys, 'frozen', False) and not probe.get('basic-pitch', {}).get('ok'):
                    report_backend_missing(probe)
                level_name, extra = audio2midi.repeat_level(self._repeat_level)
                detail = level_name
                if extra:
                    joined = '，'.join('%s=%s' % item for item in sorted(extra.items()))
                    detail = '%s（%s）' % (level_name, joined)
                    self.message.emit('同音重复敏感度：%s（%s）' % (level_name, joined))
                stage = '转换'
                out = audio2midi.convert(
                    path, out=self._audio_out, backend='auto',
                    progress=lambda text: self.message.emit(text), **extra)
            except Exception as exc:
                report_convert_failure(path, exc, stage, detail)
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
        if self.repeat_apply is not None:
            self.repeat_apply.setEnabled(self._audio_source is not None)
        if error or not midi_path:
            self.log('音频转 MIDI 失败：%s' % (error or '没生成文件'))
            if errorreport.enabled():
                self.log('这次失败已经自动上报给开发者了（设置里可以关）。')
            if getattr(sys, 'frozen', False):
                self.log('小贴士：打包版自带 basic-pitch，正常不该走到这儿 —— 把日志发出来看看。'
                         '实在不行就先用能读的 midi，或者退回单声部音频（自带 YIN 只认单声部）。')
            else:
                self.log('小贴士：装了 basic-pitch 才能转有伴奏 / 编曲的歌（自带的 YIN 只认单声部）。'
                         '安装见 mp3midi/README.md，国内用清华镜像：'
                         'pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-deps basic-pitch')
            self.set_status('转换失败', 'error')
            self._convert_next_queued()
            return
        if not self.isVisible():
            self.show_window(in_game=self.in_game())
        self.log('音频转好了：%s' % os.path.basename(midi_path))
        self.load(midi_path)
        self.log('按「♪ 试听」听听主旋律抓对没有；不对就换个后端，或者先把人声 / 主旋律分离出来再转')
        self._convert_next_queued()

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
        self._preview_seek = 0.0
        self._preview_total = preview.total_seconds(events)
        self._set_preview_button(True)
        self.preview_slider.setEnabled(False)      # 合成完才让拨
        self._set_preview_position(0.0)
        self.log('试听：%s（%d 个音，%s× 速度）'
                 % (os.path.basename(self.score_path or ''), len(events), self._speed()))
        log_event('试听：%s' % os.path.basename(self.score_path or ''))
        self._start_preview_render()

    def _start_preview_render(self, seek=None):
        """
        按当前倍速把谱面缩放一遍，交给后台线程合成。

        速度跟演奏用的是同一套换算（player.Player.speed_up：起点和时值一起乘 1/速度），
        所以试听听到的快慢跟真正演奏出来的一致。

        seek 是合成完从第几秒开始放（换速度重合成时接着放）；不传就沿用上一次记下的。
        """
        if not self._previewing or self.analysis is None:
            return
        if seek is not None:
            self._preview_seek = max(float(seek), 0.0)
        self._preview_speed = self._speed()
        events = player.Player.speed_up(list(self.score_events), self._preview_speed)
        self._preview_total = preview.total_seconds(events)
        self.preview_slider.setEnabled(False)
        self.set_status('试听准备中', 'play')
        self._preview_token = getattr(self, '_preview_token', 0) + 1
        threading.Thread(target=self._render_preview,
                         args=(events, self.analysis.tonic, self._preview_token),
                         daemon=True).start()

    def _render_preview(self, events, tonic, token=0):
        """
        后台线程：合成整首，合成完用 preview_ready 信号回到主线程再播。

        token 是这次的合成序号：合成期间用户又换了速度（重开了一次合成）的话，这一份
        已经作废，结果直接丢掉 —— 不然两次合成回来的顺序一乱，放的会是旧速度那份。
        """
        try:
            self.previewer.render(events, tonic)
        except Exception as exc:
            if token == getattr(self, '_preview_token', 0):
                self.preview_ready.emit(str(exc))
            return
        if token == getattr(self, '_preview_token', 0):
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
        if not self._start_preview_at(max(getattr(self, '_preview_seek', 0.0), 0.0)):
            self.log('试听播放不了（音频可能是空的）')
            self.stop_preview()
            return
        self._preview_tick()

    def on_speed_changed(self, _text=None):
        """
        界面上换了倍速：试听也跟着换（演奏是开始的时侯读速度；试听是合成好的音频，
        不重合成还是老速度）。

        正在听的话从**同一个乐句位置**接着放：旧时间轴位置 -> 谱面原速 -> 新时间轴。
        """
        if not getattr(self, '_previewing', False) or self.analysis is None:
            return
        old = float(getattr(self, '_preview_speed', 1.0) or 1.0)
        new = self._speed()
        if abs(new - old) < 1e-9:
            return
        elapsed = 0.0
        if self._preview_t0 is not None:
            elapsed = self._preview_base + (time.time() - self._preview_t0)
        position = elapsed * old / new
        try:                               # 先把这一段停了，免得新旧两份声音叠在一起
            self.previewer.stop()
        except Exception:
            pass
        try:
            self.preview_timer.stop()
        except Exception:
            pass
        self._preview_t0 = None
        self._preview_path = None
        self.log('试听跟着换速度：%s×，从 %s 接着放'
                 % (new, preview.format_time(position)))
        self._start_preview_render(seek=position)

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
        self._preview_token = getattr(self, '_preview_token', 0) + 1   # 还在合成的作废
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
        if self.overlay_on:
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
        """录完直接摆进编辑器窗口，有瑕疵可以当场拖。"""
        if editor is None or not edition.has_editor() or self.in_game():
            # 游戏里不开新窗口（会把游戏顶回桌面）；录的东西已经存成 midi 了，
            # 回桌面点「简谱编辑器…」再打开它照样能改。
            return
        try:
            window = self._editor_window()
            if window is None:
                return
            notes = [editor.Note(start, dur, pitch)
                     for start, dur, pitch in recorder.pairs_to_notes(pairs, tonic)]
            score = editor.Score(notes, tonic=tonic, bpm=REC_BPM, path=midi_path,
                                 track_index=-1, track_name='录音')
            score.dirty = True                  # 刚录的还没存过：让自动保存盯着它
            window.set_score(score, '刚录的：%d 个音，共 %.1f 秒。哪里不对直接拖，'
                                         '改完按「导出 MIDI」会自动载回主程序。'
                                         % (len(notes), total))
        except Exception as exc:
            self.log('编辑器没打开：%s' % exc)
            return
        self._show_editor_window(window)
        self.log('已经打开简谱编辑器窗口，可以改这段录音')

    # ---------- 演奏 ----------

    def _playing(self):
        """正在演奏（或者正在跟奏练习）都算「忙」，这时候不给换文件 / 换音轨。"""
        return (self.practice or self.rhythm_run
                or (self.worker is not None and self.worker.is_alive()))

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
        if self.follow_on and self._rhythm_pace():
            self.start_rhythm()
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
        if self.overlay_on:
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

    def _rhythm_pace(self):
        """界面上的「跟奏节奏」是不是选在音游模式。"""
        return (self.follow_pace is not None
                and self.follow_pace.currentText() == follow.RHYTHM_PACE)

    def start_rhythm(self):
        """
        音游模式：原速下落、程序一个键都不发，按对得分（判定 / 计分见 rhythm.py）。

        跟练习模式的区别：练习模式是你按对了才往下走；音游模式按原曲速度自己走，
        漏了就漏了，最后结算分数和星星。
        """
        window = self._ensure_follow()
        if window is None:
            self.log('跟奏窗口用不了（这个版本没带跟奏，或者窗口创建失败）')
            return
        self.stop_preview()
        if self.follow_box is not None and not self.follow_box.isChecked():
            self.follow_box.setChecked(True)
        window.on_progress = self._on_practice_progress
        window.on_finish = self._on_practice_finish
        window.on_result = self._on_rhythm_result
        # 音游按谱面原时值走（倍速滑块不影响它），音长设置照旧生效
        window.set_score(self._shaped_events(), 1.0)
        self.rhythm_run = True
        self._rhythm_result = None
        self._rhythm_shown = False
        self.bar.setRange(0, max(len(self.score_events), 1))
        self.bar.setValue(0)
        self.counter.setText('0 / %d' % len(self.score_events))
        self.btn_stop.setEnabled(True)
        self.btn_choose.setEnabled(False)
        self.btn_track.setEnabled(False)
        self.track_box.setEnabled(False)
        self.set_status('音游中', 'play')
        self.btn_preview.setEnabled(False)
        if self.overlay_on:
            self.overlay.begin(len(self.score_events))
        self._step_aside()
        window.begin_rhythm()
        self.log('音游模式开始：3、2、1 之后按原速下落 —— 琴键和鼠标都按对才算，'
                 '漏了算 miss（F7 暂停，F8 停止）')
        log_event('音游模式开始：%s' % os.path.basename(self.score_path or ''))

    def _on_rhythm_result(self, summary):
        """音游跑完了：把结算窗口开出来。"""
        if self._rhythm_shown:
            return                            # 一把只结算一次（防重入）
        self._rhythm_result = dict(summary or {})
        self._show_rhythm_result()

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
        if self.overlay_on:
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
        """练习 / 音游结束：自己走完了，或者中途按了 F8。"""
        if not (self.practice or self.rhythm_run):
            return
        was_rhythm = self.rhythm_run
        self.practice = False
        self.rhythm_run = False
        self.btn_stop.setEnabled(False)
        self.btn_choose.setEnabled(True)
        self.btn_preview.setEnabled(self.analysis is not None)
        self.track_box.setEnabled(True)
        self.btn_track.setEnabled(self.analysis is not None and len(self.analysis.tracks) > 1)
        self.set_status('已就绪', 'ready')
        self.overlay.finish('done' if reason == 'done' else 'stop')
        if was_rhythm:
            self.log('音游结束' + ('：整首走完了' if reason == 'done' else '（中途停止）'))
            log_event('音游结束（%s）' % ('走完' if reason == 'done' else '中途停止'))
            if reason == 'done' and self._rhythm_result:
                self._show_rhythm_result()
        else:
            self.log('跟奏练习结束' + ('：整首按完了' if reason == 'done' else '（中途停止）'))
            log_event('跟奏练习结束（%s）' % ('按完' if reason == 'done' else '中途停止'))

    def _show_rhythm_result(self):
        """音游结算窗口（懒创建），把这一把的成绩铺进去。"""
        if not self._rhythm_result or self._rhythm_shown:
            return
        window = self._result_window
        if window is None:
            window = RhythmResultWindow()
            window.name_saved.connect(self._remember_rhythm_name)
            self._result_window = window
        self._rhythm_shown = True
        # 不再拿上次记住的名字预填（用户要求：名字每次都自己填）
        window.show_result(rhythm.song_key(self.score_path), self._rhythm_result)
        self._present_popup(window)

    def _remember_rhythm_name(self, name):
        """记住用户填的名字，下次结算直接填好。"""
        name = rhythm.clean_name(name)
        if not name:
            return
        try:
            self.settings.setValue('rhythm_name', name)
        except Exception:
            pass

    def open_manual(self, force=False):
        """
        「快速上手」：开一个独立窗口显示手册。

        先把手上这份（上次拉到的 / 程序内置的）显示出来，再去后台拉最新的 ——
        联网慢也不至于让窗口卡着不出来。
        """
        window = self._manual_window
        if window is None:
            window = InfoWindow()
            self._manual_window = window
        body = self._manual_body or manual.BUILTIN
        window.set_markdown(manual.TITLE, body, self._manual_actions(window))
        self._present_popup(window)
        self.fetch_manual()

    def _manual_actions(self, window):
        return [('重新拉取', lambda: self.fetch_manual(True), False),
                ('关闭', window.hide, False)]

    def fetch_manual(self, force=False):
        """后台拉手册（跟公告一个路子：拉不到就用自带那份，绝不挡界面）。"""
        if getattr(self, '_manual_busy', False) and not force:
            return
        self._manual_busy = True

        def work():
            try:
                body, note = manual.body_and_note()
            except Exception as exc:
                body, note = manual.BUILTIN, '拉手册出错：%s' % exc
            self.manual_ready.emit(body, note)

        threading.Thread(target=work, daemon=True).start()

    def _on_manual_ready(self, body, note):
        self._manual_busy = False
        if body:
            self._manual_body = body
        window = self._manual_window
        if window is None or not window.isVisible():
            return
        text = ('%s\n\n%s' % (note, body)) if note else body
        window.set_markdown(manual.TITLE, text, self._manual_actions(window))

    def quit_app(self, reason=''):
        """
        退出：该收的收掉，然后直接 os._exit 结束进程。

        不调用 QApplication.quit() 是有意的——Qt 拆窗口 / Python 拆解释器的过程最容易
        崩（崩溃日志里那几次 0xc0000005 就落在 pyside6 里），反正程序本来就要退了，
        干脆跳过整个析构过程，走得干净。
        """
        for window in list(self.editor_windows):
            try:
                if not window.ask_save():
                    return False          # 编辑器窗口里有没保存的改动，用户反悔了
            except RuntimeError:
                continue                  # 这个窗口已经关掉了
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
    errorreport.set_app_info(APP_VERSION, edition.LITE, app_dir())
    threading.excepthook = _thread_excepthook     # 后台线程崩了也记日志 + 上报

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
    if is_admin():
        # 提了权就收不到资源管理器拖过来的文件（Windows 的完整性级别拦的，见
        # allow_lower_privilege_drop）：先记一笔，用户问「拖拽没反应」时有据可查。
        log_event('以管理员身份运行：Windows 会拦住从资源管理器拖进来的文件'
                  '（拖拽光标是个禁止的圆圈）。要拖拽就：① 把文件拖进「打开本地曲库文件夹」'
                  '打开的那个文件夹，或 ② 用普通权限启动本程序。')

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
