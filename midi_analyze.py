# -*- coding: utf-8 -*-
"""
MIDI 分析：读取 -> 找主音轨 -> 保证单音 -> 推断最干净的 tonic

流程：
    1. 读取 midi，检查是不是只有一条音轨；只有一条就直接用它。
    2. 多条音轨时先按轨道名找主音轨（主音轨 / 主旋律 / melody / lead ...），
       找不到就取音符最多的那条（打击乐通道 9 不计，免得选到鼓轨）。
    3. 检查主音轨是不是由单音构成（任意两个音不同时响）。
       有重叠时：a. 先尝试按 MIDI 通道拆出一条单音旋律线（type 0 文件常见）；
                b. 拆不出来就把重叠裁掉，只保留最高音（主旋律一般在高声部）。
    4. 枚举 tonic，取「最干净」的那个：
       代价 = 折回八度 * 1000 + 升半音次数 * 10 + 升降调次数 * 3，越小越干净；
       代价相同时优先 tonic 本身出现在旋律里、且离旋律中心最近的。
       这样出来的谱面按鼠标修饰键的次数最少。
"""

from dataclasses import dataclass
import os

import mido

import jianpu


# 判定主音轨用的轨道名关键词（小写比较）
MELODY_KEYWORDS = [
    'melody', 'lead', 'main', 'vocal', 'solo', 'voice', 'singer', 'theme',
    '主音', '主旋律', '主旋', '旋律', '主奏', '主轨', '主声道', '人声',
]

# General MIDI 里通道 10（索引 9）是打击乐
DRUM_CHANNEL = 9

DEFAULT_TEMPO = 500000

# 小于这个空隙（秒）不写休止符，直接并进上一个音的时值里
REST_MIN = 0.02


class MidiError(Exception):
    """读不了 / 分析不了时抛出，消息可以直接给用户看。"""


@dataclass
class Note:
    start: float
    end: float
    pitch: int
    velocity: int = 64
    channel: int = 0

    @property
    def duration(self):
        return self.end - self.start


@dataclass
class TrackInfo:
    index: int
    name: str
    note_count: int        # 非鼓通道音符数（挑选主音轨用）
    all_notes: int         # 所有通道音符数
    pitch_min: int
    pitch_max: int


@dataclass
class TonicResult:
    tonic: int
    cost: int
    sharp: int             # 需要升半音（鼠标中键）的音数
    mod: int               # 需要升调/降调（鼠标左/右键）的音数
    folded: int            # 超出可表示音域、被折回八度的音数
    runners: list          # 前几名，方便人工确认


@dataclass
class Analysis:
    path: str
    tracks: list
    track_index: int
    track_name: str
    track_reason: str
    notes: list
    monophonic: bool       # 原音轨本身是不是单音
    overlap_count: int
    cleanup: str           # '' / '按通道拆分（通道 0）' / '裁剪重叠，保留最高音'
    split_channel: int
    tonic_result: TonicResult
    duration: float
    manual: bool = False       # 音轨是用户手动指定的（而不是自动挑的）
    auto_index: int = -1       # 自动挑出来的音轨序号，界面可以拿它显示推荐
    auto_reason: str = ''

    @property
    def tonic(self):
        return self.tonic_result.tonic

    @property
    def pitch_min(self):
        return min(n.pitch for n in self.notes)

    @property
    def pitch_max(self):
        return max(n.pitch for n in self.notes)


