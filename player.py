# -*- coding: utf-8 -*-
"""
演奏器：把谱面按时间敲进游戏

键位：1z 2x 3c 4v 5b 6n 7m i,
修饰键：升调 = 鼠标右键，降调 = 鼠标左键，升半音 = 鼠标中键
        （都是按住才生效，所以敲键的时候按住，敲完再松开；
          下一个音还需要同一个修饰键就继续按着，省掉一次按下/松开的延迟）

输入用 pydirectinput（SendInput + scancode），DirectInput 游戏也收得到。
每个音都是「按下 -> 按住 -> 松开」，按住时长就是 midi 里这个音的真实时值
（谱面里的「音:持续时间」）：长音一直按着，短音很快松开；快到下一个音时会
提前一点点松开，免得两个音粘在一起。休止符 `0` 只是等时间，不按键。
"""

import threading
import time

import jianpu

try:
    import pydirectinput
    pydirectinput.PAUSE = 0
    # 关掉 pyautogui 那套「鼠标撞到屏幕角落就抛异常中断」的保险：我们的演奏本来就
    # 是有意为之的自动化，停不停由 F8 决定；游戏里鼠标正好停在角落时它会把整首
    # 曲子中断掉（修饰键用的就是鼠标，风险更大）。
    pydirectinput.FAILSAFE = False
    _IMPORT_ERROR = None
except Exception as _exc:                       # pragma: no cover
    pydirectinput = None
    _IMPORT_ERROR = _exc


MIN_HOLD = 0.035        # 一个音最少按这么久：游戏按帧读键，太短会被「吞音」
MIN_TAP = 0.020         # 兜底下限：音挨得极近、或者已经起晚了，也至少按这么久
RELEASE_GAP = 0.015     # 松开后到下一个音之间留一点空隙，免得两个音粘在一起
REPEAT_GAP = 0.030      # 同一个键连着响时，确保游戏看见过一次松开（否则第二个音被吞）
MOD_DELAY = 0.02        # 按下修饰键后等一小会儿再按琴键
MOD_HOLD_MAX = 0.6      # 两个音之间超过这么久，就把修饰键松开
LEAD_TIME = 3.0         # 开始演奏前留给玩家切窗口的时间

# 谱面时值怎么处理，界面上叫「音长」
MODE_EQUAL = '等长演奏'      # 每个音都按同一个长度按住 —— 长短一样，听着最稳（要自己选）
MODE_ORIGINAL = '原样演奏'   # 按 midi 的时值来 —— 长短不一，更有起伏（默认）
NOTE_MODES = (MODE_EQUAL, MODE_ORIGINAL)
NOTE_MODE_DEFAULT = MODE_ORIGINAL
NOTE_MS_DEFAULT = 90         # 一个音按多长（毫秒）
NOTE_MS_RANGE = (40, 500)
MAX_STRETCH = 8.0            # 「凑不够就整首放慢」最多放慢这么多倍，再多就不像话了


