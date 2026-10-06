# -*- coding: utf-8 -*-
"""
把 mp3 / wav / flac / ogg 转成 MIDI —— 多声部完整转谱 + 一条单音主旋律。

流程：
    basic-pitch（Spotify 的开源转写模型，ONNX 推理）听出所有音符
    → 把断成几段的长音并回去
    → 每个时刻只留一个音，得到一条主旋律（单音、绝不重叠）
    → 用 mido 写成两轨：① 主旋律（游戏要的单音轨）② 完整转谱（多音同时，留着听 / 改）

主旋律怎么挑：光按「最响」会被伴奏里更响的和弦带跑，光按「最高」会被模型偶尔冒出来
的假高音霸占（见 fused_line）。默认是**两条规则融合**：先比响度分档，同一档里再比
音高 —— 明显更响的那个赢，响度差不多的听音高的。

后端（--backend，默认 auto）：
    basic-pitch  Spotify 的 basic-pitch：真正的多声部转写，有伴奏 / 编曲也认得出旋律
                 （pip install basic-pitch，用 ONNX 模型推理，不需要 TensorFlow）
    pyin         librosa.pyin：逐帧估一个基频，适合独奏 / 清唱（pip install librosa）
    yin          自带的兜底算法，只要 numpy + scipy；只认「同时只有一个音」的音频
    auto         谁装了用谁：basic-pitch > pyin > yin

命令行：
    python audio2midi.py 歌.mp3                 # 和音频同名的 .mid
    python audio2midi.py 歌.mp3 -o 输出.mid
    python audio2midi.py 歌.mp3 --backend basic-pitch --min-note 0.12

同音重复（连着弹好几下同一个音）最容易被并成一个长音，这里靠 basic-pitch 的起音
把它们分开，见 _attack_marks / _has_attack；`--split-min` 是调这个的
（`REPEAT_LEVELS` 是主程序那个「同音重复」敏感度滑块用的档位表）。

注意：转写出来的「完整转谱」里同时响的音可能很多（钢琴 + 乐队就是这样），程序会
按上面说的规则融合出一条单音主旋律 —— 想换成别的声部，可以在主程序里换音轨 / 重新转。
`focus_melody`（截到 200~2000 Hz）是给兜底 YIN 用的老办法，默认不再开：
那样会把低音和镲全砍掉，对真正的旋律提取没有好处。
"""

import bisect
import math
import os
import sys
import importlib
import threading

import numpy as np

SR = 22050              # 统一重采样到这个采样率
FRAME = 1024            # 一帧多长（样本），约 46 毫秒
HOP = 256               # 帧移（样本），约 11.6 毫秒一步
FMIN = 55.0             # 最低音（A1）
FMAX = 1760.0           # 最高音（A6）
VOICED_MAX = 0.30       # YIN 判「这一帧有没有音高」的阈值，越小越挑
SILENCE_REL = 0.06      # 比整段峰值低这么多的帧当没声音
MIN_NOTE = 0.09         # 比这还短的音并进邻居（秒）
NOTE_GAP = 0.03         # 每个音结尾留一点松开时间（秒）
MERGE_GAP = 0.10        # 中间断了不到这么久，当成同一个音（秒）
OCTAVE_FIX = 0.20       # 孤立的八度跳短于这么久就拉回来（秒）
MELODY_LOW = 200.0      # 兜底 YIN 的「突出主旋律」频段下限（默认不用，见 focus_melody）
MELODY_HIGH = 2000.0    # 同上，上限

# basic-pitch：官方默认参数，改这三个会明显影响「听得见多少音」
BP_ONSET = 0.5            # 起音阈值：越小越敏感（也越容易听出毛刺）
BP_FRAME = 0.3            # 帧阈值
BP_MIN_NOTE_MS = 58.0     # 比这还短的音直接不算（毫秒）
BP_MIN_FREQ = 32.7        # 音域下限（C1）—— 不截频段，用模型自己的范围
BP_MAX_FREQ = 1975.5      # 音域上限（B6，模型训练到这儿）

# 「同音重复」和「一个长音被模型切成几段」在音符事件里长得一模一样 —— 两段的间隙
# 实测都是 12 毫秒上下，光看时间是分不开的。唯一靠得住的区别是**起音**：重复弹的
# 那一次带一个起音（basic-pitch 自己标出来的 onset），被切开的没有。下面这几个是
# 拿起音去挡合并时的容差和门槛，想调同音重复就动 _SPLIT_MIN（命令行 --split-min）。
ONSET_GUARD_BACK = 0.001  # 往回找的余量：接缝和起音本来是同一时刻，只有浮点误差
ONSET_GUARD_FWD = 0.05    # 往接缝后面找这么久（起音有时比音头晚一两帧）
ONSET_TOL_PITCH = 0       # 只在同一个音高上算起音（1 = 邻半音也算，更敏感也更容易误切）
ONSET_CLUSTER = 0.05      # 挨得比这还近的起音算同一次起音（秒），见 _attack_marks
ONSET_SPLIT_MIN = 0.20    # 同一个音高的起音，前后空不出这么久就不算「又弹了一下」（秒）。
                          # 颤音 / 抖音（5~7 Hz）会让模型每 0.15 秒左右报一次起音，得比它
                          # 宽一点才不会把长音切成好几段；真重复（16 分音符 150 BPM）也有
                          # 0.1 秒，比这更密的重复音就得自己把 --split-min 调小。见 _attack_marks

