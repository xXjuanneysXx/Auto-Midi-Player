# -*- coding: utf-8 -*-
"""
试听：把谱面合成成一段音频，用 Windows 自带的 winsound 异步播放
==============================================================

干什么用的
----------
选完 midi（或者刚把音频转成 midi）先按一下「试听」，用耳朵确认一遍「旋律抓对了
没有」，对了再去游戏里自动演奏，省得白弹一遍。游戏内浮层里也能按。

怎么做出来的声音
----------------
没有用系统的 MIDI 音源（不是每台机器上都有，行为也不可控），而是自己拿 numpy 叠
几个正弦当「琴音」：基音 + 三个泛音，配一条指数衰减的包络，起音 / 收尾各加一小段
渐入渐出（不然音头音尾会有咔哒声）。音色像音乐盒 / 电子琴，听音高足够用了。

时间轴
------
跟谱面「音:持续时间」完全一致（原样时值，不受界面上「音长 / 速度」影响），音尾同样
裁到下一个音之前 —— 听到的就是这首歌本来的节奏。

线程
----
一首歌要算几万个采样点，放主线程会把界面卡住，所以合成丢给后台线程，合成完再交给
winsound 异步播放。同一时刻只放一段：再按一次试听就是停掉上一段。

跳转（进度条）
--------------
winsound 只会「从头放一个文件」，不能跳转、也不能暂停续播。所以拨进度条的做法是：
整首波形留在内存里，把**后面那一截**另存成一个新的 wav 再放，界面自己记住「这一遍
是从第几秒开始放的」。切出来的开头补一小段渐入，免得有咔哒声。
"""

import os
import tempfile
import time
import wave

import numpy as np

import jianpu

try:                                     # winsound 只有 Windows 上有
    import winsound
except ImportError:                      # pragma: no cover
    winsound = None


SAMPLE_RATE = 22050         # 试听用不着 CD 音质：小一点合成更快、临时文件更小
MAX_SECONDS = 900.0         # 再长的曲子也只试听前 15 分钟
PEAK = 0.32                 # 归一化后的峰值音量，留点余量别盖住游戏声音
HARMONICS = ((1, 1.0), (2, 0.32), (3, 0.14), (4, 0.06))     # (倍频, 音量)
DECAY = 3.0                 # 包络衰减速度（1/秒）：越大的音衰减越快
MIN_SOUND = 0.030           # 再短的音也让它响这么久，不然听不见
NOTE_GAP = 0.010            # 音尾留一点空隙，别和下一个音粘在一起
ATTACK = 0.006              # 起音渐入（秒）
RELEASE = 0.025             # 收尾渐出（秒）
SEEK_FADE = 0.008           # 从中间切一段出来放时，开头补的渐入（秒），防咔哒
PREVIEW_DIR = os.path.join(tempfile.gettempdir(), 'AutoPlayPreview')


