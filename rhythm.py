# -*- coding: utf-8 -*-
r"""
音游模式：判定 / 计分 / 成绩记录
================================

跟奏窗口只管「往下落、把按键喂进来」，判定和算分全在这个模块里 —— 纯逻辑、
不碰 Qt，命令行就能测。

判定
----
* 音符头到判定线，在 [开始 - 0.25 秒, 开始 + 0.40 秒] 之间按下对应琴键就算「按到」；
* 按住时长对不对：**相对误差**（|按住时长 - 音长| / 音长）≤10% 或**绝对误差** ≤90 毫秒
  是 perfect，≤30% 或 ≤200 毫秒是 good，再离谱是「按到了但不够准」
  （plain：**给分，只是比 good 少**，不加连击，显示上叫 MISS）；
  两个尺度取宽的那个 —— 快歌的音只有几十毫秒，光按百分比算没人做得到；
* 越过判定线 400 毫秒还没按，**或者下一个音已经开始弹了**，算漏按（miss）；
* 按了琴键但升降调 / 半音（鼠标键）不对，算按错键（wrong）。
  miss 和 wrong 都「算错」，但分开记、分开显示。

计分
----
perfect +100、good +60、plain +30（按到了但差太多，少给点分）、
wrong 0、miss 0（**不扣分** —— 一首都弹错也不至于负分）；
连击（perfect / good 连续）从第 3 个起，每多连一个多 +5，**不封顶**。

星级
----
一共 5 颗星：一次都没按错 / 漏按就是 5 颗黄星；有错就是 5 - ceil(错数 / 5)，最低 1 颗。
「错数」只算 **wrong + miss**：按到了但差太多（plain）给了分，不算错。
黄星里能升级成炫彩星的：
* perfect 占比 ≥ 90%         -> 1 颗
* 最长连击 ≥ 总音符的 1/3     -> 1 颗
* perfect 占比 ≥ 70%，或最长连击 ≥ 总音符的 2/3 -> 1 颗
全 perfect（一个不漏、全是 perfect）直接 5 颗全炫彩。

记录
----
* 本机：%LOCALAPPDATA%\AutoPlay\rhythm_records.json，按曲子分开存，一首歌留最高的 50 条；
* 联网：曲库仓库的 音游记录/<曲名>/<时间>-<随机>.json —— **一条一个文件**，不做「一个大
  json 全量覆盖」（那样两个人同时上传必然撞车）。名字和上传都得用户自己点确认。
"""

import json
import math
import os
import random
import time

import library

# ---------- 判定档位 ----------
PERFECT = 'perfect'
GOOD = 'good'
PLAIN = 'plain'        # 按到了，但按住时长差太多：给分（比 good 少），不加连击，显示上叫 MISS
WRONG = 'wrong'        # 按错键（升降调 / 半音不对）
MISS = 'miss'          # 漏按

RESULTS = (PERFECT, GOOD, PLAIN, WRONG, MISS)

HOLD_PERFECT = 0.10    # 按住时长相对误差 ≤10% -> perfect
HOLD_GOOD = 0.30       # ≤30% -> good
# 绝对宽容（v1.1.1 加）：快歌的音只有几十 ~ 一百多毫秒，光按百分比算要求几毫秒 ——
# 人根本做不到。所以**相对误差**和**绝对误差**取宽的那个：满足任意一条就算这一档。
HOLD_PERFECT_MIN = 0.09    # 误差 ≤ 90 毫秒，一律 perfect
HOLD_GOOD_MIN = 0.20       # 误差 ≤ 200 毫秒，一律 good
LATE_LIMIT = 0.40      # 过了判定线这么久还没按 -> miss（v1.1.1 起从 0.15 放宽到 0.40）
EARLY_WINDOW = 0.25    # 抢拍这么早以内也算按到（和练习模式那个判定窗一致）

BASE_SCORE = {PERFECT: 100, GOOD: 60, PLAIN: 30, WRONG: 0, MISS: 0}
COMBO_STEP = 5         # 连击加成：从第 3 个起每多连一个 +5（不封顶）
COMBO_FROM = 3

