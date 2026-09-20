# -*- coding: utf-8 -*-
"""
简谱记号 & 谱面文本
====================

音 -> 记号
----------
以 tonic 为 1，用大调音阶写成 1 2 3 4 5 6 7 i（i = 高音 1，即 +12 半音）。
超出基础八度时用鼠标修饰键，修饰字母写在数字前面：

    E = 不按鼠标          E1 E2 E3 E4 E5 E6 E7 Ei      相对 tonic +0  ~ +12
    A = 鼠标右键（升调）   A1 A2 A3 A4 A5 A6 A7 Ai      相对 tonic +12 ~ +24
    B = 鼠标左键（降调）   B1 B2 B3 B4 B5 B6 B7 Bi      相对 tonic -12 ~ +0
    # = 鼠标中键（升半音），写在最前面，可以和 A / B 同时出现
                          #E2(+3)  #A2(+13)  #B5(-4)  #Bi(-11)

没有降半音，所以「比基础音低半音」一律写成低一级的音加 #（#E1 = +1）。
升调(A) 和 降调(B) 不会同时出现，所以每个音最多需要「一个升降调 + 升半音」两个修饰键。

所有记号都落在相对 tonic 的 [-12, +25] 半音范围内，超出的音会被折回八度
（fold，降八度 +12 / 升八度 -12），折回的数量由 midi_analyze 统计并提示。


谱面文本
--------
空格 / 换行分隔的「音:持续时间」，持续时间单位是秒：

    E1:0.950 E2:0.450 0:0.200 #E2:0.425

`0` 是休止符（这一段时间不按任何键）。这样某个音的起点时间就等于它前面所有
持续时间之和，节奏与 midi 完全一致。为兼容旧文件，解析时也接受三段式
「音:开始秒:持续时间」。


文件名
------
固定为  TONIC<tonic> <midi 文件名>.txt ，例如  TONIC71 1.txt
（前缀 TONIC 后面空一格，再接与 midi 同名的部分）。
多音轨文件手动指定音轨时，会在文件名后面补上 #t<音轨号>（例如 TONIC71 1#t2.txt），
免得同一首歌不同音轨的谱面互相覆盖。
"""

from collections import namedtuple
import os


# 大调音阶：相对 tonic 的半音数 -> 数字
DEGREE_BY_OFFSET = {0: '1', 2: '2', 4: '3', 5: '4', 7: '5', 9: '6', 11: '7', 12: 'i'}

# 上面那张表反过来：数字 -> 相对 tonic 的半音数（0~12）
OFFSET_BY_DEGREE = {digit: offset for offset, digit in DEGREE_BY_OFFSET.items()}

# 数字 -> 游戏按键
DIGIT_KEYS = {'1': 'z', '2': 'x', '3': 'c', '4': 'v', '5': 'b', '6': 'n', '7': 'm', 'i': ','}

# 修饰字母 -> 鼠标键
MOD_MOUSE = {'A': 'right', 'B': 'left', '#': 'middle'}

# 可表示的相对半音范围（E / A / B 三段合起来正好覆盖这个区间）
REL_MIN = -12
REL_MAX = 25

# E 段最多到 +13（#Ei），更远的音用 A 段修饰键更少
E_MAX = 13

REST_TOKEN = '0'

Token = namedtuple('Token', 'text sharp mod folded')


def fold(rel):
    """把一个相对半音数折进可表示范围 [-12, 25]。"""
    while rel < REL_MIN:
        rel += 12
    while rel > REL_MAX:
        rel -= 12
    return rel


def degree(offset):
    """offset(0~13) -> (数字, 是否需要升半音)。"""
    if offset in DEGREE_BY_OFFSET:
        return DEGREE_BY_OFFSET[offset], False
    return DEGREE_BY_OFFSET[offset - 1], True