class Player:
    def __init__(self, log=None, min_hold=None):
        self.log = log
        # 一个音最少按多久（秒），界面上可以调；太短的游戏会收不到
        self.min_hold = MIN_HOLD if min_hold is None else max(float(min_hold), MIN_TAP)
        self.held = set()
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()
        self.running = threading.Event()
        self._last_release = None      # (键, 松开时刻)，用来处理同键重复
        self._t0 = None                # 本次演奏「谱面 0 秒」对应的真实时刻
        self._paused_at = None         # 暂停发生在哪一刻（暂停时时间要冻住）

    # ---------- 小工具 ----------

    def _say(self, message):
        if self.log:
            self.log(message)

    def _mouse_down(self, button):
        pydirectinput.mouseDown(button=button)

    def _mouse_up(self, button):
        pydirectinput.mouseUp(button=button)

    # ---------- 修饰键 ----------

    def sync_mods(self, target):
        """把按住的鼠标修饰键调整成 target，返回新按下的个数。"""
        with self.lock:
            current = set(self.held)
        pressed = 0
        for mod in sorted(current - target):
            try:
                self._mouse_up(jianpu.MOD_MOUSE[mod])
            except Exception:
                pass
            with self.lock:
                self.held.discard(mod)
        for mod in sorted(target - current):
            try:
                self._mouse_down(jianpu.MOD_MOUSE[mod])
                pressed += 1
                with self.lock:
                    self.held.add(mod)
            except Exception as exc:
                self._say('按修饰键失败（%s）：%s' % (mod, exc))
        return pressed

    def release_all(self):
        """松开所有鼠标修饰键（暂停、停止、出错时都要调）。"""
        with self.lock:
            held, self.held = list(self.held), set()
        for mod in held:
            try:
                self._mouse_up(jianpu.MOD_MOUSE[mod])
            except Exception:
                pass

    # ---------- 控制 ----------

    def stop(self):
        self.stop_flag.set()
        self.pause_flag.clear()
        self.release_all()

    def toggle_pause(self):
        if not self.running.is_set():
            self._say('当前没有在演奏')
            return
        if self.pause_flag.is_set():
            self.pause_flag.clear()
            self._say('继续演奏')
        else:
            self.pause_flag.set()
            self._paused_at = time.perf_counter()
            self._say('暂停')
            self.release_all()

    def elapsed(self):
        """
        演奏到第几秒了（按谱面时间算，负数表示还在开场倒计时）。

        没在演奏时返回 None；暂停时停在暂停的那一刻。跟奏窗口靠它对齐音符。
        """
        if self._t0 is None:
            return None
        if self._paused_at is not None:
            return self._paused_at - self._t0
        return time.perf_counter() - self._t0

    # ---------- 演奏 ----------

    @staticmethod
    def speed_up(events, speed):
        if not speed or speed == 1.0:
            return list(events)
        factor = 1.0 / speed
        return [(token, start * factor, duration * factor) for token, start, duration in events]

    # ---------- 谱面时值整理（「音长」） ----------

    @staticmethod
    def stretch_factor(events, min_note, keep_duration, gap_floor=RELEASE_GAP):
        """
        要让每个音都至少按住 min_note 这么久，整首歌得放慢多少倍。

        一个音能按住多久，既看它自己的时值，也看到下一个音还有多远 —— 到下一个音
        之前得留出 gap_floor 松手，不然两个音会粘在一起。只把太短的那一个音拉长会
        让节奏变形，所以这里是**整首等比放慢**（等于降 BPM），长短关系还是原来那样。

        keep_duration=True（原样演奏）：音本身的时值也要够长；
        keep_duration=False（等长演奏）：每个音都会按 min_note 按住，只看间隔够不够。
        返回 1.0 表示不用动。
        """
        shortest = None
        total = len(events)
        for index, (token, start, duration) in enumerate(events):
            if jianpu.is_rest(token):
                continue
            if index + 1 < total:
                room = events[index + 1][1] - start - gap_floor
                if keep_duration:
                    room = min(room, duration)
            elif keep_duration:
                room = duration
            else:
                continue                    # 等长演奏：最后一个音没有「下一个」，不用管
            room = max(room, 0.02)          # 挨得太死就按 20 毫秒算，免得倍数炸掉
            shortest = room if shortest is None else min(shortest, room)
        if not shortest or shortest >= min_note:
            return 1.0
        return min(min_note / shortest, MAX_STRETCH)

    @staticmethod
    def apply_factor(events, factor):
        """整首乘一个系数：开始时间和时长一起放大，比例不变。"""
        if not factor or factor == 1.0:
            return list(events)
        return [(token, start * factor, duration * factor) for token, start, duration in events]

    @classmethod
    def shape(cls, events, mode, note_len, stretch=False):
        """
        按「音长」把谱面整理一遍，返回 (新谱面, 放慢倍数)。

        - 等长演奏：每个音都按住 note_len 这么久，长短一样、听着最稳。挨得太近的音
          自动缩短到「下一个音之前留出松手的时间」，节奏还是原速；
        - 原样演奏：原封不动，跟着 midi 的时值走，长短有起伏；
        - stretch=True（界面上「凑不够就整首放慢」）：谱面里有音比 note_len 还短时，
          整首等比放慢来凑够长度 —— 长短关系不变，但曲子会变慢。
        """
        events = list(events)
        factor = 1.0
        if stretch:
            factor = cls.stretch_factor(events, note_len, keep_duration=(mode != MODE_EQUAL))
            events = cls.apply_factor(events, factor)
        if mode != MODE_EQUAL:
            return events, factor
        out = []
        total = len(events)
        for index, (token, start, duration) in enumerate(events):
            if jianpu.is_rest(token):
                out.append((token, start, duration))
                continue
            length = note_len
            if index + 1 < total:
                room = events[index + 1][1] - start - RELEASE_GAP
                length = min(length, max(room, MIN_TAP))
            out.append((token, start, length))
        return out, factor

    def _hold(self, key, release_at):
        """
        按住 key，到 release_at 时刻松开（中途暂停 / 停止会立刻松开）。

        注意：如果前面拖了一点时间、release_at 已经过去了，也不能 0 毫秒松开——那样
        游戏很可能整帧都读不到这次按键（就是「吞音」）。所以再急也至少按 MIN_TAP。
        """
        pydirectinput.keyDown(key)
        started = time.perf_counter()
        try:
            return self._wait_for(max(release_at, started + MIN_TAP))
        finally:
            pydirectinput.keyUp(key)
            self._last_release = (key, time.perf_counter())

    def _wait_for(self, target):
        """
        等到 target 时刻。返回 True 表示可以继续，'resume' 表示刚暂停恢复
        （调用方需要重新对时），False 表示要中止。
        """
        while True:
            if self.stop_flag.is_set():
                return False
            if self.pause_flag.is_set():
                self.release_all()
                self._paused_at = time.perf_counter()      # 时间冻在这一刻
                while self.pause_flag.is_set():
                    if self.stop_flag.is_set():
                        self._paused_at = None
                        return False
                    time.sleep(0.05)
                self._paused_at = None
                return 'resume'
            remaining = target - time.perf_counter()
            if remaining <= 0:
                return True
            if remaining > 0.003:
                # 分小段睡：中间的长空隙也要能立刻收到暂停 / 停止
                time.sleep(min(remaining - 0.003, 0.02))

    def play(self, events, speed=1.0, lead_time=LEAD_TIME, on_progress=None, min_hold=None):
        """
        按谱面演奏。events = [(音, 开始秒, 持续时间)]，阻塞到演奏结束。

        每个音按住「持续时间」这么久再松开，也就是跟着 midi 的时值走。
        on_progress(已完成, 总数) 会在演奏过程中被调用（每秒最多 20 次），
        界面可以拿它画进度条。
        min_hold 可以临时指定「一个音最少按多久」（秒），界面上的最短按键就是它。
        """
        if pydirectinput is None:
            raise RuntimeError('pydirectinput 用不了：%s' % _IMPORT_ERROR)
        if min_hold is not None:
            self.min_hold = max(float(min_hold), MIN_TAP)
        self.stop_flag.clear()
        self.pause_flag.clear()
        self.running.set()
        events = self.speed_up(events, speed)
        played = 0
        tight = 0                      # 因为挨得太近而按不到 min_hold 的音数
        try:
            self._t0 = time.perf_counter() + max(lead_time, 0.0)   # 倒计时也算进这个钟
            if lead_time > 0:
                self._say('%.1f 秒后开始演奏，请切到游戏窗口…' % lead_time)
                if self._wait_for(time.perf_counter() + lead_time) is False:
                    return False
            t0 = time.perf_counter()
            self._t0 = t0
            total = len(events)
            last_report = 0.0
            for index, (token, start, duration) in enumerate(events):
                next_start = events[index + 1][1] if index + 1 < total else None
                gap = None if next_start is None else next_start - start
                mods, digit = jianpu.parse_token(token)
                rest = jianpu.is_rest(token)
                key = None if rest else jianpu.DIGIT_KEYS.get(digit)
                if not rest and key is None:
                    self._say('谱面里有看不懂的音 %r，跳过' % token)

                # 同一个键连着响（比如 E1 E1）：必须让游戏先看见一次「松开」，
                # 否则第二次按下会被当成没松手，那个音就没了
                due = t0 + start
                if key is not None and self._last_release is not None \
                        and self._last_release[0] == key:
                    due = max(due, self._last_release[1] + REPEAT_GAP)

                # 游戏是在琴键按下那一刻读修饰键状态，所以带修饰键的音要提前
                # MOD_DELAY 起床：先把鼠标按好，琴键才能正好落在音头上
                result = self._wait_for(due - (MOD_DELAY if key else 0.0))
                if result is False:
                    break
                if result == 'resume':
                    t0 = time.perf_counter() - start
                    self._t0 = t0
                    continue
                if key is None:
                    if next_start is None or gap > MOD_HOLD_MAX:
                        self.sync_mods(set())      # 长时间空闲，把修饰键松开
                    continue
                if self.sync_mods(set(mods)):
                    time.sleep(MOD_DELAY)
                result = self._wait_for(due)
                if result is False:
                    break
                if result == 'resume':
                    t0 = time.perf_counter() - start
                    self._t0 = t0
                    continue
                # 按住时长 = midi 里这个音的时值，但不短于 min_hold（太短游戏会吞音）；
                # 同时留出 RELEASE_GAP，别压到下一个音头上
                hold = max(duration, self.min_hold)
                if gap is not None:
                    limit = max(MIN_TAP, gap - RELEASE_GAP)
                    if hold > limit:
                        hold = limit
                        if hold < self.min_hold:
                            tight += 1
                result = self._hold(key, due + hold)
                played += 1
                if result is False:
                    break
                if result == 'resume':
                    t0 = time.perf_counter() - start
                    self._t0 = t0
                    continue
                if on_progress is not None:
                    now = time.perf_counter()
                    if index + 1 == total or now - last_report >= 0.05:
                        last_report = now
                        on_progress(index + 1, total)

                keep = set()
                if gap is not None and gap <= MOD_HOLD_MAX and index + 1 < total:
                    next_token = events[index + 1][0]
                    if not jianpu.is_rest(next_token):
                        keep = set(jianpu.parse_token(next_token)[0])
                if keep != set(mods):
                    self.sync_mods(keep)
            if on_progress is not None and not self.stop_flag.is_set():
                on_progress(total, total)          # 收尾时把进度补满
        finally:
            self.release_all()
            self._t0 = None
            self._paused_at = None
            self.running.clear()
        if self.stop_flag.is_set():
            self._say('已停止，共敲了 %d 个音' % played)
        else:
            self._say('演奏结束，共敲了 %d 个音' % played)
        if tight:
            self._say('有 %d 个音和下一个音挨得太近，按不到最短时长（想再稳一点就把速度调慢）' % tight)
        return not self.stop_flag.is_set()
