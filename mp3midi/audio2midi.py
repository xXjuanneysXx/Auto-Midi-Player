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

注意：转写出来的「完整转谱」里同时响的音可能很多（钢琴 + 乐队就是这样），程序会
按上面说的规则融合出一条单音主旋律 —— 想换成别的声部，可以在主程序里换音轨 / 重新转。
`focus_melody`（截到 200~2000 Hz）是给兜底 YIN 用的老办法，默认不再开：
那样会把低音和镲全砍掉，对真正的旋律提取没有好处。
"""

import math
import os
import sys
import importlib

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


def _has_pyin():
    """librosa 装了没有。"""
    return _try_import('librosa') is not None


def _has_basic_pitch():
    """
    basic-pitch 装了没有 —— 一定要探到 basic_pitch.inference。

    只 import 顶层包不够：真正干活的是 inference / note_creation，它们还要
    onnxruntime、scipy、numba 一大串。探得太浅就会出现「界面说能用、真转才报错」。
    这一下要把整条链 import 进来（一两秒），所以只在后台线程里调。
    """
    return _try_import('basic_pitch.inference') is not None


def describe_backends():
    """本机现在能用哪些后端（界面拿它显示提示）。"""
    return {'yin': True, 'pyin': _has_pyin(), 'basic-pitch': _has_basic_pitch()}


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


def basic_pitch_notes(path, progress=None):
    """
    basic-pitch 多声部转写（保留每一处重叠，别丢音符）。

    返回 [(开始秒, 结束秒, MIDI 音高, 力度)]。整个转写过程都在 basic-pitch 自己
    那边完成：它是专门为「有伴奏的完整编曲」训练的模型，而不是先砍频段再猜基频。
    """
    error = []
    module = _try_import('basic_pitch.inference', error)
    if module is None:
        raise RuntimeError('basic-pitch 导入不了（%s）。没装的话见 mp3midi/README.md；'
                           '打包版应该自带，这属于打包出问题，把这句话发出来就行。'
                           % (error[0] if error else '原因不明'))
    if progress:
        progress('用 basic-pitch 转写（Spotify 的开源模型，第一次会慢一点）…')
    _model, _midi, events = module.predict(
        str(path), onset_threshold=BP_ONSET, frame_threshold=BP_FRAME,
        minimum_note_length=BP_MIN_NOTE_MS,
        minimum_frequency=BP_MIN_FREQ, maximum_frequency=BP_MAX_FREQ)
    notes = []
    for event in events:
        start, stop, pitch = float(event[0]), float(event[1]), int(round(event[2]))
        loud = float(event[3]) if len(event) > 3 else 1.0
        if stop > start and 0 < pitch < 128:
            notes.append((start, stop, pitch, loud))
    if progress:
        progress('basic-pitch 听出 %d 个音（含伴奏）' % len(notes))
    return notes


def _line(notes, key):
    """
    多声部转写 -> 一条单线：每个时刻留下 key 最大的那个音。

    长音盖住一串短音、短音从长音中间穿过去，这里都能处理：按时间把所有音的开始 /
    结束排成队列扫一遍，每两个相邻的时间点之间记下「当时还响着的音里 key 最大的那个」。
    结果一定是单音、时间上不重叠。
    """
    if not notes:
        return []
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
                if spans and spans[-1][2] == chosen and spans[-1][1] >= previous - 1e-6:
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


def loud_line(notes):
    """
    主旋律候选 ①（默认用这条）：每个时刻取**最响**的那个音。

    为什么不用「最高音」打头：转写模型偶尔会听出一个又长又高、力度却不大的假音
    （现场录音里的高频噪声、泛音残留都会这样），「最高音」规则会让它把整段旋律
    霸占掉（实测有一首歌 22 秒的旋律被一个假的高音吃掉）。按力度挑就没这个问题：
    真正的主奏乐器在旋律上永远是响的那条。
    """
    return _line(notes, lambda pitch, loud: (round(loud, 3), pitch))


def top_line(notes):
    """主旋律候选 ②：每个时刻取**最高**的那个音（钢琴右手、弦乐主奏那种更准）。"""
    return _line(notes, lambda pitch, loud: (pitch, round(loud, 3)))


def fused_line(notes, step=FUSE_LOUD_STEP):
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
    return _line(notes, lambda pitch, loud: (int(float(loud) / step), pitch))


def notes_from_spans(spans, min_note=MIN_NOTE, gap=NOTE_GAP, merge_gap=MERGE_GAP):
    """
    [(开始, 结束, 音高)] -> [(开始秒, 持续秒, 音高)]。

    先并掉「同音高、中间只断了一小会儿」的两段（basic-pitch 会把一个长音切成几段），
    再剔掉太短的音和「夹在两个音中间、只差半音」的毛刺，最后给每个音留出 gap 秒的
    松开时间；出来的一定不重叠。
    """
    merged = _merge_same(spans, merge_gap)
    merged = [span for span in merged if span[1] - span[0] >= min_note]   # 太短的多半是毛刺
    merged = _drop_blips(merged)
    merged = _merge_same(merged, merge_gap)      # 毛刺去掉以后，同音高的两段可能又挨上了
    out = []
    for index, (start, stop, pitch) in enumerate(merged):
        finish = stop
        if index + 1 < len(merged):
            finish = min(finish, merged[index + 1][0])    # 绝不和下一个音重叠
        out.append((start, max(finish - start, 0.02), pitch))
    return out


def _merge_same(spans, merge_gap):
    """同音高、中间只断了一小会儿的两段并成一个（模型会把长音切成几段）。"""
    out = []
    for start, stop, pitch in sorted(spans, key=lambda span: (span[0], span[2])):
        if out and out[-1][2] == pitch and start - out[-1][1] <= merge_gap:
            out[-1][1] = max(out[-1][1], stop)
        else:
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
            progress=None, bpm=120.0, focus=False, full=False):
    """
    音频 -> MIDI，返回生成的 .mid 路径。

    progress 可以传一个 callable(text) 用来报进度；出错抛 RuntimeError。
    focus=True 会先做一次「突出主旋律」的频段处理（只对兜底 YIN 有效，默认不开：
    那样等于把音域截到 200~2000 Hz，低音和镲全没了，对旋律提取没有好处）。
    full=True 时把「完整转谱（多音同时）」也写成第二条音轨（basic-pitch 后端才有）。
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
    if chosen == 'pyin':
        f0 = pyin_f0(path, progress=progress)
        notes = notes_from_f0(f0, min_note=min_note, gap=gap)
    elif chosen == 'basic-pitch':
        polyphonic = basic_pitch_notes(path, progress=progress)
        if not polyphonic:
            raise RuntimeError('这段音频里没听出任何音符')
        melody = notes_from_spans(fused_line(polyphonic), min_note=min_note, gap=gap)
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
        notes = notes_from_f0(f0, min_note=min_note, gap=gap)
    if not notes:
        raise RuntimeError('没听出任何音高：这段音频可能太吵、太安静或者不是单声部旋律')
    extra = []
    if polyphonic:
        # 两条原始候选也留着（跟融合出来的那条不一样才写），想 A/B 对比就在主程序里换音轨
        for title, spans in (('melody2 loud', loud_line(polyphonic)),
                             ('melody3 high', top_line(polyphonic))):
            other = notes_from_spans(spans, min_note=min_note, gap=gap)
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
                        help='每个音结尾留的松开时间（秒），默认 %.2f' % NOTE_GAP)
    parser.add_argument('--melody-focus', action='store_true',
                        help='（只对兜底 yin 有效）先把频段截到 200~2000 Hz，默认不开')
    parser.add_argument('--full', action='store_true',
                        help='额外写一条「完整转谱」音轨（多音同时，basic-pitch 后端才有）')
    args = parser.parse_args(argv)
    try:
        out = convert(args.audio, args.out, args.backend, args.min_note, args.gap,
                      progress=lambda text: print(text),
                      focus=args.melody_focus, full=args.full)
    except RuntimeError as exc:
        print('转换失败：%s' % exc)
        return 1
    print('完成：%s' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