def format_time(seconds):
    """秒 -> '分:秒'（状态胶囊上显示用）。"""
    seconds = max(int(round(seconds)), 0)
    return '%d:%02d' % (seconds // 60, seconds % 60)


def total_seconds(events):
    """谱面一共多长（秒）。"""
    total = 0.0
    for _token, start, duration in events:
        total = max(total, float(start) + max(float(duration), 0.0))
    return min(total, MAX_SECONDS)


def melody_notes(events):
    """谱面 -> [(音, 开始秒, 持续时间)]：休止符和看不懂的记号都去掉。"""
    notes = []
    for token, start, duration in events:
        if jianpu.token_to_rel(token) is None:
            continue
        notes.append((token, float(start), float(duration)))
    return notes


def midi_freq(pitch):
    """midi 音高 -> 频率（Hz）。A4 = 69 号音 = 440 Hz。"""
    return 440.0 * 2.0 ** ((float(pitch) - 69.0) / 12.0)


def _tone(freq, seconds, rate=SAMPLE_RATE):
    """一个音的波形：几个正弦叠起来 + 指数衰减包络（起音 / 收尾各渐一段）。"""
    count = max(int(seconds * rate), 1)
    t = np.arange(count, dtype=np.float32) / np.float32(rate)
    samples = np.zeros(count, dtype=np.float32)
    for multiple, weight in HARMONICS:
        if freq * multiple >= rate * 0.45:      # 超过奈奎斯特频率的泛音不要，免得折返成怪声
            break
        samples += weight * np.sin(np.float32(2.0 * np.pi) * np.float32(freq * multiple) * t)
    envelope = np.exp(np.float32(-DECAY) * t)
    fade_in = min(int(ATTACK * rate), count)
    if fade_in > 1:
        envelope[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    fade_out = min(int(RELEASE * rate), count)
    if fade_out > 1:
        envelope[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
    return samples * envelope


def synth_pitches(notes, rate=SAMPLE_RATE, total=None):
    """
    [(开始秒, 持续秒, midi 音高)] -> (float32 波形, 总秒数)。

    每个音的音尾都裁到下一个音开始之前，所以听出来一定是单音、不会叠在一起
    （挨得特别近的时候按 MIN_SOUND 兜底，最多重叠几毫秒，听不出来）。

    编辑器的音高直接给 midi 号，不走简谱记号 —— 免得改着改着超出简谱范围、
    被折回八度之后听着不是自己写的那一个。
    total 不传就按最后一个音的结束时间算；休止符在末尾的谱面要传一下，免得尾巴被砍掉。
    """
    notes = sorted([(float(start), max(float(dur), 0.0), int(pitch))
                    for start, dur, pitch in notes], key=lambda item: item[0])
    if total is None:
        total = max([start + dur for start, dur, _pitch in notes] or [0.0])
    total = min(float(total), MAX_SECONDS)
    if total <= 0.0 or not notes:
        return np.zeros(1, dtype=np.float32), 0.0
    samples = np.zeros(int(total * rate) + rate // 2 + 1, dtype=np.float32)
    for index, (start, duration, pitch) in enumerate(notes):
        if start >= total:
            break
        seconds = max(duration, MIN_SOUND)
        if index + 1 < len(notes):
            seconds = min(seconds, max(notes[index + 1][0] - start - NOTE_GAP, MIN_SOUND))
        seconds = min(seconds, total - start)
        if seconds <= 0.0:
            continue
        begin = int(start * rate)
        piece = _tone(midi_freq(pitch), seconds, rate)
        stop = min(begin + piece.size, samples.size)
        if stop > begin:
            samples[begin:stop] += piece[:stop - begin]
    peak = float(np.max(np.abs(samples)))
    if peak > 0.0:
        samples *= np.float32(PEAK / peak)
    return samples, total


def synth(events, tonic, rate=SAMPLE_RATE):
    """
    谱面（简谱记号）-> (float32 波形, 总秒数)。

    总长按整份谱面算（包括末尾的休止符），再交给 synth_pitches 出波形。
    """
    notes = []
    for token, start, duration in melody_notes(events):
        rel = jianpu.token_to_rel(token)
        if rel is None:
            continue
        notes.append((float(start), float(duration), tonic + rel))
    return synth_pitches(notes, rate, total=total_seconds(events))


def new_path(tag=None):
    """
    这次试听用的临时文件名。

    winsound 可能还拿着上一个文件，所以每次都得换个名字；拖进度条时一秒能切好几个
    片段，光靠毫秒时间戳有可能撞上，再带个自增序号就稳了。
    """
    name = 'preview-%d-%d' % (int(time.time() * 1000), os.getpid())
    if tag is not None:
        name += '-%d' % tag
    return os.path.join(PREVIEW_DIR, name + '.wav')


def write_wav(samples, path=None, rate=SAMPLE_RATE):
    """float32 波形 -> 16 位单声道 wav，返回路径。"""
    if path is None:
        path = new_path()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    pcm = (np.clip(samples, -1.0, 1.0) * np.float32(32767.0)).astype('<i2')
    with wave.open(path, 'wb') as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(pcm.tobytes())
    return path


def remove(path):
    """删掉试听用的临时 wav（正在放的那个可能删不掉，删不掉就算了）。"""
    try:
        os.remove(path)
    except OSError:
        pass


def clean_old(keep=None):
    """把前几次试听留下的 wav 清掉，别让临时目录一直堆。"""
    try:
        names = os.listdir(PREVIEW_DIR)
    except OSError:
        return
    for name in names:
        if not name.startswith('preview-') or not name.endswith('.wav'):
            continue
        path = os.path.join(PREVIEW_DIR, name)
        if path != keep:
            remove(path)


class Preview:
    """
    试听播放器：合成 -> 播放 -> 跳转 -> 停止。

    整首波形留在内存里（几分钟的歌也就几十兆 float32），所以拨进度条只是「切一段
    另存成 wav 再放」，不用重新合成。
    """

    def __init__(self, log=None):
        self.log = log
        self.path = None                 # 现在放着哪个 wav
        self.samples = None              # 整首波形（float32），跳转时从这里切
        self.rate = SAMPLE_RATE
        self.total = 0.0                 # 整首多长（秒）
        self.offset = 0.0                # 这一遍是从第几秒开始放的
        self._serial = 0                 # 临时文件序号：同一毫秒里切好几段也不重名

    def _say(self, text):
        if self.log:
            self.log(text)

    @staticmethod
    def available():
        """这台机器能不能试听（winsound 只有 Windows 上才有）。"""
        return winsound is not None

    def render(self, events, tonic, rate=SAMPLE_RATE):
        """
        谱面 -> 合成整首，返回总秒数（要算几百毫秒，放后台线程里调）。

        波形留在内存里给进度条跳转用，同时顺手把上几次试听留下的临时 wav 清掉。
        """
        samples, total = synth(events, tonic, rate)
        self.samples = samples
        self.total = total
        self.rate = rate
        self.offset = 0.0
        clean_old()
        return total

    def render_pitches(self, notes, rate=SAMPLE_RATE):
        """
        [(开始秒, 持续秒, midi 音高)] -> 合成整首（编辑器用，音高不走简谱记号）。
        """
        samples, total = synth_pitches(notes, rate)
        self.samples = samples
        self.total = total
        self.rate = rate
        self.offset = 0.0
        clean_old()
        return total

    def play_from(self, seconds=0.0):
        """
        从第 seconds 秒开始放，返回实际开始的秒数（放不了就返回 None）。

        winsound 只能「从头放一个文件」，所以跳转是：把后面那一截另存成一个新的
        wav 再放。切出来的开头补一小段渐入，不然会有咔哒声。
        """
        if winsound is None or self.samples is None or self.samples.size == 0:
            return None
        total = self.total
        if total <= 0.0:
            return None
        start = min(max(float(seconds), 0.0), max(total - 0.05, 0.0))
        piece = np.array(self.samples[int(start * self.rate):], dtype=np.float32, copy=True)
        if piece.size == 0:
            return None
        fade = min(int(SEEK_FADE * self.rate), piece.size)
        if fade > 1:
            piece[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        self._serial += 1
        path = new_path(self._serial)
        write_wav(piece, path, self.rate)
        previous = self.path
        self.path = path
        self.offset = start
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
        except Exception as exc:         # pragma: no cover
            self._say('试听播放失败：%s' % exc)
            return None
        if previous:                     # 上一段这会儿没人拿着了，删掉
            remove(previous)
        return start

    def stop(self):
        """停掉正在放的试听。"""
        self.path = None
        if winsound is None:
            return
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:                # pragma: no cover
            pass