FORMAT_SCORE = 'autoplay-rhythm-score'
NAME_LIMIT = 12            # 名字最多几个字
MAX_LOCAL = 50             # 本机一首歌最多留多少条
ONLINE_FETCH_LIMIT = 30    # 联网成绩一次最多读多少个文件（多了又慢又没人看）
ONLINE_KEEP = 200          # 拉回来之后最多留多少条

# 联网等多久：音游成绩都是几 KB 的小 json，连不上就早点说，别让用户干等。
NET_TIMEOUT = 8.0          # 单个请求最多等几秒
NET_DEADLINE = 12.0        # 整趟「列目录 + 读文件」加起来最多等几秒

# 线上榜单**只在 Gitee**（用户要求）：GitHub 那边国内经常拉不到，成绩数据不放那儿。
SITE_ONLY = library.SITE_GITEE

DEFAULT_REPO = {'site': library.SITE_GITEE, 'owner': 'juanneys', 'repo': 'midi-music',
                'branch': 'master', 'dir': library.RHYTHM_DIR}
CONFIG_NAME = 'rhythm.json'      # 仓库根目录那份（可选）：换仓库 / 目录不用重新打包
CONFIG_CACHE = 'rhythm_remote.json'   # 拉回来的那份存在 %LOCALAPPDATA%\AutoPlay\ 下
# 「哪些曲子有人传过成绩」的总目录（放在音游记录下面）。
# 有它在，就能分清「一首歌没人传过」和「网线不通」—— 前者去列目录会被 Gitee 报错，
# 用户看到的就成了「没读到联网成绩」，其实只是这首歌还没人传。
MAP_NAME = '曲目索引.json'
FORMAT_MAP = 'autoplay-rhythm-songs'


# ---------- 纯计算 ----------

def relative_error(hold, dur):
    """按住时长的相对误差：|按住 - 音长| / 音长。"""
    dur = max(float(dur or 0.0), 0.001)
    return abs(float(hold) - dur) / dur


def hold_error(hold, dur):
    """按住时长和音长的差（秒，绝对值）。"""
    return abs(float(hold or 0.0) - float(dur or 0.0))


def grade(hold, dur):
    """
    按住这么久算哪一档。

    相对误差（≤10% / ≤30%）和绝对误差（≤90 / ≤200 毫秒）**取宽的那个**：
    「提前按了一点、也提前松了」按住时长就短，百分比一看差得离谱，
    实际也就差几十毫秒 —— 这种该算按对，别因为音短就判成没按。
    """
    diff = hold_error(hold, dur)
    if diff <= HOLD_PERFECT_MIN or diff <= max(float(dur or 0.0), 0.001) * HOLD_PERFECT:
        return PERFECT
    if diff <= HOLD_GOOD_MIN or diff <= max(float(dur or 0.0), 0.001) * HOLD_GOOD:
        return GOOD
    return PLAIN


def miss_total(counts):
    """「按错 + 漏按」一共多少个 —— 星级按这个算（«按到了但差太多»给了分，不算错）。"""
    counts = counts or {}
    return int(counts.get(WRONG, 0)) + int(counts.get(MISS, 0))


def stars(total, counts):
    """几颗星（黄星总数）。"""
    total = int(total or 0)
    if total <= 0:
        return 0
    bad = miss_total(counts)
    if bad <= 0:
        return 5
    return max(1, 5 - int(math.ceil(bad / 5.0)))


def rainbow(total, counts, max_combo):
    """5 颗星里有几颗炫彩星。"""
    total = int(total or 0)
    if total <= 0 or miss_total(counts) > 0:
        return 0
    counts = counts or {}
    perfect_ratio = int(counts.get(PERFECT, 0)) / float(total)
    combo_ratio = float(max_combo or 0) / float(total)
    if perfect_ratio >= 1.0:
        return 5
    count = 0
    if perfect_ratio >= 0.9:
        count += 1
    if combo_ratio >= 1.0 / 3.0:
        count += 1
    if perfect_ratio >= 0.7 or combo_ratio >= 2.0 / 3.0:
        count += 1
    return count