# 「同音重复」敏感度的档位表：主界面那个滑块用它（0 档 = 标准参数，不传额外参数）。
# 档位越高 split_min / merge_gap 越小 —— 连着弹的同一个音越容易被切成好几个音，代价
# 是颤音 / 抖音可能被切碎。命令行上等价于 --split-min / --merge-gap。
# 故意不动 onset —— 起音阈值调小会多出一堆颤音起音，跟真起音挨得太近反而会被
# _attack_marks 当成一串滤掉，重复音更容易被并。
REPEAT_LEVELS = (
    {'name': '标准', 'extra': {}},                                        # 0：默认那一套
    {'name': '稍敏感', 'extra': {'split_min': 0.17, 'merge_gap': 0.07}},   # 1
    {'name': '中等', 'extra': {'split_min': 0.14, 'merge_gap': 0.05}},     # 2
    {'name': '较敏感', 'extra': {'split_min': 0.12, 'merge_gap': 0.04}},   # 3
    {'name': '最敏感', 'extra': {'split_min': 0.09, 'merge_gap': 0.02}},   # 4
)
REPEAT_LEVEL_DEFAULT = 0

# 老名字：等于滑块上「较敏感」那一档（老版本那个「同音重复更敏感」勾选框就是它）。
# 命令行和老文档还在用它，留着别删。
REPEAT_SENSITIVE = dict(REPEAT_LEVELS[3]['extra'])


def repeat_level(index):
    """滑块第几档 -> (档位名, 额外参数)。越界 / 不是数字都退回标准档。"""
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = REPEAT_LEVEL_DEFAULT
    if not 0 <= index < len(REPEAT_LEVELS):
        index = REPEAT_LEVEL_DEFAULT
    item = REPEAT_LEVELS[index]
    return item['name'], dict(item['extra'])

# 融合主旋律时的「响度分档」粒度：同一档里比音高，差出一档才听响度。
# 0.10 大致是「力度差不到 10% 就算一样响」。
FUSE_LOUD_STEP = 0.10


# ============ 音高换算 ============

def midi_of(freq):
    """频率 -> MIDI 音高（浮点）。"""
    return 69.0 + 12.0 * math.log2(freq / 440.0)


def freq_of(midi):
    """MIDI 音高 -> 频率。"""
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


# ============ 读音频 ============

def load_audio(path, sr=SR, progress=None):
    """读成单声道 float32 并重采样到 sr。"""
    import soundfile as sf
    data, rate = sf.read(path, dtype='float32', always_2d=True)
    mono = data.mean(axis=1).astype('float32')
    if rate != sr:
        if progress:
            progress('重采样：%d Hz -> %d Hz' % (rate, sr))
        mono = _resample(mono, int(rate), sr)
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak > 0:
        mono = (mono / peak * 0.9).astype('float32')       # 统一响度，后面的阈值才好使
    return mono, sr