PITCH_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def pitch_name(pitch):
    """72 -> 'C5'"""
    return '%s%d' % (PITCH_NAMES[pitch % 12], pitch // 12 - 1)


def decode_text(raw):
    """
    mido 默认按 latin-1 解 meta 文本，中文轨道名会变乱码，这里还原回来。

    先试 utf-8（新一点的软件都这么存），再试 gbk（老 Cakewalk / 国产软件），
    都不行就原样返回 —— 总比抛异常强。
    """
    if not raw:
        return ''
    try:
        data = raw.encode('latin-1')
    except UnicodeEncodeError:
        return raw
    for codec in ('utf-8', 'gbk'):
        try:
            return data.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw


def load(path):
    if not os.path.isfile(path):
        raise MidiError('找不到文件：%s' % path)
    try:
        return mido.MidiFile(path, clip=True)
    except Exception as exc:
        raise MidiError('读不了这个 midi 文件：%s' % exc)


def describe_tracks(midi):
    """列出每条音轨的名称和音符数。"""
    info = []
    for index, track in enumerate(midi.tracks):
        name = ''
        pitches = []
        all_notes = 0
        for msg in track:
            if msg.type == 'track_name' and not name:
                name = decode_text(msg.name)
            elif msg.type == 'note_on' and msg.velocity > 0:
                all_notes += 1
                if getattr(msg, 'channel', 0) != DRUM_CHANNEL:
                    pitches.append(msg.note)
        info.append(TrackInfo(
            index=index,
            name=name,
            note_count=len(pitches),
            all_notes=all_notes,
            pitch_min=min(pitches) if pitches else 0,
            pitch_max=max(pitches) if pitches else 0,
        ))
    return info


def select_melody_track(tracks):
    """返回 (音轨序号, 选择理由)。"""
    if not tracks:
        raise MidiError('这个文件里没有音轨')
    if len(tracks) == 1:
        return tracks[0].index, '只有一个音轨'

    for track in tracks:
        name = track.name.lower()
        for keyword in MELODY_KEYWORDS:
            if keyword in name and track.all_notes > 0:
                return track.index, '音轨名匹配「%s」：%s' % (keyword, track.name)

    usable = [t for t in tracks if t.note_count > 0] or [t for t in tracks if t.all_notes > 0]
    if not usable:
        raise MidiError('所有音轨里都没有音符')
    best = max(usable, key=lambda t: (t.note_count, t.all_notes, -t.index))
    return best.index, '音符最多（%d 个）' % best.note_count


def extract_notes(midi, track):
    """把一条音轨里的 note_on / note_off 配对成音符，时间换算成秒（跟着 tempo 走）。"""
    ticks_per_beat = midi.ticks_per_beat
    tempo = DEFAULT_TEMPO
    now = 0.0
    pending = {}
    notes = []
    for msg in track:
        now += mido.tick2second(msg.time, ticks_per_beat, tempo)
        if msg.type == 'set_tempo':
            tempo = msg.tempo
            continue
        if msg.type == 'note_on' and msg.velocity > 0:
            key = (getattr(msg, 'channel', 0), msg.note)
            old = pending.get(key)
            if old is not None:                      # 同一个音还没松开又按了一次
                notes.append(Note(old[0], now, msg.note, old[1], key[0]))
            pending[key] = (now, msg.velocity)
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            key = (getattr(msg, 'channel', 0), msg.note)
            old = pending.pop(key, None)
            if old is not None:
                notes.append(Note(old[0], now, msg.note, old[1], key[0]))
    for (channel, pitch), (start, velocity) in pending.items():   # 没松开的音收在末尾
        notes.append(Note(start, now, pitch, velocity, channel))
    notes = [n for n in notes if n.duration > 1e-6]
    notes.sort(key=lambda n: (n.start, -n.pitch))
    return notes


def count_overlaps(notes):
    """同时响着的音的对数（近似：只要新音比当前结束时间早就是重叠）。"""
    overlaps = 0
    end = float('-inf')
    for note in notes:
        if note.start < end - 1e-9:
            overlaps += 1
        end = max(end, note.end)
    return overlaps


def is_monophonic(notes):
    return count_overlaps(notes) == 0


def monophonize(notes):
    """把复音裁成单音：重叠时保留更高的音。"""
    result = []
    for note in sorted(notes, key=lambda n: (n.start, -n.pitch)):
        if result and note.start < result[-1].end - 1e-9:
            if note.pitch > result[-1].pitch:
                result[-1].end = note.start
                result.append(note)
        else:
            result.append(note)
    return [n for n in result if n.duration > 1e-6]


def pick_monophonic_channel(notes):
    """按通道分组，找出一条本身是单音的通道（音符最多的那条）。"""
    groups = {}
    for note in notes:
        groups.setdefault(note.channel, []).append(note)
    candidates = []
    for channel, group in groups.items():
        if channel == DRUM_CHANNEL:
            continue
        group.sort(key=lambda n: (n.start, -n.pitch))
        if is_monophonic(group):
            candidates.append((len(group), channel, group))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    _, channel, group = candidates[0]
    return channel, group


def find_tonic(notes):
    """枚举 tonic，挑修饰最少、也最贴合旋律的那个。"""
    pitches = [n.pitch for n in notes]
    present = set(pitches)
    center = sorted(pitches)[len(pitches) // 2]
    results = []
    for tonic in range(128):
        sharp = mod = folded = 0
        for pitch in pitches:
            token = jianpu.pitch_to_token(pitch, tonic)
            sharp += 1 if token.sharp else 0
            mod += 1 if token.mod else 0
            folded += 1 if token.folded else 0
        cost = folded * 1000 + sharp * 10 + mod * 3
        results.append((cost, tonic, sharp, mod, folded))
    results.sort(key=lambda r: (r[0], 0 if r[1] in present else 1, abs(r[1] - center), r[1]))
    cost, tonic, sharp, mod, folded = results[0]
    return TonicResult(tonic, cost, sharp, mod, folded, results[:5])


def analyze(path, track_index=None):
    """
    完整分析一个 midi 文件。

    track_index 为 None 时自动挑主旋律音轨；传入具体序号则强制用那一条
    （界面上的「音轨选择」用），这时 Analysis.manual 是 True。
    """
    midi = load(path)
    tracks = describe_tracks(midi)
    manual = track_index is not None
    try:
        auto_index, auto_reason = select_melody_track(tracks)
    except MidiError:
        if not manual:
            raise
        auto_index, auto_reason = -1, ''
    if manual:
        if not 0 <= track_index < len(tracks):
            raise MidiError('音轨序号 %d 超出范围（这个文件只有 %d 条音轨）' % (track_index, len(tracks)))
        reason = '手动选择'
    else:
        track_index, reason = auto_index, auto_reason
    notes = extract_notes(midi, midi.tracks[track_index])
    if not notes:
        if manual:
            raise MidiError('音轨 %d 里没有音符，换一条音轨试试' % track_index)
        raise MidiError('音轨 %d 里没有音符' % track_index)

    overlaps = count_overlaps(notes)
    monophonic = overlaps == 0
    cleanup = ''
    split_channel = -1
    if not monophonic:
        picked = pick_monophonic_channel(notes)
        if picked:
            split_channel, notes = picked
            cleanup = '原音轨是多音（多个通道混在一起），按单音通道 %d 拆分' % split_channel
        else:
            notes = monophonize(notes)
            cleanup = '原音轨有多音重叠，已裁剪成单音旋律（保留最高音）'

    tonic_result = find_tonic(notes)
    return Analysis(
        path=path,
        tracks=tracks,
        track_index=track_index,
        track_name=tracks[track_index].name,
        track_reason=reason,
        notes=notes,
        monophonic=monophonic,
        overlap_count=overlaps,
        cleanup=cleanup,
        split_channel=split_channel,
        tonic_result=tonic_result,
        duration=max(n.end for n in notes),
        manual=manual,
        auto_index=auto_index,
        auto_reason=auto_reason,
    )


def build_score(notes, tonic):
    """
    音符序列 -> [(音, 持续时间)]，时间轴与 midi 完全对齐。

    音符之间明显的空隙补一个休止符 `0`，很小的空隙并进上一个音的时值
    （开头的小空隙没有上家，直接忽略，最多差 20 毫秒）；
    最后把每条时间边界四舍五入到毫秒再求差，这样累加时值不会累积误差。
    """
    items = []
    clock = 0.0
    for note in notes:
        if note.start > clock + 1e-9:
            gap = note.start - clock
            if items and gap < REST_MIN:
                items[-1][1] += gap
            elif gap >= REST_MIN:
                items.append([jianpu.REST_TOKEN, gap])
        duration = max(note.duration, 0.001)
        items.append([jianpu.pitch_to_token(note.pitch, tonic).text, duration])
        clock = note.start + duration

    edges = [0.0]
    for _, duration in items:
        edges.append(edges[-1] + duration)
    edges = [round(edge, 3) for edge in edges]
    return [(items[i][0], max(edges[i + 1] - edges[i], 0.0)) for i in range(len(items))]


def format_report(analysis, score_path=None):
    """把分析结果排成给人看的几行文字。"""
    lines = []
    lines.append('音轨：%d 条' % len(analysis.tracks))
    for track in analysis.tracks:
        mark = '->' if track.index == analysis.track_index else '  '
        name = track.name or '(无名)'
        if track.note_count:
            lines.append('  %s [%d] %s  音符 %d，音域 %s~%s' % (
                mark, track.index, name, track.all_notes,
                pitch_name(track.pitch_min), pitch_name(track.pitch_max)))
        else:
            lines.append('  %s [%d] %s  音符 %d' % (mark, track.index, name, track.all_notes))
    lines.append('选中音轨：[%d] %s（%s）' % (analysis.track_index, analysis.track_name or '(无名)', analysis.track_reason))
    if analysis.manual and analysis.auto_index >= 0 and analysis.auto_index != analysis.track_index:
        auto_track = analysis.tracks[analysis.auto_index]
        lines.append('  自动推荐的是：[%d] %s（%s）' % (
            auto_track.index, auto_track.name or '(无名)', analysis.auto_reason))
    lines.append('单音检查：%s' % ('是单音，没有重叠' if analysis.monophonic else '有 %d 处重叠 -> %s' % (analysis.overlap_count, analysis.cleanup)))
    lines.append('音符：%d 个，音高 %s ~ %s，时长 %.2f 秒' % (
        len(analysis.notes), pitch_name(analysis.pitch_min), pitch_name(analysis.pitch_max), analysis.duration))
    result = analysis.tonic_result
    lines.append('最干净 tonic：%d (%s)，代价 %d = 升半音 %d 个 + 升降调 %d 个%s' % (
        result.tonic, pitch_name(result.tonic), result.cost, result.sharp, result.mod,
        '，折回八度 %d 个' % result.folded if result.folded else ''))
    others = ', '.join('%d(%s)/%d' % (r[1], pitch_name(r[1]), r[0]) for r in result.runners[1:4])
    if others:
        lines.append('  备选（tonic/代价）：%s' % others)
    if score_path:
        lines.append('谱面：%s' % score_path)
    return '\n'.join(lines)