def clean_name(text):
    """把用户填的名字洗干净：没有换行、前后不留空白、最多 12 个字。"""
    name = ''.join(ch for ch in str(text or '') if ch not in '\r\n\t').strip()
    return name[:NAME_LIMIT]


class Session(object):
    """
    一首曲子的判定会话。

    跟奏窗口每帧 tick(now)，按下 / 松开分别调 hit / release，返回「刚刚发生了哪些
    判定」（列表，元素是 (档位, 音符)），窗口拿它去画 HUD。
    """

    def __init__(self, notes, early=EARLY_WINDOW, late=LATE_LIMIT):
        self.notes = list(notes)
        self.early = float(early)
        self.late = float(late)
        self.cursor = 0
        self.judged = [False] * len(self.notes)
        self.open = {}                 # lane -> (索引, 按下时刻, 音符)
        self.counts = dict((key, 0) for key in RESULTS)
        self.score = 0
        self.combo = 0
        self.max_combo = 0

    # ---- 内部 ----

    def _holding(self, index):
        for held in self.open.values():
            if held[0] == index:
                return True
        return False

    def _advance(self):
        while self.cursor < len(self.notes):
            if self.judged[self.cursor] or self._holding(self.cursor):
                self.cursor += 1
            else:
                break

    def _score(self, result):
        self.counts[result] = self.counts.get(result, 0) + 1
        self.score += BASE_SCORE.get(result, 0)
        if result in (PERFECT, GOOD):
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            if self.combo >= COMBO_FROM:
                self.score += COMBO_STEP * (self.combo - COMBO_FROM + 1)
        else:
            self.combo = 0

    # ---- 对外 ----

    @property
    def done(self):
        return self.cursor >= len(self.notes) and not self.open

    def pending(self):
        """下一个等着判定的音（没有就 None）。"""
        return self.notes[self.cursor] if self.cursor < len(self.notes) else None

    def tick(self, now):
        """往前走一步：把「已经错过」的音判成漏按，把按住不放的收尾。"""
        out = []
        while self.cursor < len(self.notes):
            note = self.notes[self.cursor]
            nxt = self.notes[self.cursor + 1] if self.cursor + 1 < len(self.notes) else None
            late = now > note.start + self.late
            # 下一个音都开始弹了，这个还没按到 —— 也算漏了（不然会一直等它）
            stepped = nxt is not None and now >= nxt.start
            if not (late or stepped):
                break
            self.judged[self.cursor] = True
            self._score(MISS)
            out.append((MISS, note))
            self._advance()
        for lane, (index, down, note) in list(self.open.items()):
            if now > note.start + max(note.dur, 0.2) * 2.0:      # 一直不松手
                del self.open[lane]
                self._score(PLAIN)
                out.append((PLAIN, note))
                self._advance()
        return out

    def hit(self, lane, combo, now):
        """按下：琴键要对得上那一列，鼠标组合也要对得上这个音的记号。"""
        out = self.tick(now)
        if lane in self.open:                # 这一列还按着（上一拍的松手事件丢了）：
            out.extend(self.release(lane, now))   # 先把它结掉，别把它的判定吃掉
        if self.cursor >= len(self.notes):
            return out
        note = self.notes[self.cursor]
        if now < note.start - self.early or now > note.start + self.late:
            return out                       # 太早 / 太晚：当没按
        self.judged[self.cursor] = True
        if note.lane == lane and note.color == combo:
            self.open[lane] = (self.cursor, now, note)      # 按住了，等松手看时长
        else:
            self._score(WRONG)
            out.append((WRONG, note))
        self._advance()
        return out

    def release(self, lane, now):
        """松开：这时候才知道按住时长够不够。"""
        out = []
        got = self.open.pop(lane, None)
        if got is not None:
            index, down, note = got
            hold = max(now - max(down, note.start), 0.0)
            result = grade(hold, note.dur)
            self._score(result)
            out.append((result, note))
        self._advance()
        return out

    def summary(self):
        counts = dict(self.counts)
        total = len(self.notes)
        # 用户要求：界面上不要「不达标」这种词，一律叫 MISS ——
        # 所以「按到了但差太多」和「漏按」合并成一个 miss 数（分数算法不变）。
        miss = counts[MISS] + counts[PLAIN]
        return {'notes': total,
                'score': int(self.score),
                'max_combo': int(self.max_combo),
                'perfect': counts[PERFECT],
                'good': counts[GOOD],
                'plain': counts[PLAIN],
                'wrong': counts[WRONG],
                'miss': miss,
                'miss_only': counts[MISS],
                'stars': stars(total, counts),
                'rainbow': rainbow(total, counts, self.max_combo)}

    def abandon(self):
        """
        中途停下：把**还没判过的音**全算漏按。

        不然半截成绩会虚高 —— 弹了两个音就退出，只算那两个音的分，
        星级还能五颗。没弹到的一律 miss，分数才作数。
        """
        out = []
        for index, note in enumerate(self.notes):
            if self.judged[index]:
                continue
            self.judged[index] = True
            self._score(MISS)
            out.append((MISS, note))
        self.open.clear()
        self.cursor = len(self.notes)
        return out


