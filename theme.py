# -*- coding: utf-8 -*-
"""
配色主题（json）
================

一个主题 = 一个 json 文件，里面是「角色 -> 颜色」。程序启动时读两处：

    %LOCALAPPDATA%\\AutoPlay\\themes\\*.json     第一次运行会把预设写在这儿
    <exe 或源码>\\themes\\*.json                  便携版 / 自己放一套也行（同名覆盖）

预设那几套会在第一次运行时写出去，照着文件改任何一个都行：改完在托盘图标上
右键 →「配色主题」→「重新载入主题文件」，或者重启程序。

怎么生效的
----------
代码里那些写死的老色号（'#0f1219' 之类）都过一遍 ROLES 这张表换算成当前主题的
颜色（`c()` / `paint()`），所以换主题的时候**主界面、右上角进度浮窗、跟奏面板、
编辑器**会一起换，不用挨个地方改。角色名是语义化的：

    bg / panel / control / line      底色、卡片、控件、边框
    text / text_dim / text_faint     正文、次要文字、提示文字
    accent / accent_soft / ...       主色那一族（按钮、进度条、选中态）
    editor_note_text / editor_*      编辑器那一片
    status_play / status_error       状态胶囊那几个（含义固定，不跟着主题转）

音符那六种操作色是**单独一套**（json 里的 `notes`）：**每套主题的音符颜色都不一样**
—— 换主题的时候音符跟着换色，一眼就能看出「换过了」。但六种之间的区分度在每套里
都得保住：等长演奏时色块短得写不下 ↑ # ↓，颜色是唯一的线索。所以预设那六套的
十二个色号是**一个一个挑出来的**，不是拿基准整体转色相转的（整体转会出事：
「橘 / 橘红」那一对转到绿色区就撞成一个颜色了）。

`colors` 里 66 个角色各管哪儿、六种操作色怎么挑、预设是怎么算出来的、怎么自己做
一套并分享 —— 都在 `docs/主题格式.md` 里。

自己加一套
----------
拷一份 json 改名叫「我的主题.json」，把 name 改成想要的名字，颜色随便调。
颜色写 #rrggbb。
"""

import colorsys
import json
import os
import re
import sys
import tempfile

# 主题文件放哪儿（第一次运行会把预设写进去）
APP_FOLDER = 'AutoPlay'
THEME_SUFFIX = '.json'
# 预设配色自己也有版本号：以后改了预设（比如给音符换色调），老版本写出去的 json
# 版本号对不上就自动重写一遍。用户自己加 / 手改的文件没有这个字段，永远不动。
PRESET_REV = 2

# 基准配色 = 程序原来写死的那些颜色，角色名 -> 色号。
# 顺序也就是「同一个色号当多个角色用时，算哪个角色」的优先级（见 _hex_roles）。
BASE = {
    # —— 窗口 / 卡片 / 控件 ——
    'bg': '#0f1219',              # 窗口底色
    'panel': '#161a23',           # 卡片底
    'panel_line': '#232937',      # 卡片描边
    'control': '#1d2330',         # 按钮 / 下拉框 / 圆钮的底
    'control_hover': '#242c3c',
    'control_press': '#1a2029',
    'line': '#2b3345',            # 控件描边
    'log_bg': '#0c0f15',          # 运行日志那块的底
    'menu_bg': '#12161f',         # 下拉列表的底
    'row_hover': '#232b3a',
    'row_off': '#565e70',
    'text_row': '#cbd3e1',
    'spin': '#232a38',
    'spin_hover': '#2c3547',
    'track': '#1b2130',           # 进度条凹槽
    'track_off': '#171c26',
    'base': '#141922',
    # —— 文字 ——
    'text': '#e6e9ef',
    'text_strong': '#dfe4ee',
    'text_dim': '#8b93a7',
    'text_faint': '#6f7787',
    'text_off': '#5c6478',
    'text_icon': '#b9c1d1',
    'text_soft': '#c8d0de',
    'text_tab_hover': '#c9d2e2',
    'white': '#ffffff',
    # —— 主色（蓝）——
    'accent': '#3b82f6',
    'accent_hover': '#4b8ef8',
    'accent_deep': '#2f6fd0',
    'accent_soft': '#7fb0ff',
    'accent_pale': '#bcd8ff',
    'accent_ice': '#9ec9ff',
    'accent_bg': '#24405f',
    'accent_bg_hover': '#202a3c',
    'accent_bg_current': '#1d3555',
    'accent_text_current': '#cfe1ff',
    'accent_text_off': '#7d8ea0',
    'slider_handle': '#c8d2e2',
    'slider_handle_off': '#3b4457',
    'scroll_handle_hover': '#39435a',
    # —— 红（关闭 / 试听中 / 危险）——
    'close_hover': '#b0413e',
    'danger_bg': '#3a2226',
    'danger_line': '#6b2f33',
    'danger_text': '#f0a0a0',
    'danger_bg_hover': '#46282d',
    # —— 灰掉 ——
    'off_bg': '#171b24',
    'off_line': '#222836',
    # —— 状态胶囊：含义是固定的（绿=演奏中、红=出错），不跟主题转 ——
    'status_idle': '#9aa6ba',
    'status_play': '#5fd18b',
    'status_pause': '#e0b341',
    'status_error': '#f08a8a',
    'status_rec': '#e06c75',
    # —— 编辑器 ——
    'editor_bg': '#0d1017',
    'editor_lane': '#111620',
    'editor_lane_light': '#171d29',
    'editor_grid': '#1c2230',
    'editor_grid_bold': '#2a3450',
    'editor_roll_line': '#222b3d',
    'editor_key_dark': '#1a2030',
    'editor_key_light': '#c3ccdc',
    'editor_key_edge': '#39415a',
    'editor_ruler': '#11151d',
    'editor_note_text': '#0b0e14',
    'editor_cursor': '#ff6b6b',
    # —— 跟奏 ——
    'follow_idle_glow': '#9fc4ff',
    'follow_dim': '#5a6478',
}