def _resample(mono, rate, sr):
    """
    重采样到 sr。有 scipy 就用它的多相滤波（干净），没有就线性插值顶一下。

    scipy 是运行时才 import 的：PyInstaller 静态分析要是看见它，会白白塞进去一百多兆。
    """
    signal = _try_import('scipy.signal')
    if signal is not None:
        from math import gcd
        step = gcd(rate, sr)
        return signal.resample_poly(mono, sr // step, rate // step).astype('float32')
    total = int(len(mono) * sr / float(rate))
    old = np.arange(len(mono))
    new = np.linspace(0, len(mono) - 1, total)
    return np.interp(new, old, mono).astype('float32')


def focus_melody(mono, sr, low=MELODY_LOW, high=MELODY_HIGH):
    """
    只留下主旋律常待的频段（默认 200~2000 Hz）。

    整首歌里贝斯和底鼓最容易被单声部算法当成「基频」，把低频压掉能少被带跑；
    高频主要是镲和齿音，也一起去掉。用 FFT 直接改频谱，不需要 scipy。
    """
    if len(mono) < 64:
        return mono
    size = 1
    while size < len(mono):
        size *= 2
    spec = np.fft.rfft(mono, size)
    freq = np.fft.rfftfreq(size, 1.0 / sr)
    weight = np.ones_like(freq)
    weight[freq < low] = 0.0
    weight[freq > high] = 0.0
    ramp = (freq >= low) & (freq < low * 1.5)          # 边缘做个斜坡，免得咔哒
    weight[ramp] = (freq[ramp] - low) / (low * 0.5)
    ramp = (freq > high * 0.7) & (freq <= high)
    weight[ramp] = (high - freq[ramp]) / (high * 0.3)
    return np.fft.irfft(spec * weight, size)[:len(mono)].astype('float32')


# ============ 自带 YIN ============

def yin_f0(x, sr=SR, frame=FRAME, hop=HOP, fmin=FMIN, fmax=FMAX, progress=None,
           prefer_high=True):
    """
    逐帧 YIN，返回 f0（Hz，0 表示这一帧没音高）。

    算法就是 YIN 的第二步：先算差值函数 d(tau)，再算累积均值归一化的
    d'(tau)，取第一个低于阈值的局部极小。d(tau) 用 FFT 自相关算，快很多。

    prefer_high=True：主旋律通常是最高的那个声部，如果「高八度」那个周期也
    说得通，就取高的那个 —— 有伴奏时被低音带跑的情况能少很多。
    """
    total = 1 + max(0, (len(x) - frame) // hop)
    tau_min = max(2, int(sr / fmax))
    tau_max = min(frame // 2, int(sr / fmin))
    window = np.hanning(frame).astype('float32')
    f0 = np.zeros(total, dtype='float32')
    rms = np.zeros(total, dtype='float32')
    size = 1
    while size < 2 * frame:
        size *= 2
    lags = np.arange(tau_max + 1)
    for index in range(total):
        seg = x[index * hop: index * hop + frame]
        rms[index] = float(np.sqrt(np.mean(seg ** 2)))
        seg = (seg - seg.mean()) * window
        energy = float(np.dot(seg, seg))
        if energy <= 1e-9:
            continue
        spec = np.fft.rfft(seg, size)
        ac = np.fft.irfft(spec * np.conj(spec), size)[:tau_max + 1].real
        cum = np.concatenate(([0.0], np.cumsum(seg.astype('float64') ** 2)))
        diff = cum[frame - lags] + (cum[frame] - cum[lags]) - 2.0 * ac
        cmnd = np.ones_like(diff)
        run = np.cumsum(diff[1:])
        taus = np.arange(1, len(diff))
        cmnd[1:] = diff[1:] * taus / np.maximum(run, 1e-12)
        best = int(np.argmin(cmnd[tau_min:tau_max + 1])) + tau_min
        for tau in range(tau_min, tau_max):          # 第一个低于阈值的局部极小
            if cmnd[tau] < VOICED_MAX and cmnd[tau] <= cmnd[tau + 1]:
                best = tau
                break
        if cmnd[best] >= VOICED_MAX:
            continue
        if prefer_high:                              # 上一步锁低了就往上提八度
            half = int(round(best / 2.0))
            if half >= tau_min and cmnd[half] <= cmnd[best] * 1.3:
                best = half
        tau = float(best)
        if 0 < best < tau_max:                       # 抛物线插值，小数级精度
            a, b, c = cmnd[best - 1], cmnd[best], cmnd[best + 1]
            denom = 2.0 * (2.0 * b - a - c)
            if abs(denom) > 1e-12:
                tau = best + (c - a) / denom
        if tau > 0:
            f0[index] = sr / tau
        if progress and index % 2000 == 0:
            progress('估基频：%d%%' % int(index * 100 / max(1, total)))
    # 整段里太安静的帧当没声音
    if rms.max() > 0:
        f0[rms < rms.max() * SILENCE_REL] = 0.0
    return f0


# ============ 可选后端 ============

def _pick_backend(name):
    """挑一个能用的后端。"""
    if name != 'auto':
        return name
    if _has_basic_pitch():
        return 'basic-pitch'
    if _has_pyin():
        return 'pyin'
    return 'yin'


def _try_import(name, error=None):
    """
    试着 import，没装就返回 None。

    故意用 importlib：可选后端（librosa / basic-pitch 都很大）不能让 PyInstaller
    的静态分析看见，否则会被一股脑塞进 exe 里。

    传了 error（一个 list）就把原始异常记进去 —— 打包版踩过一次坑：顶层包能
    import、真正干活的 inference 里因为少了个 scipy._cyutility 而炸，光看
    「没有装 basic-pitch」这句话根本查不出来。
    """
    try:
        return importlib.import_module(name)
    except Exception as exc:
        if error is not None:
            error.append('%s: %s' % (type(exc).__name__, exc))
        return None


_PROBE = {}
_PROBE_LOCK = threading.Lock()


def backend_probe(force=False):
    """
    三个后端现在能不能用，不能用的话带上原始异常信息。进程内只真探一次。

    import 很贵（basic-pitch 那一串要一两秒），所以结果缓存下来；缓存失败原因
    是为了出错上报 —— 打包版踩过一次「顶层包能 import、真干活时少个 dll 才炸」，
    界面只显示「没装 basic-pitch」，光看那句话根本查不出来。
    """
    with _PROBE_LOCK:
        if _PROBE and not force:
            return dict((name, dict(info)) for name, info in _PROBE.items())
        result = {'yin': {'ok': True, 'error': None}}     # 自带的，永远能用
        for name, module in (('pyin', 'librosa'),
                             ('basic-pitch', 'basic_pitch.inference')):
            error = []
            ok = _try_import(module, error) is not None
            result[name] = {'ok': ok, 'error': (error[0] if error else None)}
        _PROBE.clear()
        _PROBE.update(result)
        return dict((name, dict(info)) for name, info in result.items())


def _has_pyin():
    """librosa 装了没有。"""
    return bool(backend_probe().get('pyin', {}).get('ok'))


def _has_basic_pitch():
    """
    basic-pitch 装了没有 —— 一定要探到 basic_pitch.inference。

    只 import 顶层包不够：真正干活的是 inference / note_creation，它们还要
    onnxruntime、scipy、numba 一大串。探得太浅就会出现「界面说能用、真转才报错」。
    这一下要把整条链 import 进来（一两秒），所以只在后台线程里调；结果由
    backend_probe 缓存，同一次运行里不会反复 import。
    """
    return bool(backend_probe().get('basic-pitch', {}).get('ok'))


def describe_backends():
    """本机现在能用哪些后端（界面拿它显示提示）。"""
    return dict((name, info['ok']) for name, info in backend_probe().items())


def probe_report():
    """每个后端的探测结果拼成几行，失败带上原因 —— 给错误上报用。"""
    lines = []
    for name, info in backend_probe().items():
        if info['ok']:
            lines.append('%s：可用' % name)
        else:
            lines.append('%s：不可用（%s）' % (name, info['error'] or '原因不明'))
    return '\n'.join(lines)


def pyin_f0(path, sr=SR, progress=None):
    """librosa.pyin：单声部 / 主旋律最稳的那一档。"""
    librosa = _try_import('librosa')
    if librosa is None:
        raise RuntimeError('没有装 librosa：pip install librosa（或者改用 --backend yin）')
    y, _ = librosa.load(path, sr=sr, mono=True)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 0:
        y = (y / peak * 0.9).astype('float32')
    if progress:
        progress('用 librosa.pyin 估基频…')
    f0, voiced, _prob = librosa.pyin(y, fmin=FMIN, fmax=FMAX, sr=sr,
                                     frame_length=FRAME, hop_length=HOP)
    f0 = np.nan_to_num(f0)
    return f0.astype('float32')


def basic_pitch_transcribe(path, progress=None, onset=BP_ONSET, frame=BP_FRAME,
                           min_note_ms=BP_MIN_NOTE_MS):
    """
    basic-pitch 多声部转写（保留每一处重叠，别丢音符），返回 (音符, 起音)。

    音符是 [(开始秒, 结束秒, MIDI 音高, 力度)]。整个转写过程都在 basic-pitch 自己
    那边完成：它是专门为「有伴奏的完整编曲」训练的模型，而不是先砍频段再猜基频。

    起音是 [(秒, 音高)]，模型自己标的「这里新起了一个音」。后处理要靠它才能分清
    「同一个音重复弹了几下」和「一个长音被模型切成了几段」：这两件事在音符事件里
    长得一模一样（间隙都是十几毫秒），只有重复弹的那次带起音。见 _attack_marks。

    onset 越小越敏感（重复音更容易被切开，也更容易听出毛刺）；frame 越小音越长；
    min_note_ms 以下的音在 basic-pitch 里就不算了（毫秒）。
    """
    error = []
    module = _try_import('basic_pitch.inference', error)
    if module is None:
        raise RuntimeError('basic-pitch 导入不了（%s）。没装的话见 mp3midi/README.md；'
                           '打包版应该自带，这属于打包出问题，把这句话发出来就行。'
                           % (error[0] if error else '原因不明'))
    if progress:
        progress('用 basic-pitch 转写（Spotify 的开源模型，第一次会慢一点）…')
    output, _midi, events = module.predict(
        str(path), onset_threshold=onset, frame_threshold=frame,
        minimum_note_length=min_note_ms,
        minimum_frequency=BP_MIN_FREQ, maximum_frequency=BP_MAX_FREQ)
    notes = []
    for event in events:
        start, stop, pitch = float(event[0]), float(event[1]), int(round(event[2]))
        loud = float(event[3]) if len(event) > 3 else 1.0
        if stop > start and 0 < pitch < 128:
            notes.append((start, stop, pitch, loud))
    onsets = _model_onsets(output, onset)
    if progress:
        progress('basic-pitch 听出 %d 个音（含伴奏）、%d 处起音' % (len(notes), len(onsets)))
    return notes, onsets


def basic_pitch_notes(path, progress=None):
    """只要音符（老接口）：[(开始秒, 结束秒, MIDI 音高, 力度)]。"""
    return basic_pitch_transcribe(path, progress=progress)[0]


def _model_onsets(output, thresh):
    """
    从模型的 onsets 矩阵里挑起音，返回 [(秒, 音高)]。

    和 basic-pitch 内部（note_creation.output_to_notes_polyphonic 里那几步）用的是
    同一份数据：先取「比左右两帧都高」的局部极大，再卡阈值。这里自己用 numpy 写，
    是为了不多依赖一个 scipy，也免得跟着它的版本变。
    """
    if not isinstance(output, dict) or output.get('onset') is None:
        return []
    matrix = np.asarray(output['onset'], dtype='float32')
    if matrix.ndim != 2 or not matrix.size:
        return []
    peaks = np.zeros_like(matrix, dtype=bool)
    if len(matrix) > 2:
        middle = matrix[1:-1]
        peaks[1:-1] = (middle > matrix[:-2]) & (middle > matrix[2:])
    points = np.zeros_like(matrix)
    points[peaks] = matrix[peaks]
    rows, cols = np.where(points >= thresh)
    hop = _bp_hop_seconds()
    offset = _bp_midi_offset()
    return sorted((float(row) * hop, int(col) + offset) for row, col in zip(rows, cols))


def _bp_hop_seconds():
    """basic-pitch 一帧多少秒（问得到库就问，问不到按 22050 Hz / 256 样本算）。"""
    constants = _try_import('basic_pitch.constants')
    hop = getattr(constants, 'FFT_HOP', 256)
    rate = getattr(constants, 'AUDIO_SAMPLE_RATE', 22050)
    return float(hop) / float(rate)


def _bp_midi_offset():
    """模型输出第 0 个频点对应的音高（basic-pitch 里是 A0 = 21）。"""
    module = _try_import('basic_pitch.note_creation')
    return int(getattr(module, 'MIDI_OFFSET', 21))


def _attack_marks(onsets, split_min=ONSET_SPLIT_MIN):
    """
    [(秒, 音高)] -> {音高: [秒, …]}：只留下「真的又弹了一下」的起音。

    模型报出来的起音比真起音多得多，要筛两道才敢拿来当「同音重复」的分界：
      ① 一个音头附近常报两三个挨着的峰，其实是同一次起音 —— 按 ONSET_CLUSTER 并成
         一个，取最后一个峰（真起音在那个峰上）；
      ② 颤音 / 抖音那种音一路报下去（本仓库的合成测试音就是每 0.1 秒报一次，强度
         还不低），照它切会把一个音切成好几截 —— 只有前后都空出 split_min 的起音
         才算「又弹了一下」。
    筛完剩下这些时刻，就是可以拿来切同音重复的分界，见 _has_attack。
    """
    table = {}
    for at, pitch in onsets or ():
        table.setdefault(int(pitch), []).append(float(at))
    marks = {}
    for pitch, times in table.items():
        times.sort()
        groups = []                                       # 挨在一起的起音算一个音头
        for at in times:
            if groups and at - groups[-1][-1] < ONSET_CLUSTER:
                groups[-1].append(at)
            else:
                groups.append([at])
        peaks = [group[-1] for group in groups]
        kept = []
        for index, at in enumerate(peaks):
            before = peaks[index - 1] if index else None
            after = peaks[index + 1] if index + 1 < len(peaks) else None
            if (before is None or at - before >= split_min) \
                    and (after is None or after - at >= split_min):
                kept.append(at)
        marks[pitch] = kept
    return marks


def _has_attack(table, pitch, start, stop):
    """
    start~stop 这一小段里，同一个音高有没有**又弹一下**的分界。

    有 = 这里真的重新弹了同一个音，不能并成一个长音；没有 = 多半是模型把长音切成
    几段，可以并回去。窗口是「接缝到接缝后一点」（ONSET_GUARD_FWD：起音有时比音头
    晚一两帧），往前只留 ONSET_GUARD_BACK 那么一丁点 —— 那点余量是给浮点误差的
    （接缝和起音本来是同一个时刻，算出来的值差着最后几位），不是用来往前找的：
    音尾那点地方冒出来的起音是模型在音的尾巴上抖了一下，往前找得多了就会把长音错
    切成两段。
    """
    if not table:
        return False
    low = start - ONSET_GUARD_BACK
    high = stop + ONSET_GUARD_FWD
    for note in range(pitch - ONSET_TOL_PITCH, pitch + ONSET_TOL_PITCH + 1):
        times = table.get(note)
        if not times:
            continue
        index = bisect.bisect_left(times, low)
        if index < len(times) and times[index] <= high:
            return True
    return False


def _line(notes, key, onsets=None, split_min=ONSET_SPLIT_MIN):
    """
    多声部转写 -> 一条单线：每个时刻留下 key 最大的那个音。

    长音盖住一串短音、短音从长音中间穿过去，这里都能处理：按时间把所有音的开始 /
    结束排成队列扫一遍，每两个相邻的时间点之间记下「当时还响着的音里 key 最大的那个」。
    结果一定是单音、时间上不重叠。

    onsets 是 basic-pitch 的起音（见 basic_pitch_transcribe）。两段同音高首尾相接时
    本来会并成一个长音 —— 模型切开同音重复靠的就是这个「接缝」，所以接缝上真有起音
    就不并，没有才并（长音被切成几段的情况）。判断见 _has_attack。
    """
    if not notes:
        return []
    table = _attack_marks(onsets, split_min)
    events = []
    for index, (start, stop, pitch, _loud) in enumerate(notes):
        events.append((start, 1, index))             # 1 = 起头
        events.append((stop, 0, index))              # 0 = 收尾（同一时刻先收尾）
    events.sort(key=lambda item: (item[0], item[1]))
    active = {}                                      # 下标 -> (排队的 key, 音高)
    spans = []
    previous = events[0][0]
    for at, kind, index in events:
        if at > previous:
            if active:
                chosen = max(active.values())[1]     # key 最大那条线的音高
                if spans and spans[-1][2] == chosen and spans[-1][1] >= previous - 1e-6 \
                        and not _has_attack(table, chosen, previous, previous):
                    spans[-1][1] = at
                else:
                    spans.append([previous, at, chosen])
            previous = at
        if kind:
            start, _stop, pitch, loud = notes[index]
            active[index] = (key(pitch, loud), int(pitch))
        else:
            active.pop(index, None)
    return [tuple(span) for span in spans]


def loud_line(notes, onsets=None, split_min=ONSET_SPLIT_MIN):
    """
    主旋律候选 ①（默认用这条）：每个时刻取**最响**的那个音。

    为什么不用「最高音」打头：转写模型偶尔会听出一个又长又高、力度却不大的假音
    （现场录音里的高频噪声、泛音残留都会这样），「最高音」规则会让它把整段旋律
    霸占掉（实测有一首歌 22 秒的旋律被一个假的高音吃掉）。按力度挑就没这个问题：
    真正的主奏乐器在旋律上永远是响的那条。
    """
    return _line(notes, lambda pitch, loud: (round(loud, 3), pitch), onsets, split_min)


def top_line(notes, onsets=None, split_min=ONSET_SPLIT_MIN):
    """主旋律候选 ②：每个时刻取**最高**的那个音（钢琴右手、弦乐主奏那种更准）。"""
    return _line(notes, lambda pitch, loud: (pitch, round(loud, 3)), onsets, split_min)


def fused_line(notes, step=FUSE_LOUD_STEP, onsets=None, split_min=ONSET_SPLIT_MIN):
    """
    主旋律候选 ③（默认用这条）：把「最响」和「最高」两条线**合起来** —— 先比响度
    分档，同一档里再比音高。

    为什么要合：单用「最响」会被伴奏里更响的和弦带跑（弦乐、鼓一进来就把旋律抢走），
    单用「最高」又会被模型偶尔冒出来的假高音霸占（又长又高、力度却不大 —— 实测有
    一首歌 22 秒的旋律被一个假高音吃掉）。分档以后：明显更响的那个音赢，响度差不多
    的就听音高的，两个毛病一起躲开，挑出来的还是「伴奏上方那条主奏线」。

    注意这不是把两条线拼起来：起点仍然是**原始的那堆音**，每个时刻只留一个，
    所以结果还是严格单音、绝不重叠。
    """
    return _line(notes, lambda pitch, loud: (int(float(loud) / step), pitch), onsets, split_min)


def notes_from_spans(spans, min_note=MIN_NOTE, gap=NOTE_GAP, merge_gap=MERGE_GAP,
                     onsets=None, split_min=ONSET_SPLIT_MIN):
    """
    [(开始, 结束, 音高)] -> [(开始秒, 持续秒, 音高)]。

    先并掉「同音高、中间只断了一小会儿」的两段（basic-pitch 会把一个长音切成几段），
    再剔掉太短的音和「夹在两个音中间、只差半音」的毛刺，最后给每个音留出 gap 秒的
    松开时间；出来的一定不重叠。

    onsets 是 basic-pitch 的起音：接缝上有起音就不并 —— 「同一个音重复弹了几下」
    才不会被并成一个长音（merge_gap 只管没有起音可看的那种情况）。
    留 gap 也是为重复音好：两段同音高首尾相接时，演奏端（尤其游戏里）很容易把它
    当成一个音连过去，中间空出一点才听得出来是两个音。
    """
    merged = _merge_same(spans, merge_gap, onsets, split_min)
    merged = [span for span in merged if span[1] - span[0] >= min_note]   # 太短的多半是毛刺
    merged = _drop_blips(merged)
    # 毛刺去掉以后，同音高的两段可能又挨上了
    merged = _merge_same(merged, merge_gap, onsets, split_min)
    out = []
    for index, (start, stop, pitch) in enumerate(merged):
        finish = stop
        if index + 1 < len(merged):
            finish = min(finish, merged[index + 1][0])    # 绝不和下一个音重叠
        out.append((start, max(finish - start - gap, min(gap, finish - start)), pitch))
    return out


def _merge_same(spans, merge_gap, onsets=None, split_min=ONSET_SPLIT_MIN):
    """
    同音高、中间只断了一小会儿的两段并成一个（模型会把长音切成几段）。

    只在**没有起音**的地方并：断口上带着起音，那是同一个音重复弹了一下，得留着。
    """
    table = _attack_marks(onsets, split_min)
    out = []
    for start, stop, pitch in sorted(spans, key=lambda span: (span[0], span[2])):
        if out:
            prev = out[-1]
            if prev[2] == pitch and start - prev[1] <= merge_gap \
                    and not _has_attack(table, pitch, prev[1], start):
                prev[1] = max(prev[1], stop)
                continue
        out.append([start, stop, pitch])
    return out


def _drop_blips(spans, max_len=0.15):
    """
    去掉「比前后两个音都只差半音」的小毛刺。

    转写模型在音的交接处（上一个音还没松干净、下一个已经起音）经常插一个几十毫秒、
    差半音的音出来，人耳听着就是「蹭」一下的杂音。判断依据：它两旁是同一个方向的
    半音邻居、而且它特别短 —— 真是旋律里的半音经过音不会这么短。
    """
    out = []
    for index, (start, stop, pitch) in enumerate(spans):
        if stop - start <= max_len and 0 < index < len(spans) - 1:
            before, after = spans[index - 1], spans[index + 1]
            if abs(pitch - before[2]) == 1 and abs(pitch - after[2]) == 1 \
                    and (pitch - before[2]) * (pitch - after[2]) > 0:
                out[-1][1] = max(out[-1][1], stop)          # 并进前一个音
                continue
        out.append([start, stop, pitch])
    return out


# ============ f0 -> 一个个音 ============

def _fill_short_gaps(pitch, max_gap):
    """中间断了一小会儿的，线性插值补上（避免一个字被切成两半）。"""
    out = pitch.copy()
    index = 0
    total = len(out)
    while index < total:
        if not np.isnan(out[index]):
            index += 1
            continue
        start = index
        while index < total and np.isnan(out[index]):
            index += 1
        if start == 0 or index >= total:
            continue
        if index - start <= max_gap:
            left, right = out[start - 1], out[index]
            step = (right - left) / (index - start + 1)
            for k in range(start, index):
                out[k] = left + step * (k - start + 1)
    return out


def _median(values, window):
    """一维中值滤波（不对 NaN 做特殊处理，调用前先补洞）。"""
    if window <= 1:
        return values
    half = window // 2
    padded = np.pad(values, half, mode='edge')
    out = np.empty_like(values)
    for i in range(len(values)):
        out[i] = np.median(padded[i:i + window])
    return out


def notes_from_f0(f0, sr=SR, hop=HOP, min_note=MIN_NOTE, gap=NOTE_GAP,
                  merge_gap=MERGE_GAP, octave_fix=OCTAVE_FIX):
    """
    f0 曲线 -> [(开始秒, 持续秒, MIDI 音高)]。

    出来的一定是单音：每个音都等上一个音结束了才开始（小段之间还会留出 gap 秒）。
    """
    step = hop / float(sr)
    if len(f0) == 0:
        return []
    pitch = np.full(len(f0), np.nan, dtype='float64')
    voiced = f0 > 0
    pitch[voiced] = [midi_of(v) for v in f0[voiced]]
    if not voiced.any():
        return []
    pitch = _fill_short_gaps(pitch, int(round(merge_gap / step)))
    pitch = _median(pitch, 5)                                  # 抖一下的去掉
    pitch = np.where(np.isnan(pitch), np.nan, np.round(pitch))  # 量化到半音

    spans = []                                                 # (开始, 结束, 音高)
    index = 0
    total = len(pitch)
    while index < total:
        if np.isnan(pitch[index]):
            index += 1
            continue
        value = pitch[index]
        start = index
        while index < total and not np.isnan(pitch[index]) and pitch[index] == value:
            index += 1
        spans.append([start, index, int(value)])

    spans = _merge_spans(spans, merge_gap / step)              # 同音高、断得近的并起来
    spans = _fix_octaves(spans, octave_fix / step)             # 孤立的八度跳拉回来
    spans = _drop_short(spans, min_note / step)                # 太短的并进邻居

    notes = []
    for start, stop, value in spans:
        if value <= 0:
            continue
        begin = start * step
        length = (stop - start) * step
        notes.append((begin, length, value))
    out = []
    for index, (begin, length, value) in enumerate(notes):
        end = begin + length
        if index + 1 < len(notes):
            end = min(end, notes[index + 1][0])               # 绝不和下一个音重叠
        length = max(end - begin - gap, min(gap, length))
        out.append((begin, length, value))
    return out


def _merge_spans(spans, merge_gap):
    """相邻同音高、断得又近的并成一个。"""
    out = []
    for start, stop, value in spans:
        if out and out[-1][2] == value and start - out[-1][1] <= merge_gap:
            out[-1][1] = stop
        else:
            out.append([start, stop, value])
    return out


def _fix_octaves(spans, octave_fix):
    """夹在两个同音高之间的、很短的 ±12 度跳，当成估错，拉回来。"""
    for index in range(1, len(spans) - 1):
        prev, cur, nxt = spans[index - 1], spans[index], spans[index + 1]
        if prev[2] != nxt[2] or cur[2] == prev[2]:
            continue
        if abs(cur[2] - prev[2]) == 12 and cur[1] - cur[0] <= octave_fix:
            cur[2] = prev[2]
    return spans


def _drop_short(spans, min_frames):
    """比 min_note 还短的音：往前并（并不了就往后并）。"""
    out = []
    for start, stop, value in spans:
        if out and (stop - start) < min_frames:
            out[-1][1] = stop                     # 拉长上一个音，把这个吞掉
            continue
        out.append([start, stop, value])
    return _merge_spans(out, 1)


def write_midi(notes, path, bpm=120.0, program=0, extra=None, name='melody'):
    """
    写成 MIDI。

    notes 是主旋律 [(开始秒, 持续秒, 音高)]，写成第一条音轨；
    extra 是额外音轨 [(音轨名, [(开始秒, 持续秒, 音高)], 音色号), ...]，
    转写出来的「完整转谱」就放在这里 —— 主程序里能换音轨，DAW 里也能直接听。
    注意音轨名只能是 ASCII（MIDI 文件本身只认 latin-1，写中文会直接报错）。
    """
    import mido
    per_beat = 480
    mid = mido.MidiFile(ticks_per_beat=per_beat)
    scale = bpm / 60.0 * per_beat

    def build(track, items, title, tone, tempo=True):
        track.append(mido.MetaMessage('track_name', name=title, time=0))
        if tempo:
            track.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(bpm), time=0))
        track.append(mido.Message('program_change', program=tone, time=0))
        events = []
        for begin, length, value in items:
            # 同一个时刻先松开再按下：不然两个音会在那一瞬间同时响着（听着是叠的，
            # 主程序读回去也会当成「上一轨不是单音」）
            events.append((begin, 1, value))      # 1 = 按下
            events.append((begin + max(length, 0.01), 0, value))
        events.sort(key=lambda item: (item[0], item[1]))
        last = 0
        for at, kind, value in events:
            ticks = int(round(at * scale))
            track.append(mido.Message('note_on' if kind else 'note_off',
                                      note=int(value), velocity=80 if kind else 0,
                                      time=max(0, ticks - last)))
            last = max(last, ticks)
        track.append(mido.MetaMessage('end_of_track', time=0))

    track = mido.MidiTrack()
    mid.tracks.append(track)
    build(track, notes, name, program)
    for title, items, tone in (extra or []):
        other = mido.MidiTrack()
        mid.tracks.append(other)
        build(other, items, title, tone, tempo=False)
    mid.save(path)
    return path


# ============ 对外主函数 ============

def convert(path, out=None, backend='auto', min_note=MIN_NOTE, gap=NOTE_GAP,
            progress=None, bpm=120.0, focus=False, full=False,
            merge_gap=MERGE_GAP, onset=BP_ONSET, frame=BP_FRAME,
            min_note_ms=BP_MIN_NOTE_MS, split_min=ONSET_SPLIT_MIN):
    """
    音频 -> MIDI，返回生成的 .mid 路径。

    progress 可以传一个 callable(text) 用来报进度；出错抛 RuntimeError。
    focus=True 会先做一次「突出主旋律」的频段处理（只对兜底 YIN 有效，默认不开：
    那样等于把音域截到 200~2000 Hz，低音和镲全没了，对旋律提取没有好处）。
    full=True 时把「完整转谱（多音同时）」也写成第二条音轨（basic-pitch 后端才有）。

    同音重复（连着弹好几下同一个音）被并成一个长音的话，调这几个：
    onset 调小（起音更敏感，重复音更容易被切开）、merge_gap 调小（没有起音可看的
    那种断口也别并）、gap 调大（两个同音高的音之间空得更开，演奏端更容易分开）、
    split_min 调小（间隔更密的重复音也切开 —— 默认 0.15 秒是防颤音误切的）。
    """
    if not os.path.isfile(path):
        raise RuntimeError('找不到文件：%s' % path)
    if out is None:
        out = os.path.splitext(path)[0] + '.mid'
        try:                                    # 音频旁边写不了就丢到临时目录
            probe = open(out + '.tmp', 'wb')
            probe.close()
            os.remove(out + '.tmp')
        except OSError:
            import tempfile
            out = os.path.join(tempfile.gettempdir(), 'AutoPlayAudio',
                               os.path.basename(out))
            os.makedirs(os.path.dirname(out), exist_ok=True)
    chosen = _pick_backend(backend)
    if progress:
        progress('转换后端：%s' % chosen)
    polyphonic = []
    onsets = []
    if chosen == 'pyin':
        f0 = pyin_f0(path, progress=progress)
        notes = notes_from_f0(f0, min_note=min_note, gap=gap, merge_gap=merge_gap)
    elif chosen == 'basic-pitch':
        polyphonic, onsets = basic_pitch_transcribe(
            path, progress=progress, onset=onset, frame=frame, min_note_ms=min_note_ms)
        if not polyphonic:
            raise RuntimeError('这段音频里没听出任何音符')
        melody = notes_from_spans(fused_line(polyphonic, onsets=onsets, split_min=split_min),
                                  min_note=min_note, gap=gap, merge_gap=merge_gap,
                                  onsets=onsets, split_min=split_min)
        if progress:
            progress('从 %d 个音里融合出一条主旋律（先比响度、同档再比音高）：%d 个音'
                     % (len(polyphonic), len(melody)))
        notes = melody
    else:
        if progress:
            progress('读音频…')
        mono, sr = load_audio(path, progress=progress)
        if focus:
            if progress:
                progress('突出主旋律频段：%d~%d Hz' % (MELODY_LOW, MELODY_HIGH))
            mono = focus_melody(mono, sr)
        f0 = yin_f0(mono, sr, progress=progress)
        notes = notes_from_f0(f0, min_note=min_note, gap=gap, merge_gap=merge_gap)
    if not notes:
        raise RuntimeError('没听出任何音高：这段音频可能太吵、太安静或者不是单声部旋律')
    extra = []
    if polyphonic:
        # 两条原始候选也留着（跟融合出来的那条不一样才写），想 A/B 对比就在主程序里换音轨
        for title, spans in (('melody2 loud', loud_line(polyphonic, onsets, split_min)),
                             ('melody3 high', top_line(polyphonic, onsets, split_min))):
            other = notes_from_spans(spans, min_note=min_note, gap=gap, merge_gap=merge_gap,
                                     onsets=onsets, split_min=split_min)
            if other and other != notes:
                extra.append((title, _flatten(other), 0))
                if progress:
                    progress('备用旋律（%s）：%d 个音，可以在主程序里换音轨试听'
                             % (title.split(' ', 1)[1], len(other)))
        if full:
            extra.append(('full all-notes', _flatten(polyphonic), 0))
    write_midi(notes, out, bpm=bpm, extra=extra, name='melody lead')
    if progress:
        progress('识别出 %d 个音，已写成 %s' % (len(notes), os.path.basename(out)))
    return out


def _flatten(notes):
    """[(开始, 结束, 音高, ...)] -> [(开始, 持续, 音高)]，同一音高内部先裁掉重叠。"""
    out = []
    by_pitch = {}
    for note in sorted(notes, key=lambda item: item[0]):
        start, stop, pitch = float(note[0]), float(note[1]), int(note[2])
        by_pitch.setdefault(pitch, []).append([start, stop])
    for pitch, spans in by_pitch.items():
        for index, (start, stop) in enumerate(spans):
            if index + 1 < len(spans):
                stop = min(stop, spans[index + 1][0])
            out.append((start, max(stop - start, 0.01), pitch))
    out.sort(key=lambda item: item[0])
    return out


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description='mp3 / wav / flac -> 单音 MIDI（只留一条主旋律）')
    parser.add_argument('audio', help='要转的音频文件')
    parser.add_argument('-o', '--out', help='输出的 .mid（默认和音频同名）')
    parser.add_argument('--backend', default='auto',
                        choices=['auto', 'yin', 'pyin', 'basic-pitch'],
                        help='用哪个后端，默认 auto')
    parser.add_argument('--min-note', type=float, default=MIN_NOTE,
                        help='最短音长（秒），默认 %.2f' % MIN_NOTE)
    parser.add_argument('--gap', type=float, default=NOTE_GAP,
                        help='每个音结尾留的松开时间（秒），默认 %.2f；'
                             '两个同音高的音容易被当成一个音连过去，就是留得不够' % NOTE_GAP)
    parser.add_argument('--merge-gap', type=float, default=MERGE_GAP,
                        help='同音高的两段断开不到这么久就当同一个音（秒），默认 %.2f；'
                             '想更容易听出重复音就调小' % MERGE_GAP)
    parser.add_argument('--onset', type=float, default=BP_ONSET,
                        help='basic-pitch 起音阈值，默认 %.2f；调小会多听出一些起音'
                             '（也包括毛刺）。它跟「重复音切不切得开」不是一回事，'
                             '想切重复音请用 --split-min' % BP_ONSET)
    parser.add_argument('--frame', type=float, default=BP_FRAME,
                        help='basic-pitch 帧阈值，默认 %.2f；越小音越长' % BP_FRAME)
    parser.add_argument('--min-note-ms', type=float, default=BP_MIN_NOTE_MS,
                        help='basic-pitch 里比这还短的音直接不算（毫秒），默认 %.0f'
                             % BP_MIN_NOTE_MS)
    parser.add_argument('--split-min', type=float, default=ONSET_SPLIT_MIN,
                        help='同一个音高的起音，前后空不出这么久就不算「又弹了一下」（秒），'
                             '默认 %.2f；重复音太密被并掉就调小，颤音多的曲子被切碎就调大'
                             % ONSET_SPLIT_MIN)
    parser.add_argument('--melody-focus', action='store_true',
                        help='（只对兜底 yin 有效）先把频段截到 200~2000 Hz，默认不开')
    parser.add_argument('--full', action='store_true',
                        help='额外写一条「完整转谱」音轨（多音同时，basic-pitch 后端才有）')
    args = parser.parse_args(argv)
    try:
        out = convert(args.audio, args.out, args.backend, args.min_note, args.gap,
                      progress=lambda text: print(text),
                      focus=args.melody_focus, full=args.full,
                      merge_gap=args.merge_gap, onset=args.onset, frame=args.frame,
                      min_note_ms=args.min_note_ms, split_min=args.split_min)
    except RuntimeError as exc:
        print('转换失败：%s' % exc)
        return 1
    print('完成：%s' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