# ---------- 成绩记录（本机） ----------

def song_key(path):
    """曲子的身份：就用文件名（国内源和 GitHub 源是同一个名字，两边能合起来比）。"""
    name = os.path.basename(str(path or '').replace('\\', '/')).strip()
    return name or '未知曲子'


def _appdata():
    return os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')


def records_path():
    return os.path.join(_appdata(), 'AutoPlay', 'rhythm_records.json')


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        return True
    except OSError:
        return False


def load_local(song=None):
    """本机成绩：不传 song 就整份拿回来，传了就只拿那一首。"""
    data = _read_json(records_path())
    if song is None:
        return data
    items = data.get(song)
    return items if isinstance(items, list) else []


def save_local(song, entry):
    """存一条本机成绩，返回这首歌排完序之后的榜单。"""
    path = records_path()
    data = _read_json(path)
    items = data.get(song)
    if not isinstance(items, list):
        items = []
    items.append(dict(entry))
    items.sort(key=lambda item: int(item.get('score') or 0), reverse=True)
    data[song] = items[:MAX_LOCAL]
    _write_json(path, data)
    return data[song]


def entry_of(summary, name, version=''):
    """把一次结算整理成要存 / 要传的一条记录。"""
    entry = dict(summary or {})
    entry['format'] = FORMAT_SCORE
    entry['name'] = clean_name(name)
    entry['app_version'] = str(version or '')
    entry['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
    return entry


# ---------- 成绩记录（联网） ----------

def _safe(text, limit=60):
    keep = ''.join(ch if (ch.isalnum() or ch in '-_') else '_' for ch in str(text or ''))
    return keep[:limit] or 'song'


def _config_path():
    return os.path.join(_appdata(), 'AutoPlay', CONFIG_CACHE)


def remote_config():
    """上次从仓库拉到的配置（换仓库 / 换成绩目录不用重新打包程序）。"""
    return repo_config(_read_json(_config_path()))


def refresh(timeout=None):
    """
    联网把仓库根目录那份 rhythm.json 拉回来。

    跟错误上报那份配置一个路子：拉到了就存本机一份；拉不到就接着用上次的 /
    内置默认值（成绩一样能存能传，只是按默认仓库走）。
    """
    try:
        import notice as notice_mod
    except Exception:
        return False
    try:
        if timeout is None:
            text, _why = notice_mod.text_file(CONFIG_NAME)
        else:
            text, _why = notice_mod.text_file(CONFIG_NAME, timeout=timeout)
    except Exception:
        return False
    if not text:
        return False
    try:
        data = json.loads(text)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    return _write_json(_config_path(), data)


def repo_config(remote=None):
    """写到哪个仓库：仓库里那份 rhythm.json > 程序内置的默认值。"""
    if remote is None:
        remote = remote_config()
    out = dict(DEFAULT_REPO)
    if isinstance(remote, dict):
        for key in ('site', 'owner', 'repo', 'branch', 'dir'):
            value = remote.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
    out['site'] = SITE_ONLY          # 线上排名只在 Gitee：别让配置把它指到 GitHub 去
    return out


def remote_dir(song, folder=None):
    """这首歌在仓库里的目录。"""
    return '%s/%s' % (str(folder or DEFAULT_REPO['dir']).strip('/'), _safe(song))


def map_path(cfg=None):
    """「哪些曲子有成绩」那份总目录在仓库里的路径。"""
    cfg = cfg or repo_config()
    return '%s/%s' % (str(cfg.get('dir') or DEFAULT_REPO['dir']).strip('/'), MAP_NAME)


def _has_song(mapping, song):
    """表里有没有这首歌（大小写不敏感）。"""
    key = str(song or '').strip().lower()
    if not key:
        return False
    for name in mapping or {}:
        if str(name).strip().lower() == key:
            return True
    return False


def score_map(remote=None, timeout=NET_TIMEOUT):
    """
    读仓库里那份「哪些曲子有人传过成绩」的总目录：({曲名: {...}} 或 None, 出错信息)。

    * 读到了 → (表, '')；表里没有这首 = **这首歌根本没人传过**，不是网络问题；
    * 索引文件还不存在（第一次用 / 仓库刚建）→ ({}, '')，也当「都没传过」；
    * 真读不到（连不上 / 接口报错）→ (None, 说明)，这才该说「请检查网络连接」。
    """
    cfg = repo_config(remote)
    try:
        token = str(library.get_token(cfg['site']) or '')
    except Exception:
        token = ''
    text, exists, why = library.read_file_optional(
        cfg['site'], cfg['owner'], cfg['repo'], map_path(cfg), token,
        branch=cfg['branch'], timeout=timeout)
    if why:
        return None, why
    if not exists or not text:
        return {}, ''
    try:
        data = json.loads(text)
    except ValueError:
        return {}, ''
    songs = data.get('songs') if isinstance(data, dict) else None
    if not isinstance(songs, dict):
        return {}, ''
    return dict((str(name), info) for name, info in songs.items()), ''


def bump_score_map(song, entry, remote=None, timeout=NET_TIMEOUT):
    """
    传完成绩，顺手把总目录更新一下（这首歌传了几次、最高多少分）。

    **尽力而为**：撞车 / 网差更新不了就算了 —— 那份目录只是让界面能分清
    「没人传过」和「网线不通」，成绩本身还是那条 json（`fetch` 以目录为准，
    目录里漏了也只会让这首歌显示成「还没人传过」，不会把已有的成绩弄丢）。
    """
    cfg = repo_config(remote)
    try:
        token = str(library.get_token(cfg['site']) or '')
    except Exception:
        token = ''
    if not token:
        return False
    path = map_path(cfg)
    try:
        sha, why = library.remote_sha(cfg['site'], cfg['owner'], cfg['repo'], path,
                                      token=token, ref=cfg['branch'])
        if why:
            return False
        text = ''
        if sha:
            text, _why = library.read_text_file(cfg['site'], cfg['owner'], cfg['repo'], path,
                                                token, branch=cfg['branch'], timeout=timeout)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        songs = data.get('songs')
        if not isinstance(songs, dict):
            songs = {}
        info = dict(songs.get(str(song)) or {}) if isinstance(songs.get(str(song)), dict) else {}
        info['count'] = int(info.get('count') or 0) + 1
        info['time'] = str(entry.get('time') or info.get('time') or '')
        info['best'] = max(int(info.get('best') or 0), int(entry.get('score') or 0))
        songs[str(song)] = info
        body = json.dumps({'format': FORMAT_MAP, 'songs': songs},
                          ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
        _url, why2 = library.put_file(cfg['site'], cfg['owner'], cfg['repo'], path, body,
                                      token, branch=cfg['branch'], sha=sha,
                                      message='AutoPlay 音游曲目索引：%s' % song,
                                      timeout=timeout)
        return not why2
    except Exception:
        return False


def in_library(song, timeout=None):
    """
    这首歌在不在**联网曲库**里（按文件名比，大小写不敏感）。

    只有联网曲库里的曲子才允许上传成绩参加线上排名 —— 本机自己转的 mp3、自己做的曲子
    随便玩音游、本机记录照存，但传上去别人也下不到，只会把成绩目录弄乱。

    返回 (结果, 说明)：
    * True  = 在曲库里，可以上传；
    * False = 不在曲库里（上传门禁挡住）；
    * None  = 没查成（连不上 / 仓库里没有索引），说明写在第二个值里。
    """
    key = os.path.basename(str(song or '').replace('\\', '/')).strip().lower()
    if not key:
        return False, '这首歌没有文件名'
    try:
        songs, why = library.fetch_index(timeout=timeout or NET_TIMEOUT)
    except Exception as exc:                  # 后台线程里绝不能把异常漏出去
        return None, str(exc)
    if not songs:
        return None, why or '没拉到联网曲库'
    for item in songs:
        name = os.path.basename(str(item.get('file') or '').replace('\\', '/')).strip().lower()
        if name and name == key:
            return True, ''
    return False, ''


def upload(song, entry, remote=None, timeout=NET_TIMEOUT):
    """把一条成绩传上仓库（一条一个文件）。返回 (网页地址, 出错信息)。"""
    cfg = repo_config(remote)
    try:
        token = str(library.get_token(cfg['site']) or '')
    except Exception:
        token = ''
    if not token:
        return '', '没有令牌，传不上去'
    name = '%s-%04d.json' % (time.strftime('%Y%m%d-%H%M%S'), random.randint(0, 9999))
    path = '%s/%s' % (remote_dir(song, cfg['dir']), name)
    body = json.dumps(entry, ensure_ascii=False, indent=2).encode('utf-8')
    url, why = library.put_file(cfg['site'], cfg['owner'], cfg['repo'], path, body, token,
                                branch=cfg['branch'],
                                message='AutoPlay 音游成绩 %s %s 分'
                                        % (song, entry.get('score')),
                                timeout=timeout)
    if why:
        return url, why
    bump_score_map(song, entry, remote, timeout=timeout)   # 总目录：尽力而为，失败不影响成绩
    return url, ''


def fetch(song, remote=None, timeout=NET_TIMEOUT, limit=ONLINE_FETCH_LIMIT):
    """
    把一首歌的联网成绩拉回来：([记录, ...], 出错信息)。

    一条记录一个文件（见 upload），所以得先列目录、再一个个读 —— 只读最新的
    那些（文件名前面就是时间），不然文件多了要等很久。
    """
    cfg = repo_config(remote)
    try:
        token = str(library.get_token(cfg['site']) or '')
    except Exception:
        token = ''
    folder = remote_dir(song, cfg['dir'])
    files, why = library.list_dir(cfg['site'], cfg['owner'], cfg['repo'], folder,
                                  token=token, branch=cfg['branch'], timeout=timeout)
    if why:
        # 列目录失败：可能真连不上，也可能只是**这首歌还没人传过**（Gitee 对不存在的
        # 目录也会报错）。看一眼总目录就知道是哪种 —— 目录里没有这首 = 没人传过。
        known, why_map = score_map(remote, timeout=timeout)
        if known is not None and not _has_song(known, song):
            return [], ''
        return [], why
    if not files:
        return [], ''
    files = [item for item in files
             if os.path.basename(str(item.get('path') or '')) != MAP_NAME]
    files.sort(key=lambda item: item.get('path') or '', reverse=True)
    out = []
    last = ''
    deadline = time.time() + NET_DEADLINE
    for item in files[:max(1, int(limit))]:
        if time.time() > deadline:
            last = last or '联网成绩读取超时'
            break
        text, why_one = library.read_text_file(cfg['site'], cfg['owner'], cfg['repo'],
                                               item.get('path') or '', token,
                                               branch=cfg['branch'], timeout=timeout)
        if why_one:
            last = why_one
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('format') == FORMAT_SCORE:
            data.pop('format', None)
            out.append(data)
    out.sort(key=lambda item: int(item.get('score') or 0), reverse=True)
    return out[:ONLINE_KEEP], last