# 六种「要按哪个鼠标键」对应的颜色：操作 -> (尾色, 头色)
NOTE_ROLES = {
    '': 'note_none',            # 什么都不按
    '#': 'note_sharp',          # 升半音（鼠标中键）
    'A': 'note_up',             # 升调（鼠标右键）
    '#A': 'note_up_sharp',      # 升调 + 升半音
    'B': 'note_down',           # 降调（鼠标左键）
    '#B': 'note_down_sharp',    # 降调 + 升半音
}
NOTE_ORDER = ('', '#', 'A', '#A', 'B', '#B')
BASE_NOTES = {
    '': ('#2f6fd0', '#6fb0ff'),
    '#': ('#17879b', '#4fd6ec'),
    'A': ('#c07a12', '#ffc247'),
    '#A': ('#bf4f22', '#ff8f5c'),
    'B': ('#6d4fc9', '#b39bff'),
    '#B': ('#b23f77', '#ff8fc0'),
}

DEFAULT_NAME = '深空蓝'

# 预设：界面那部分都是拿基准配色按色相旋转算出来的 —— 背景、控件、文字、主色一起
# 转，所以不会出现「紫底配蓝按钮」那种脏搭配。
#
#     hue / sat            界面（背景、控件、文字、主色）转多少度、饱和度乘多少
#     notes                十二个音符色号（六种操作 × 头尾两色），写了就用写的
#     note_hue / note_sat  没写 notes 时的偷懒办法：整体转多少度、饱和度乘多少
#
# **每套主题的音符颜色都不一样**（用户一眼能看出「换主题了」），所以每套的
# notes 都是单独挑的，不做整体旋转 —— 整体转色相容易让「橘 / 橘红」这一对在
# 绿色区撞成一个颜色。
PRESETS = {
    # 默认那套：音符色就是基准那六个（没写 notes 就是用它）
    '深空蓝': {'hue': 0.0, 'sat': 1.0,
               'note': '默认那套：深蓝底 + 蓝色主色，音符是原本那六色'},
    # 下面这几套的 notes 是**一个一个挑出来的**，不是拿基准转出来的 —— 整片转色相
    # 容易让「橙 / 橙红」那一对在绿色区撞在一起（看着像一个色）。每个色号的含义
    # 都写在后面了，照着改就行。
    '午夜紫': {
        'hue': 44.0, 'sat': 1.02,
        'note': '偏紫的夜色调，主色转成紫罗兰；音符整体也偏冷紫',
        'notes': {
            '':   ('#3f57e0', '#7f95ff'),    # 无操作：靛蓝
            '#':  ('#0f9bb0', '#4fdcea'),    # 升半音（中键）：青
            'A':  ('#d18a12', '#ffc65c'),    # 升调（右键）：琥珀
            '#A': ('#d24a3a', '#ff8c78'),    # 升调 + 升半音：朱红
            'B':  ('#8f4ad6', '#c79cff'),    # 降调（左键）：紫
            '#B': ('#c93a86', '#ff92c4'),    # 降调 + 升半音：品红
        }},
    '松林绿': {
        'hue': -74.0, 'sat': 0.96,
        'note': '墨绿底 + 青绿主色，看久了不累；音符是草木那一套',
        'notes': {
            '':   ('#1f9f6a', '#5fe0a8'),    # 无操作：草绿
            '#':  ('#2f9fc0', '#6fe0ee'),    # 升半音（中键）：青
            'A':  ('#c9761c', '#ffb35c'),    # 升调（右键）：琥珀
            '#A': ('#c03f4f', '#ff8090'),    # 升调 + 升半音：玫红
            'B':  ('#6a6ad0', '#a8a8ff'),    # 降调（左键）：蓝紫
            '#B': ('#b03f9a', '#ff92e0'),    # 降调 + 升半音：紫红
        }},
    '琥珀暖夜': {
        'hue': -192.0, 'sat': 1.0,
        'note': '暖色调：琥珀主色，晚上开着不刺眼；音符暖冷各一半，照样分得清',
        'notes': {
            '':   ('#2f8fb8', '#6fd0ef'),    # 无操作：天蓝
            '#':  ('#2f9c86', '#66dcc0'),    # 升半音（中键）：青绿
            'A':  ('#d08a1c', '#ffc451'),    # 升调（右键）：琥珀
            '#A': ('#cf5f2a', '#ff9a68'),    # 升调 + 升半音：橘红
            'B':  ('#8a5ad0', '#c3a3ff'),    # 降调（左键）：紫
            '#B': ('#c23f84', '#ff8fc2'),    # 降调 + 升半音：品红
        }},
    '绯红霓虹': {
        'hue': 104.0, 'sat': 1.14,
        'note': '霓虹感最强的一套，颜色浓；音符是六个互不相干的荧光色',
        'notes': {
            '':   ('#e0338f', '#ff7fd0'),    # 无操作：荧光粉
            '#':  ('#7a4ad6', '#c09cff'),    # 升半音（中键）：紫
            'A':  ('#e8c322', '#fff07a'),    # 升调（右键）：荧光黄
            '#A': ('#e0641f', '#ffa060'),    # 升调 + 升半音：荧光橘
            'B':  ('#2f8fe0', '#7fc4ff'),    # 降调（左键）：荧光蓝
            '#B': ('#35c46a', '#7ef0aa'),    # 降调 + 升半音：荧光绿
        }},
    '石墨灰': {
        'hue': -16.0, 'sat': 0.12,
        'note': '界面几乎无彩（低干扰），音符还是彩色的，只是压暗了一档',
        'notes': {
            '':   ('#4693b9', '#83ceeb'),    # 无操作：钢蓝
            '#':  ('#3f9c8f', '#79d6c8'),    # 升半音（中键）：青灰
            'A':  ('#c08a35', '#f0bd6f'),    # 升调（右键）：赭黄
            '#A': ('#bd5a3c', '#ea8f70'),    # 升调 + 升半音：铁锈红
            'B':  ('#6f6fc0', '#a9a9f0'),    # 降调（左键）：灰紫
            '#B': ('#a8558c', '#e592c0'),    # 降调 + 升半音：藕荷
        }},
}

