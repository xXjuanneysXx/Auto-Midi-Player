# -*- coding: utf-8 -*-
"""
录制监听音：弹一个键就响一声
============================

干什么用的
----------
在桌面（游戏外）按 F10 录制时，每按下一个琴键就立刻响一个音 —— 像是给你一个
「琴键反馈」，能听出来自己按的是哪个音、有没有按错。

**开就是开、关就是关**：界面上那个「录制时发声」勾着就响，不勾就全程静音，
不去猜「现在是不是在游戏里」。游戏里的音是游戏自己放的，想清静就自己关掉。

怎么做出来的声音
----------------
跟试听（preview.py）同一套合成：几个正弦叠起来，包络是「一拨就响、然后一直亮着」，
听起来像按住琴弦不放。

**按住多久就响多久**：按下一个键 = 开始放这个音，松开 = 立刻停。监听音本身合成成
一段很长的**持续音**（按下先「咚」一下，之后一直保持同一音量），松开时由
`NotePlayer.release` 掐掉 —— 所以长按听到的是长音，而不是「响一下就没了」。

**为什么落盘不内存播放**：winsound 的 `SND_MEMORY` 和 `SND_ASYNC` *不能*一起用
（Windows 会直接报 `Cannot play asynchronously from memory`，实测就是这么弹回来的）。
同步播又会把录制钩子的回调堵住十几秒 —— 低级钩子超过 LowLevelHooksTimeout 会被
系统摘掉，绝对不能那么干。所以做法是：**每个音高只合成一次，写成一个 wav 放在
临时目录里**，之后按键就是 `SND_FILENAME | SND_ASYNC` 直接放文件，
既不卡钩子也不用等合成。文件按音高命名，跨次运行还能接着用。
"""

import io
import os
import tempfile
import time
import wave

import numpy as np

import preview                                    # 借它一个「midi 音高 -> 频率」

try:                                              # winsound 只有 Windows 上有
    import winsound
except ImportError:                               # pragma: no cover
    winsound = None


SAMPLE_RATE = 22050
# 按住时最长响这么久。按下就开声、松开才停（见 NotePlayer.play / release），
# 所以这个值约等于「一次最多能按住多久」，留得比一般曲子里的长音宽裕。
SOUND_SECONDS = 16.0
SUSTAIN = 0.35                # 起音衰减完之后留下来继续响的音量：长按听着才是个「持续音」
HOLD_DECAY = 2.2              # 从峰值衰到 SUSTAIN 的快慢（1/秒）
PEAK = 0.34                   # 峰值音量（别盖过游戏声音）
HARMONICS = ((1, 1.0), (2, 0.30), (3, 0.12))     # (倍频, 音量)
ATTACK = 0.004                # 起音渐入（秒），防咔哒
RELEASE = 0.040               # 收尾渐出（秒）
KEEP_DAYS = 7                 # 临时目录里的监听音放这么久，过期清掉
PREFIX = 'monitor2-'          # 临时文件名前缀：包络一改就换个前缀，免得接着用旧的短音

MONITOR_DIR = os.path.join(tempfile.gettempdir(), 'AutoPlayMonitor')


def tone_wav(pitch, seconds=SOUND_SECONDS, rate=SAMPLE_RATE):
    """midi 音高 -> 一小段 wav 的字节。"""
    count = max(int(seconds * rate), 1)
    t = np.arange(count, dtype=np.float32) / np.float32(rate)
    freq = preview.midi_freq(pitch)
    samples = np.zeros(count, dtype=np.float32)
    for multiple, weight in HARMONICS:
        if freq * multiple >= rate * 0.45:       # 超过奈奎斯特频率的泛音不要
            break
        samples += weight * np.sin(np.float32(2.0 * np.pi) * np.float32(freq * multiple) * t)
    # 拨弦式的起音 + 长长的持续段：按下先「咚」一下，随后一直保持 SUSTAIN 的音量，
    # 直到松开那一刻被 NotePlayer.release 掐掉 —— 按住多久就响多久。
    envelope = SUSTAIN + np.float32(1.0 - SUSTAIN) * np.exp(np.float32(-HOLD_DECAY) * t)
    fade_in = min(int(ATTACK * rate), count)
    if fade_in > 1:
        envelope[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)
    fade_out = min(int(RELEASE * rate), count)
    if fade_out > 1:
        envelope[-fade_out:] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)
    pcm = (np.clip(samples * envelope * np.float32(PEAK), -1.0, 1.0)
           * np.float32(32767.0)).astype('<i2')
    return _container(pcm, rate)