def pitch_to_token(pitch, tonic):
    """
    MIDI 音高 -> 记号。

    返回 Token(text, sharp, mod, folded)：
        text   如 'E1' / '#E2' / 'A2' / '#Ai' / 'B5'
        sharp  是否需要升半音（鼠标中键）
        mod    是否需要升降调（鼠标左键 / 右键）
        folded 是否因为超出范围被折回八度
    """
    rel = pitch - tonic
    folded = rel < REL_MIN or rel > REL_MAX
    rel = fold(rel)
    if rel < 0:
        prefix, offset = 'B', rel + 12
    elif rel > E_MAX:
        prefix, offset = 'A', rel - 12
    else:
        prefix, offset = 'E', rel
    digit, sharp = degree(offset)
    return Token(('#' if sharp else '') + prefix + digit, sharp, prefix != 'E', folded)


def parse_token(text):
    """
    记号 -> (修饰键列表, 数字)。

    '#A2' -> (['#', 'A'], '2')      'E1' -> ([], '1')
    '#Bi' -> (['#', 'B'], 'i')      '0'  -> ([], '0')
    """
    mods = []
    digit = ''
    for ch in text:
        if ch == '#':
            if '#' not in mods:
                mods.append('#')
        elif ch in ('A', 'B') and not digit and ch not in mods:
            mods.append(ch)
        elif ch == 'E' and not digit:
            continue
        else:
            digit += ch
    return mods, digit


def is_rest(token):
    """是不是休止符（空记号也算）。"""
    _, digit = parse_token(token)
    return digit in ('', REST_TOKEN)


def token_to_rel(text):
    """
    记号 -> 相对 tonic 的半音数；休止符 / 看不懂的记号返回 None。

    'E1' -> 0    '#E2' -> 3    'A1' -> 12    'B7' -> -5    '#Bi' -> -11
    就是 pitch_to_token 的反操作：数字查表得 0~12，A / B 各 ±12，# 再 +1。
    """
    mods, digit = parse_token(text)
    offset = OFFSET_BY_DEGREE.get(digit)
    if offset is None:
        return None
    if 'A' in mods:
        offset += 12
    elif 'B' in mods:
        offset -= 12
    if '#' in mods:
        offset += 1
    return offset


def token_to_pitch(text, tonic):
    """记号 -> midi 音高；休止符 / 看不懂的记号返回 None。"""
    rel = token_to_rel(text)
    return None if rel is None else tonic + rel


def parse_score(text):
    """
    谱面文本 -> [(音, 开始秒, 持续时间)]。

    两段式「音:持续时间」按顺序累加起点，三段式「音:开始:持续时间」用写明的起点。
    解析不了的内容直接跳过（例如文件名里混进来的 TONIC71）。
    """
    events = []
    clock = 0.0
    for raw in text.replace('：', ':').split():
        parts = raw.split(':')
        if len(parts) == 2:
            token, start, dur = parts[0], clock, parts[1]
        elif len(parts) == 3:
            token, start, dur = parts
        else:
            continue
        try:
            start = float(start)
            dur = float(dur)
        except ValueError:
            continue
        if not token or dur < 0:
            continue
        events.append((token, start, dur))
        clock = start + dur
    return events


def dump_score(pairs, per_line=16):
    """[(音, 持续时间)] -> 谱面文本。"""
    rows = []
    row = []
    for token, dur in pairs:
        row.append('%s:%.3f' % (token, dur))
        if len(row) >= per_line:
            rows.append(' '.join(row))
            row = []
    if row:
        rows.append(' '.join(row))
    return '\n'.join(rows) + '\n'


def score_filename(tonic, midi_path, track_index=None):
    """1.mid + tonic 71 -> 'TONIC71 1.txt'；指定音轨 2 -> 'TONIC71 1#t2.txt'"""
    stem = os.path.splitext(os.path.basename(midi_path))[0]
    if track_index:
        stem = '%s#t%d' % (stem, track_index)
    return 'TONIC%d %s.txt' % (tonic, stem)


def write_score(midi_path, tonic, pairs, out_dir=None, track_index=None):
    """写出 TONIC<tonic> <midi 文件名>.txt，返回路径。"""
    if out_dir is None:
        out_dir = os.path.dirname(os.path.abspath(midi_path))
    path = os.path.join(out_dir, score_filename(tonic, midi_path, track_index))
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(dump_score(pairs))
    return path


def read_score(path):
    """读谱面文本（自动忽略编码错误）。"""
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read()