# 界面里的老色号 -> 角色（同一个色号只归一个角色）
_hex_to_role = {}
for _role, _hexv in BASE.items():
    _hex_to_role.setdefault(_hexv.lower(), _role)

_HEX_RE = re.compile(r'#[0-9a-fA-F]{6}')

# 当前主题
_palette = dict(BASE)               # 角色 -> 色号
_map = {}                           # 老色号 -> 当前主题色号
_watchers = []
_current_name = DEFAULT_NAME
_available = {}                     # 名字 -> {'colors': {...}, 'notes': {...}, 'note': 说明}
_folder = ''


# ============ 生成预设 ============

def _hex_rgb(value):
    value = value.lstrip('#')
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rgb_hex(rgb):
    return '#%02x%02x%02x' % tuple(max(0, min(255, int(round(channel * 255)))) for channel in rgb)


def _rotate(value, degrees, sat=1.0):
    """按色相旋转一个色号；sat<1 就同时往灰里拉。"""
    red, green, blue = _hex_rgb(value)
    hue, light, saturation = colorsys.rgb_to_hls(red, green, blue)
    hue = (hue + degrees / 360.0) % 1.0
    saturation = max(0.0, min(1.0, saturation * sat))
    return _rgb_hex(colorsys.hls_to_rgb(hue, light, saturation))