def _container(pcm, rate):
    """16 位单声道 PCM -> 一整段 wav 的字节（不落盘，写文件时再写）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


class NotePlayer:
    """
    按键监听音播放器：play(音高) 就响一声，stop() 立刻闭嘴。

    会被钩子线程调用（录制回调里），所以这儿不碰任何界面对象，只动文件和 winsound。
    """

    def __init__(self, log=None):
        self.log = log
        self._paths = {}           # 音高 -> 已经写好的 wav 路径
        self._keep = []            # 还在放的那几个文件路径：先留着别删
        self._current = None       # 这会儿正响着的音高（没有就是 None）
        self._clean_old()

    def _say(self, message):
        if self.log:
            try:
                self.log(message)
            except Exception:
                pass

    @staticmethod
    def available():
        """这台机器能不能出声（winsound 只有 Windows 上有）。"""
        return winsound is not None

    @staticmethod
    def _clean_old():
        """把很久以前留下的监听音清掉，别让临时目录一直堆（正放着的先不管）。"""
        try:
            names = os.listdir(MONITOR_DIR)
        except OSError:
            return
        deadline = time.time() - KEEP_DAYS * 86400
        for name in names:
            if not name.endswith('.wav') or not name.startswith('monitor'):
                continue
            path = os.path.join(MONITOR_DIR, name)
            try:
                if os.path.getmtime(path) < deadline:
                    os.remove(path)
            except OSError:
                pass

    def path_of(self, pitch):
        """这个音高的 wav 在哪；没有就现合一个写出来（写一次，之后一直用）。"""
        path = self._paths.get(int(pitch))
        if path and os.path.isfile(path):
            return path
        try:
            os.makedirs(MONITOR_DIR, exist_ok=True)
        except OSError:
            return None
        path = os.path.join(MONITOR_DIR, PREFIX + '%d.wav' % int(pitch))
        try:
            if not os.path.isfile(path) or os.path.getsize(path) < 44:
                with open(path, 'wb') as handle:
                    handle.write(tone_wav(int(pitch)))
        except OSError as exc:                       # pragma: no cover
            self._say('写监听音文件失败：%s' % exc)
            return None
        self._paths[int(pitch)] = path
        return path

    def play(self, pitch):
        """
        按下去：这个音开始响，一直响到你松开（release 来掐）。

        监听音本身是一段很长的持续音（SOUND_SECONDS），按下时从头上放，
        松开时由 release() 停掉 —— 所以「按住多久就响多久」，不再是响一下就没。
        """
        if winsound is None:
            return False
        try:
            pitch = int(pitch)
        except (TypeError, ValueError):
            return False
        path = self.path_of(pitch)
        if not path:
            return False
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_NODEFAULT)
        except Exception as exc:                     # pragma: no cover
            self._say('播放按键监听音失败：%s' % exc)
            return False
        # 正在放的那个文件先留着（winsound 是异步的，这会儿还在读它）
        self._keep.append(path)
        del self._keep[:-2]
        self._current = pitch
        return True

    def release(self, pitch):
        """
        松开某个键：正响着的确实是它才停。

        为什么要对音高：先按住 z 再按下 x（x 把声音接了过去），这时候松开 z 不该
        把 x 掐掉 —— 那听着就像「按了没反应」。
        """
        try:
            pitch = int(pitch)
        except (TypeError, ValueError):
            return False
        if self._current != pitch:
            return False
        self._current = None
        self._silence()
        return True

    @staticmethod
    def _silence():
        """掐掉正在响的这一个（winsound 一次只放一个音，见模块开头的说明）。"""
        if winsound is None:
            return
        try:
            winsound.PlaySound(None, 0)              # NULL = 停掉当前这个
        except Exception:                            # pragma: no cover
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def stop(self):
        """闭嘴（停录 / 退出程序时用）。"""
        self._keep = []
        self._current = None
        self._silence()