def _build(spec, notes=None):
    """按预设生成一份完整配色。"""
    degrees = spec.get('hue', 0.0)
    sat = spec.get('sat', 1.0)
    note_hue = spec.get('note_hue', 0.0)     # 没写 notes 时的偷懒办法
    note_sat = spec.get('note_sat', 1.0)
    colors = {}
    for role, value in BASE.items():
        # 状态色不跟主题转：绿就是「在演奏」、红就是「出错」
        if role.startswith('status_') or degrees == 0.0 and sat == 1.0:
            colors[role] = value
        else:
            colors[role] = _rotate(value, degrees, sat)
    # 音符那六种操作色的来源，按优先级：预设写死的 notes -> 传进来的 -> 基准那六个。
    # 写死的就直接用（预设那几套都是这么来的）；否则按 note_hue / note_sat 转一下。
    source = spec.get('notes') or notes or BASE_NOTES
    out_notes = {}
    for key, pair in source.items():
        if not note_hue and note_sat == 1.0:
            out_notes[key] = tuple(str(v) for v in pair)     # 转 0 度：原样，别被来回换算磨掉
        else:
            out_notes[key] = tuple(_rotate(str(v), note_hue, note_sat) for v in pair)
    return {'colors': colors, 'notes': out_notes}


def _path_of(name):
    return os.path.join(_folder, name + THEME_SUFFIX)


def _read(path):
    """读一个主题文件；坏文件就当没有，别让程序起不来。"""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except Exception:
        return None, '读不了'
    if not isinstance(data, dict):
        return None, '内容不是一套主题'
    colors = data.get('colors')
    if not isinstance(colors, dict):
        return None, '缺 colors'
    name = str(data.get('name') or os.path.splitext(os.path.basename(path))[0]).strip()
    notes = {}
    raw_notes = data.get('notes') or {}
    if isinstance(raw_notes, dict):
        for key, pair in raw_notes.items():
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                notes[str(key)] = (str(pair[0]), str(pair[1]))
    return {'name': name, 'colors': {str(k): str(v) for k, v in colors.items()},
            'notes': notes, 'note': str(data.get('note') or '')}, ''


def _write(path, name, spec, note='', rev=None):
    built = _build(spec)
    data = {
        'name': name,
        'note': note or spec.get('note', ''),
        'colors': built['colors'],
        'notes': {key: list(pair) for key, pair in built['notes'].items()},
    }
    if rev is not None:
        data['preset_rev'] = int(rev)      # 「这是程序写的预设」，见 ensure_files
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
        return True
    except OSError:
        return False


def folders():
    """主题文件都从哪些目录读（后面的同名覆盖前面的）。"""
    out = []
    try:
        near = os.path.dirname(sys.executable if getattr(sys, 'frozen', False)
                               else os.path.abspath(__file__))
        out.append(os.path.join(near, 'themes'))
    except Exception:
        pass
    try:
        local = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
        out.append(os.path.join(local, APP_FOLDER, 'themes'))
    except Exception:
        pass
    return out


# 主题文件夹里的小本子：记着「这儿的预设是第几版写的」，见 ensure_files
MARK_NAME = 'presets.rev'


def _mark_path():
    return os.path.join(_folder, MARK_NAME)


def _mark_rev():
    """这个文件夹里的预设是第几版写出去的（没写过就是 -1）。"""
    try:
        with open(_mark_path(), 'r', encoding='utf-8') as handle:
            return int(handle.read().strip())
    except Exception:
        return -1


def _write_mark():
    try:
        os.makedirs(_folder, exist_ok=True)
        with open(_mark_path(), 'w', encoding='utf-8') as handle:
            handle.write('%d\n' % PRESET_REV)
    except OSError:
        pass


def ensure_files(folder=None):
    """
    把预设写成 json。

    已经有的不覆盖（用户改过就随他去）—— 但预设自己升级了（PRESET_REV 变了）会
    整个重写一遍：老版本写出去的预设得跟着更新，不然换主题时音符颜色还是旧的。
    自己新增的主题文件（不叫预设那六个名字）从头到尾都不会被动。
    """
    global _folder
    _folder = folder or _folder or (folders()[-1] if folders() else '')
    upgrade = _mark_rev() < PRESET_REV
    written = []
    for name, spec in PRESETS.items():
        path = _path_of(name)
        if os.path.isfile(path) and not upgrade:
            continue
        if _write(path, name, spec, rev=PRESET_REV):
            written.append(path)
    if written or upgrade:
        _write_mark()
    return written


def load_all():
    """
    把所有主题读进来（预设 + 文件）。返回 {名字: 主题}。

    文件里的主题能覆盖同名预设；文件坏了跳过并在 stderr 留一句，
    程序照常用别的主题跑起来。
    """
    global _available
    ensure_files()
    found = {}
    for name, spec in PRESETS.items():
        built = _build(spec)
        found[name] = {'name': name, 'colors': built['colors'], 'notes': built['notes'],
                       'note': spec.get('note', ''), 'builtin': True}
    for folder in folders():
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for filename in names:
            if not filename.lower().endswith(THEME_SUFFIX):
                continue
            data, why = _read(os.path.join(folder, filename))
            if data is None:
                continue
            key = data['name'] or os.path.splitext(filename)[0]
            if data['notes']:
                base = dict(BASE_NOTES)
                base.update(data['notes'])
                data['notes'] = base
            else:
                data['notes'] = dict(BASE_NOTES)
            found[key] = data
    _available = found
    return found


def names():
    """所有主题的名字（默认那套排最前）。"""
    have = list(_available) or list(PRESETS)
    out = [DEFAULT_NAME] if DEFAULT_NAME in have else []
    out += [name for name in have if name != DEFAULT_NAME]
    return out


def describe(name):
    """主题的一句话说明（没有就空）。"""
    data = _available.get(name)
    return (data or {}).get('note', '')


# ============ 用哪套 ============

def set_current(name):
    """换主题：把老色号 -> 新颜色的对照表算出来，然后通知订阅者重画。"""
    global _palette, _map, _current_name
    if name not in _available:
        name = DEFAULT_NAME if DEFAULT_NAME in _available else (names() or [DEFAULT_NAME])[0]
    data = _available.get(name) or {'colors': BASE, 'notes': BASE_NOTES}
    colors = dict(BASE)
    colors.update(data.get('colors') or {})
    _palette = colors
    _current_name = name
    _map = {}
    for hexv, role in _hex_to_role.items():
        value = colors.get(role)
        if value:
            _map[hexv] = str(value).lower()
    # 音符那六种：就地改 NOTE_COLORS 这个字典，编辑器 / 跟奏读的就是它
    notes = dict(BASE_NOTES)
    notes.update(data.get('notes') or {})
    for key in NOTE_ORDER:
        if key in notes:
            NOTE_COLORS[key] = tuple(str(v) for v in notes[key])
    for watcher in list(_watchers):
        try:
            watcher()
        except Exception:
            pass
    return _current_name


def current_name():
    return _current_name


def saved_name():
    """上次用的那套配色（记在 AutoPlay 的设置里；读不到就用默认那套）。"""
    try:
        try:
            from PySide6.QtCore import QSettings
        except ImportError:
            from PyQt6.QtCore import QSettings
        return str(QSettings('AutoPlay', 'AutoPlay').value('theme', DEFAULT_NAME))
    except Exception:
        return DEFAULT_NAME


def subscribe(callback):
    """主题一换就叫一下（给模块级的颜色常量用：它们不是每次画的时候现取的）。"""
    if callback not in _watchers:
        _watchers.append(callback)
    return callback


def palette():
    return dict(_palette)


def c(value):
    """
    老色号 / 角色名 -> 当前主题的颜色。

    c('#0f1219') -> 这个主题里的窗口底色       c('bg') -> 一样
    认不出来的原样返回，免得把不该动的东西染了。
    """
    if not isinstance(value, str):
        return value
    if value.startswith('#'):
        return _map.get(value.lower(), value)
    return _palette.get(value, value)


def color(role_or_hex):
    return c(role_or_hex)


def paint(text):
    """把一段文字（QSS 之类）里的老色号全换成当前主题的颜色。"""
    if not isinstance(text, str) or not _map:
        return text
    return _HEX_RE.sub(lambda m: _map.get(m.group(0).lower(), m.group(0)), text)


def note_color(key):
    """某个鼠标键组合 -> (尾色, 头色)。"""
    return NOTE_COLORS.get(key, NOTE_COLORS[''])


def note_symbol(key):
    """某个鼠标键组合 -> 色块上写的记号（跟谱面文件里写法一致）。"""
    return NOTE_SYMBOL.get(key, '')


# 六种操作色：做成就地改的字典，换主题时 set_current 会更新它，
# 编辑器 / 跟奏窗口直接读这个对象就行，不用自己去订阅。
NOTE_COLORS = {key: BASE_NOTES[key] for key in NOTE_ORDER}
NOTE_SYMBOL = {'': '', '#': '#', 'A': '↑', 'B': '↓', '#A': '#↑', '#B': '#↓'}