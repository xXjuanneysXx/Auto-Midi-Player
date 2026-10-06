# -*- coding: utf-8 -*-
"""
联网曲库：曲子放在 Gitee / GitHub 上，程序自己去拉
==================================================

没有服务器也能共享曲库：把 midi 传到一个仓库里，再放一个**索引文件**
`library.json`，程序读这一个地址就知道有哪些曲子、每首多大、在仓库里叫什么。

    仓库结构（随便你，路径写在索引里就行）
        library.json
        songs/鸟之诗.mid
        songs/xxx.mid

    library.json
        {
          "version": 1,
          "songs": [
            {"title": "鸟之诗", "artist": "Lia", "file": "songs/鸟之诗.mid",
             "size": 9031, "sha1": "可写可不写"},
            ...
          ]
        }

    `file` 写**相对于索引文件的路径**（上面就是 songs/xxx.mid），所以程序只要一个
    索引地址就能推算出每首曲子的下载地址。

两套曲库：国内（Gitee）/ GitHub
-------------------------------
默认走**国内曲库（Gitee）** —— 国内直连 GitHub 的 raw 经常连不上。界面上能一键
切换，选哪套记在 `%LOCALAPPDATA%\\AutoPlay\\library\\source_name.txt` 里，重启也记得。

地址分别写在（打包时定下来的默认值）：

    library_source.py   GITEE_INDEX_URL = https://gitee.com/<你>/<仓库>/raw/<分支>/library.json
                        INDEX_URL       = https://cdn.jsdelivr.net/gh/<你>/<仓库>@<分支>/library.json

想临时换个仓库（或者源挂了想换一个）不用重新打包：往
`%LOCALAPPDATA%\\AutoPlay\\library\\source.txt` 写一行地址就顶掉了当前那套。

⚠ Gitee 的一个坑：它的 raw 直链会过内容审核，library.json 这种「里面一堆中文歌名」
的文本文件会被挡下来（HTTP 451，换文件名也没用）。所以 Gitee 的**索引走 API**
（contents 接口，公开仓库匿名就能读），只有 midi 文件才走 raw 直链 —— midi 是
二进制，实测不会被挡。

没有 library.json 也能用（兜底）
-------------------------------
索引文件是「给程序看的一份歌单」，得先有人把它放进仓库。要是仓库里**只有
midi、没有索引**，程序不会干等着：它发现索引拉不到，就**干脆直接问这个仓库里
有哪些文件**（文件列表 API），把里面所有 `.mid / .midi` 当成歌单。
所以一个「只往里丢 midi」的仓库开箱就能用，不用额外维护 library.json。

（这一下是匿名请求。GitHub 对匿名调用有次数限制 —— 一小时几十次；Gitee 的
git/trees 接口匿名调没问题。刷新歌单够用了。真要传曲子、改索引，还是得有令牌，见下面。）

拉不到会怎么样
--------------
**什么都不影响**：下载过的曲子都在本地缓存里，程序自带的 `songs\\` 也照常能用。
拉索引失败只是「联网曲库」那个窗口里多一行说明，别的功能一点不碰。

上传（把曲子传回仓库）
----------------------
程序也能往仓库里传东西 —— 用 Contents API，要靠一个**访问令牌**（仓库读写）。
两个令牌都是**内置在程序里**的：Gitee 在 gitee_token_local.py、GitHub 在
github_token_local.py（这两个文件不进 git，只有自己这台机器和自己打出来的包里有）。
界面上没有「填令牌」这一项，打开就能传。

想让程序用别的令牌：改对应文件里的 TOKEN，重新打包。

传一首曲子做两件事：把 midi 写进仓库，再把 library.json 按仓库里现有的文件重新
生成一遍（所以索引不会写着写着就和仓库对不上）。**上传时两套曲库都传**，
传一次两个仓库都有 —— 国内用户走 Gitee，海外 / Gitee 挂了还能走 GitHub。

曲库是公开的、大家一起用的仓库：传上去就是分享出去，所有人都能看见、能下载。

两条路，任选：

    library.upload_song_all(本地文件, 标题, 艺术家)         # 两套曲库都传
    library.refresh_index_all()                            # 只重排索引，不传新文件
    library.upload_song(..., site='gitee')                 # 只传某一套
    library.refresh_index(owner, repo, branch, site=...)   # 只重排某一套
"""

import base64
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

APP_FOLDER = 'AutoPlay'
INDEX_NAME = 'library.json'
# 拉索引最多等这么久（秒）；网断了也要几秒钟就回来，不能把界面卡住
INDEX_TIMEOUT = 6.0
# 下载一首曲子最多等这么久（秒）。曲库里的 midi 都是几十 KB 的小文件，10 秒还没
# 下完基本就是网络不通了 —— 早点了断，别让界面干等着（用户会以为程序卡死）。
SONG_TIMEOUT = 10.0
# 缓存里的索引多久算「旧」（秒）：旧的先拿本地那份顶上，同时后台再刷新
INDEX_STALE = 6 * 3600
USER_AGENT = 'AutoPlay/1.0 (+midi jianpu player)'
# GitHub 的 API 根地址（上传、列目录、改索引都走它）
GITHUB_API = 'https://api.github.com'
# API 调用（尤其上传）超时放宽一点：传文件本来就慢
API_TIMEOUT = 30.0
# 备用令牌存哪儿（内置那个能用的话用不到它）
TOKEN_NAME = 'github_token.txt'
# 什么样的文件算「曲子」
MIDI_SUFFIX = ('.mid', '.midi')
# 下架名单：这些曲子不再出现在联网歌单里（按文件名 / 标题匹配，子串就算）。
# 「邓垚 - 诀别书」是个 10MB 的大文件，仓库里早就删了，可旧索引里还留着一条 ——
# 用户点它只会白等到超时，看着就像程序卡死。拉歌单时顺手滤掉，不用等索引重排。
HIDDEN_SONGS = ('邓垚 - 诀别书',)

# 曲库仓库里跟曲子无关的元文件（索引 / 公告 / 版本 / 更新清单 / 错误上报配置）。
# 正常它们不会出现在 library.json 的 songs 里，但万一手滑写进去，这里兜一层：
# 别把 error_report.json 这种配置当成曲子解析出来。
META_FILES = ('library.json', 'notice.json', 'version.json', 'update.json',
              'themes.json', 'error_report.json', 'rhythm.json', 'payload.json')
ERROR_REPORT_DIR = '错误报告'
RHYTHM_DIR = '音游记录'          # 音游成绩（一条一个 json）
MANUAL_DIR = '快速上手'          # 程序内「快速上手」手册（md）
MANIFEST_DIR = 'manifests'       # 每一版的文件清单（发版留档，客户端不读）


def is_meta_path(path):
    """这个索引路径是不是曲库仓库的元文件（是就别当曲子）。"""
    text = str(path or '').replace('\\', '/').strip().lower()
    if not text:
        return True
    if text.rsplit('/', 1)[-1] in META_FILES:
        return True
    # 这几个目录里的东西也一律不是曲子：错误报告 / 音游成绩 / 快速上手手册
    return any(folder in text for folder in (ERROR_REPORT_DIR, RHYTHM_DIR, MANUAL_DIR,
                                             MANIFEST_DIR))


def is_hidden(song):
    """这首歌在不在下架名单里。"""
    if not isinstance(song, dict):
        return False
    text = ('%s %s' % (song.get('file') or '', song.get('title') or '')).lower()
    return any(word.lower() in text for word in HIDDEN_SONGS)
# 源代码里带的占位符：别人拿到源码时看到的是一眼假的字符串，程序也会当「没内置令牌」
# 处理（不然它真拿着这串去请求，只会换来一个莫名其妙的 401）。
TOKEN_PLACEHOLDER = 'GITHUB_PERSONAL_TOKEN'

# 打包时写进来的默认索引地址（library_source.py 由打包脚本生成）
try:
    from library_source import INDEX_URL as BAKED_URL
except Exception:                                   # pragma: no cover
    BAKED_URL = ''

# 内置令牌（github_token_local.py）：令牌写死在程序里，界面上没有让人填的地方。
# 那个文件不进 git，所以只在自己这台机器 / 自己打出来的包里存在。
try:
    from github_token_local import TOKEN as BAKED_TOKEN
except Exception:                                   # pragma: no cover
    BAKED_TOKEN = ''

# ============ 两套曲库：国内（Gitee）/ GitHub ============
#
# 国内直连 GitHub 经常不通，所以默认走 Gitee；连不上时界面上能一键切到 GitHub。
# 上传的时候两边都传（传一次，两个仓库都有），下载时由用户选一边。
#
# Gitee 的一个坑：raw 直链会过内容审核，library.json 这种「里面一堆中文歌名」的
# 文本文件会被挡（HTTP 451）。所以 Gitee 这边的**索引走 API**（contents 接口），
# 只有 midi 文件才走 raw 直链 —— midi 是二进制，实测不会被挡。
GITEE_API = 'https://gitee.com/api/v5'
SITE_GITEE = 'gitee'
SITE_GITHUB = 'github'
SITE_ORDER = (SITE_GITEE, SITE_GITHUB)          # 第一个是默认（国内直连更稳）
SITE_LABELS = {SITE_GITEE: '国内曲库（Gitee）',
               SITE_GITHUB: 'GitHub 曲库'}
# 内置的默认地址（打包时 library_source.py 里的值优先）
DEFAULT_GITEE_URL = 'https://gitee.com/juanneys/midi-music/raw/master/library.json'
DEFAULT_GITHUB_URL = ('https://raw.githubusercontent.com/xXjuanneysXx/'
                      'midi-music/main/library.json')
# 选的是哪套曲库（本机覆盖，重启也记得）
SOURCE_NAME_FILE = 'source_name.txt'
# Gitee 的备用令牌存哪儿（内置那个能用的话用不到它）
GITEE_TOKEN_NAME = 'gitee_token.txt'
GITEE_TOKEN_PLACEHOLDER = 'GITEE_ACCESS_TOKEN'

# 打包时写进来的 Gitee 默认索引地址（没有就退回内置那个）
try:
    from library_source import GITEE_INDEX_URL as BAKED_GITEE_URL
except Exception:                                   # pragma: no cover
    BAKED_GITEE_URL = ''

# 内置的 Gitee 令牌（gitee_token_local.py，同样不进 git）
try:
    from gitee_token_local import TOKEN as BAKED_GITEE_TOKEN
except Exception:                                   # pragma: no cover
    BAKED_GITEE_TOKEN = ''


def _local_appdata():
    return os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()


def cache_dir():
    """下载下来的曲子放哪儿。"""
    return os.path.join(_local_appdata(), APP_FOLDER, 'library')


def source_file():
    """本地那个「索引地址」文件（优先级比打包进去的高）。"""
    return os.path.join(cache_dir(), 'source.txt')


def source_name():
    """现在选的是哪套曲库：'gitee'（国内，默认）/ 'github'。"""
    try:
        with open(os.path.join(cache_dir(), SOURCE_NAME_FILE), 'r',
                  encoding='utf-8') as handle:
            name = handle.read().strip().lower()
    except OSError:
        name = ''
    return name if name in SITE_LABELS else SITE_GITEE


def set_source_name(name):
    """
    切换曲库（记住选择）。顺便把「自定义地址」清掉 —— 它优先级最高，
    不清的话切了也看不出变化。
    """
    name = str(name or '').strip().lower()
    if name not in SITE_LABELS:
        return False
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        with open(os.path.join(cache_dir(), SOURCE_NAME_FILE), 'w',
                  encoding='utf-8') as handle:
            handle.write(name + '\n')
        if os.path.isfile(source_file()):
            os.remove(source_file())
        return True
    except OSError:
        return False


def default_index_url(name=None):
    """某套曲库的默认索引地址：打包时写进来的 > 内置的。"""
    name = name or source_name()
    if name == SITE_GITHUB:
        return str(BAKED_URL or '').strip() or DEFAULT_GITHUB_URL
    return str(BAKED_GITEE_URL or '').strip() or DEFAULT_GITEE_URL


def source_url():
    """现在用哪个索引地址：本地覆盖文件 > 当前那套曲库的默认地址。"""
    try:
        with open(source_file(), 'r', encoding='utf-8') as handle:
            text = handle.read().strip()
        if text:
            return text
    except OSError:
        pass
    return default_index_url()


def set_source_url(url):
    """把索引地址写到本地覆盖文件里（程序里改源用，不用重新打包）。"""
    url = str(url or '').strip()
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        if url:
            with open(source_file(), 'w', encoding='utf-8') as handle:
                handle.write(url + '\n')
        elif os.path.isfile(source_file()):
            os.remove(source_file())
        return True
    except OSError:
        return False


def mirror_urls(url):
    """
    除了原地址，还能从哪些镜像拿（现在只有 GitHub raw -> jsDelivr 这一条）。

    ⚠ jsDelivr 对「分支地址」（@main / @master）是**长缓存**：实测 12 小时
    （响应头 s-maxage=43200），而且**查询串不参与它的缓存键** —— 加 ?_=时间戳
    一点用都没有（实测三个不同 ?_ 的地址回的是同一份副本）。
    所以「会变的内容」（version.json / notice.json / update.json / library.json
    这些索引 json）不能把镜像当第一选择：调 _try_all 时带 mirror=False，
    镜像只留作最后的兜底。midi 那种传上去就不太动的二进制文件用镜像没问题。
    """
    out = []
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return out
    if parts.netloc.lower() == 'raw.githubusercontent.com':
        bits = parts.path.lstrip('/').split('/')
        if len(bits) >= 4:                      # 用户 / 仓库 / 分支 / 路径…
            user, repo, branch = bits[0], bits[1], bits[2]
            rest = '/'.join(bits[3:])
            out.append('https://cdn.jsdelivr.net/gh/%s/%s@%s/%s' % (user, repo, branch, rest))
    return out


def mirrors(url):
    """
    同一个东西可以试的几个地址：第一个是原地址，后面是镜像。

    GitHub 的 raw 在国内经常连不上，所以给 raw 地址自动补一个 jsDelivr 的镜像
    （同一个仓库、同一个文件，CDN 分发，一般能通）。列表里的顺序就是尝试顺序。
    """
    return [url] + mirror_urls(url)


def _quote_url(url):
    """
    把地址里的非 ASCII 字符转义掉。

    曲库里多半是中文文件名（songs/鸟之诗.mid），而 http.client 组请求行的时候只认
    ascii，直接拿中文去请求会抛 "ordinal not in range(128)"。已经转义好的 (%E9…) 不动。
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme in ('', 'file'):
            return url
        path = urllib.parse.quote(parts.path, safe="/%:@&=+$,;~()!*'")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path,
                                        parts.query, parts.fragment))
    except Exception:                                # pragma: no cover
        return url


def _get(url, timeout):
    """下载一个地址的内容（字节）。"""
    request = urllib.request.Request(_quote_url(url), headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _try_all(url, timeout, what, total=None, mirror=True):
    """
    挨个试镜像，成功就返回 (内容, 出错信息)。

    timeout 是「每个地址最多等多久」，total 是「这一轮总共最多等多久」—— 不能几个
    镜像各等一遍（两个地址就翻倍，用户会觉得界面卡死了）。total 不写就按「每个地址
    各等一遍」来，给「拉索引」那种本来就要多试几个地址的场合留余地。
    mirror=False 就只试原地址 —— 拉会变的 json 时这么用（镜像有 12 小时长缓存，
    而且查询串不进它的缓存键，见 mirror_urls）。
    """
    candidates = list(mirrors(url)) if mirror else [url]
    budget = float(total) if total else float(timeout) * max(1, len(candidates))
    deadline = time.monotonic() + max(1.0, budget)
    last = ''
    for index, candidate in enumerate(candidates):
        left = deadline - time.monotonic()
        if left <= 0.5:
            break
        share = max(0.5, min(float(timeout), left / max(1, len(candidates) - index)))
        try:
            return _get(candidate, share), ''
        except socket.timeout:
            last = '连不上（等超时了，网线 / 代理 / 需要梯子都有可能）'
        except urllib.error.HTTPError as exc:
            last = '服务器回了 %s（地址对不对？仓库是不是私有的？）' % exc.code
        except urllib.error.URLError as exc:
            reason = getattr(exc, 'reason', None)
            if isinstance(reason, socket.timeout):
                last = '连不上（等超时了，网线 / 代理 / 需要梯子都有可能）'
            else:
                last = '连不上：%s' % (reason or exc)
        except (OSError, ValueError) as exc:
            last = '连不上：%s' % (getattr(exc, 'reason', None) or exc)
        except Exception as exc:                    # pragma: no cover
            last = '出错：%s' % exc
    return None, last or ('%s 打不开' % what)


def parse_index(text):
    """索引文件 -> [歌, ...]；看不懂就返回 None。"""
    try:
        data = json.loads(text)
    except Exception:
        return None
    songs = data.get('songs') if isinstance(data, dict) else data
    if not isinstance(songs, list):
        return None
    out = []
    for item in songs:
        if not isinstance(item, dict):
            continue
        path = str(item.get('file') or item.get('path') or '').strip()
        if not path or is_meta_path(path):
            continue
        song = {
            'title': str(item.get('title') or os.path.splitext(os.path.basename(path))[0]),
            'artist': str(item.get('artist') or ''),
            'file': path,
            'size': int(item.get('size') or 0),
            'sha1': str(item.get('sha1') or '').strip().lower(),
        }
        if is_hidden(song):                     # 下架名单里的直接不列（见 HIDDEN_SONGS）
            continue
        out.append(song)
    return out


def cached_index():
    """上次拉到的索引（可能没有）。"""
    try:
        with open(os.path.join(cache_dir(), INDEX_NAME), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except Exception:
        return [], 0.0
    songs = data.get('songs') if isinstance(data, dict) else None
    when = float(data.get('fetched') or 0.0) if isinstance(data, dict) else 0.0
    songs = songs if isinstance(songs, list) else []
    return [song for song in songs if not is_hidden(song)], when


def save_index(songs):
    """把索引存一份，下次网断了也能看见「有哪些曲子」。"""
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        with open(os.path.join(cache_dir(), INDEX_NAME), 'w', encoding='utf-8') as handle:
            json.dump({'fetched': time.time(), 'songs': songs}, handle,
                      ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def songs_from_repo(url=None, timeout=INDEX_TIMEOUT, token='', site=''):
    """
    兜底：不问索引文件，直接问「这个仓库里有哪些文件」。

    用它的好处是仓库里**只有 midi 也能用**（不用先准备 library.json）。
    `file` 写相对路径，所以下面那套「索引地址 + file」拼下载地址的逻辑一个字都不用改。

    返回 (歌单, 出错信息)。GitHub 匿名也能调，但有次数限制（一小时几十次）；
    Gitee 的 git/trees 接口匿名调没问题。
    """
    site2, owner, repo, branch, why = backend_of(url)
    if why:
        return [], why
    site = site or site2
    files, why = repo_files(site, owner, repo, branch, token,
                            timeout=max(float(timeout or 0), API_TIMEOUT))
    if why:
        return [], why
    songs = []
    for item in files:
        path = str(item.get('path') or '')
        if not is_midi(path):
            continue
        song = {
            'title': os.path.splitext(os.path.basename(path))[0],
            'artist': '',
            'file': path,
            'size': int(item.get('size') or 0),
            'sha1': '',
        }
        if is_hidden(song):
            continue
        songs.append(song)
    songs.sort(key=lambda song: song['file'])
    if not songs:
        return [], '这个仓库里一个 midi 都没有（%s）' % repo
    return songs, ''


def songs_from_gitee(url=None, timeout=INDEX_TIMEOUT):
    """
    国内曲库（Gitee）的索引：**不走 raw 直链**。

    Gitee 的 raw 会过内容审核，library.json 这种「一堆中文歌名」的文本文件会被
    挡下来（HTTP 451），换文件名也没用。所以这里改走 API：
        1. contents 接口把 library.json 取回来（公开仓库匿名也能读）；
        2. 万一没有 / 读不到，就用 git/trees 接口把仓库里的 midi 直接列成歌单。
    下载曲子仍然走 raw 直链 —— midi 是二进制文件，实测不会被挡。

    返回 (歌单, 出错信息)。
    """
    site, owner, repo, branch, why = backend_of(url)
    if why:
        return [], why
    api = _contents_url(site, owner, repo, INDEX_NAME, branch)
    # 读接口也要带令牌：Gitee 的 contents 匿名调会 403（实测），不带令牌这里就
    # 永远拿不到索引，只能退回本地缓存 / 列仓库，看着就像「打不开、加载很慢」。
    token = get_token(site)
    # 这里是「打开窗口就要等」的那一下，超时按索引那套来（几秒），不能按上传那套的 30 秒
    data, code, why_read = _api_raw(api, token,
                                    timeout=max(float(timeout or 0), INDEX_TIMEOUT),
                                    site=site)
    if isinstance(data, dict) and data.get('content'):
        try:
            text = base64.b64decode(data['content']).decode('utf-8', 'replace')
        except Exception:
            text = ''
        songs = parse_index(text) if text else None
        if songs:
            return songs, ''
        why_read = '索引文件看不懂（应该是一个 json：{"songs": [...]}）'
    elif code == 404:
        why_read = ''
    songs, why2 = songs_from_repo(url, timeout, site=site, token=token)
    if songs:
        return songs, ''
    return [], why_read or why2


def fetch_index(url=None, timeout=INDEX_TIMEOUT):
    """
    拉索引。返回 (歌单, 出错信息)。

    出错分两种，调用方看歌单是不是空的就知道了：网断了 -> 空歌单 + 一句话；
    拉到了 -> 歌单 + 空字符串。拉到的会顺手存一份当缓存。

    按地址认站点：Gitee 走 API（raw 会被内容审核挡），GitHub 走 raw（不带镜像）+
    contents API 兜底 —— 索引这种会变的内容不能走 jsDelivr 镜像，它是 12 小时长缓存。
    索引拉不到（仓库里还没有 library.json）或者根本不是 json 时，会自动退到
    songs_from_repo()：直接列仓库里的 midi 当歌单。所以「只丢 midi 不写索引」
    的仓库照样能用。
    """
    url = url if url is not None else source_url()
    url = str(url or '').strip()
    if not url:
        return [], '还没有设置联网曲库的地址'
    site, _owner, _repo, _branch, why = backend_of(url)
    if why:
        return [], why
    if site == SITE_GITEE:
        songs, why = songs_from_gitee(url, timeout)
        if songs:
            save_index(songs)
        return songs, why
    # GitHub 这边 raw 先试，但**不带 jsDelivr 镜像**：它对分支地址缓存 12 小时，
    # 而且查询串不进缓存键，索引从它那儿拿就是旧的。
    raw, why = _try_all(url, timeout, '索引', mirror=False)
    songs = None
    if raw is not None:
        songs = parse_index(raw.decode('utf-8', 'replace'))
    if songs:
        save_index(songs)
        return songs, ''
    # raw 连不上 / 拿回来是网页：走 contents API（带内置令牌，内容是实时的）
    songs, why_api = songs_from_gitee(url, timeout)
    if songs:
        save_index(songs)
        return songs, ''
    # 索引这条路走不通（还没有这个文件 / 不是 json / 拿回来是网页）——列仓库兜底
    fallback, why2 = songs_from_repo(url, timeout)
    if fallback:
        save_index(fallback)
        return fallback, ''
    if raw is None:
        return [], why2 or why_api or why
    return [], '索引文件看不懂（应该是一个 json：{"songs": [...]}）'


def song_url(song, base=None):
    """一首曲子的下载地址：索引地址 + 索引里写的相对路径。"""
    base = base if base is not None else source_url()
    return urllib.parse.urljoin(str(base or ''), str(song.get('file') or ''))


def local_path(song):
    """这首曲子下载之后放在本地哪个文件（保持索引里的目录结构）。"""
    rel = str(song.get('file') or '').replace('\\', '/').lstrip('/')
    parts = [p for p in rel.split('/') if p not in ('', '.', '..')]
    return os.path.join(cache_dir(), *parts) if parts else ''


def _sha1(path):
    digest = hashlib.sha1()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 16), b''):
            digest.update(block)
    return digest.hexdigest()


def is_cached(song):
    """这首曲子本地已经有了没（有的话返回路径）。"""
    path = local_path(song)
    if not path or not os.path.isfile(path):
        return ''
    size = int(song.get('size') or 0)
    if size and os.path.getsize(path) != size:
        return ''
    sha1 = str(song.get('sha1') or '')
    if sha1 and _sha1(path) != sha1:
        return ''
    return path


def installed_songs():
    """本地缓存里已经有的曲子：[(歌, 路径), ...]。"""
    songs, _when = cached_index()
    out = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        path = is_cached(song)
        if path:
            out.append((song, path))
    return out


def downloaded_root():
    """
    联网曲库下载的曲子放在哪儿（给「曲库」里那个「已下载」入口跳转用）。

    索引里的 file 多半写成 songs/xxx.mid，所以优先用 cache_dir 下的 songs\\ 子目录；
    没有的话直接用 cache_dir（用户自己往里拷曲子也算）。
    """
    base = cache_dir()
    songs = os.path.join(base, 'songs')
    return songs if os.path.isdir(songs) else base


def downloaded_files():
    """
    本地缓存目录里所有能当曲子读进来的文件：[(路径, 标题), ...]。

    不光认索引里登记过的那几首 —— 用户自己往这个文件夹里拷的也算，方便「下载下来
    的、手动放进去的」一视同仁地出现在「曲库 -> 已下载」里。
    """
    out = []
    for folder, _dirs, names in os.walk(cache_dir()):
        for name in names:
            if name.lower().endswith(MIDI_SUFFIX):
                path = os.path.join(folder, name)
                out.append((path, os.path.splitext(name)[0]))
    out.sort(key=lambda item: item[1].lower())
    return out


def download(song, base=None, timeout=SONG_TIMEOUT):
    """
    下载一首曲子到缓存里。返回 (本地路径, 出错信息)。

    本地已经有了（大小 / sha1 都对得上）就直接用，不再下一遍。
    """
    path = is_cached(song)
    if path:
        return path, ''
    path = local_path(song)
    if not path:
        return '', '这首歌在索引里没写文件名'
    url = song_url(song, base)
    if not url:
        return '', '不知道从哪儿下（索引地址是空的）'
    raw, why = _try_all(url, timeout, '这个文件', total=timeout)
    if raw is None:
        return '', why
    want = int(song.get('size') or 0)
    if want and len(raw) != want:
        return '', '下下来的大小对不上（%d 字节，应该是 %d 字节）' % (len(raw), want)
    sha1 = str(song.get('sha1') or '')
    if sha1 and hashlib.sha1(raw).hexdigest() != sha1:
        return '', '下下来的内容对不上（校验值不一样）'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(raw)
    except OSError as exc:
        return '', '存不下来：%s' % exc
    return path, ''


# ============ 上传 / 整理索引（GitHub Contents API） ============
#
# 只用标准库，不额外装东西。整块功能都是「可选」的：没令牌、没网、仓库不对，
# 都只是返回一句中文错误，不会影响听歌那一半。

def backend_of(url=None):
    """
    从索引地址里认出「哪个站、谁的、哪个仓库、哪个分支」。
    返回 (站点, owner, repo, branch, 出错信息)，站点是 'gitee' / 'github'。

    认识的写法（就是你填在「曲库地址」里的那种）：

        https://gitee.com/<owner>/<repo>/raw/<branch>/library.json      （国内曲库）
        https://gitee.com/<owner>/<repo>/blob/<branch>/library.json
        https://raw.githubusercontent.com/<owner>/<repo>/<branch>/library.json
        https://cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/library.json   （GitHub 镜像）
        https://github.com/<owner>/<repo>/blob/<branch>/library.json
    """
    url = str(url if url is not None else source_url()).strip()
    if not url:
        return '', '', '', '', '还没设置联网曲库的地址（先在曲库地址里填一个）'
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return '', '', '', '', '这个地址看不懂：%s' % url
    host = parts.netloc.lower()
    bits = [b for b in parts.path.split('/') if b]
    if host.endswith('gitee.com'):
        if len(bits) >= 6 and bits[0] == 'api' and bits[2] == 'repos':
            # /api/v5/repos/<owner>/<repo>/contents/library.json
            ref = ''
            query = urllib.parse.parse_qs(parts.query)
            if query.get('ref'):
                ref = query['ref'][0]
            return SITE_GITEE, bits[3], bits[4], (ref or 'master'), ''
        if len(bits) >= 4 and bits[2] in ('raw', 'blob', 'tree'):
            return SITE_GITEE, bits[0], bits[1], bits[3], ''
        return '', '', '', '', ('认不出这是哪个 Gitee 仓库：%s\n'
                                '（应该形如 https://gitee.com/你/仓库/raw/master/library.json）'
                                % url)
    if host.endswith('raw.githubusercontent.com') and len(bits) >= 3:
        return SITE_GITHUB, bits[0], bits[1], bits[2], ''
    if host.endswith('jsdelivr.net') and len(bits) >= 3 and bits[0] == 'gh':
        repo, _, branch = bits[2].partition('@')
        return SITE_GITHUB, bits[1], repo, branch or 'main', ''
    if host.endswith('github.com') and len(bits) >= 4 and bits[2] in ('blob', 'raw', 'tree'):
        return SITE_GITHUB, bits[0], bits[1], bits[3], ''
    return '', '', '', '', ('认不出这是哪个曲库仓库：%s\n'
                            '（Gitee 形如 https://gitee.com/你/仓库/raw/master/library.json，\n'
                            ' GitHub 形如 https://raw.githubusercontent.com/你/仓库/main/library.json）'
                            % url)


def repo_of(url=None):
    """跟 backend_of 一样，只是不要站点（老调用方用）。"""
    _site, owner, repo, branch, why = backend_of(url)
    return owner, repo, branch, why


def token_file(site=SITE_GITHUB):
    """访问令牌存在哪个文件里（只在本机）；Gitee / GitHub 各一份。"""
    name = GITEE_TOKEN_NAME if site == SITE_GITEE else TOKEN_NAME
    return os.path.join(cache_dir(), name)


def has_builtin_token(site=SITE_GITHUB):
    """内置了令牌没（没内置的话上传那套就白搭，日志里说一声）。"""
    return bool(get_token(site))


def get_token(site=SITE_GITHUB):
    """
    现在用哪个令牌：**内置的那个优先**，没有才看本机那份。

    令牌写死在 `github_token_local.py` / `gitee_token_local.py` 里，界面上不让人填 ——
    打开就能传。想换一个：改那个文件的 `TOKEN`，重新打包。
    """
    if site == SITE_GITEE:
        builtin = str(BAKED_GITEE_TOKEN or '').strip()
        placeholder = GITEE_TOKEN_PLACEHOLDER
    else:
        builtin = str(BAKED_TOKEN or '').strip()
        placeholder = TOKEN_PLACEHOLDER
    if builtin and builtin != placeholder:
        return builtin
    try:
        with open(token_file(site), 'r', encoding='utf-8') as handle:
            return handle.read().strip()
    except OSError:
        return ''


def set_token(token, site=SITE_GITHUB):
    """把备用令牌存到本机（传空字符串就是清掉）；内置那个能用时用不到它。"""
    token = str(token or '').strip()
    path = token_file(site)
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        if token:
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(token + '\n')
        elif os.path.isfile(path):
            os.remove(path)
        return True
    except OSError:
        return False


def _api_base(site):
    """这个站的 API 根地址（GitHub / Gitee 的路径长得一样，只有域名不同）。"""
    return GITEE_API if site == SITE_GITEE else GITHUB_API


def _contents_url(site, owner, repo, path, ref=''):
    """Contents API 里某个文件 / 某个目录的地址。"""
    url = '%s/repos/%s/%s/contents/%s' % (_api_base(site), owner, repo,
                                          urllib.parse.quote(str(path).strip('/')))
    if ref:
        url += '?ref=' + urllib.parse.quote(str(ref))
    return url


def _api_error(site, code, detail=''):
    """把 API 的报错翻成人话（几个常见的坑各给一句提示）。"""
    label = 'Gitee' if site == SITE_GITEE else 'GitHub'
    hints = {
        401: '令牌不对或者过期了，去 %s 重新生成一个' % label,
        403: '没权限（令牌要勾仓库读写；也可能是短时间调太多次被限流了）',
        404: '找不到这个仓库 / 分支 / 文件（私有仓库没给令牌也会 404）',
        409: '仓库里已经有一个同名文件了',
        422: '不接受这次提交（同名文件的 sha 对不上，重试一次多半就好）',
        451: '内容被平台审核挡下来了（raw 直链常见，走 API 就能绕开）',
    }
    hint = hints.get(code, '')
    return '%s 回了 %s%s%s' % (label, code, ('：%s' % detail) if detail else '',
                               ('（%s）' % hint) if hint else '')


def _api_raw(url, token='', method='GET', payload=None, timeout=API_TIMEOUT,
             site=SITE_GITHUB):
    """
    调一次曲库 API。返回 (解析出来的内容, HTTP 状态码, 出错信息)。

    GitHub / Gitee 的路径一样，区别在鉴权：GitHub 用 Authorization 头，
    Gitee 读接口用 URL 上的 access_token、写接口放在 body 里。
    连不上那种（网络层）状态码给 0，调用方看状态码就知道「是没这个文件，还是网断了」。
    """
    label = 'Gitee' if site == SITE_GITEE else 'GitHub'
    headers = {'User-Agent': USER_AGENT}
    body = None
    query_token = ''
    if payload is not None:
        payload = dict(payload)
        if site == SITE_GITEE and token:
            payload.setdefault('access_token', token)   # Gitee 写接口：令牌在 body 里
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        if site == SITE_GITEE:
            if payload is None:
                query_token = token                      # Gitee 读接口：令牌在 URL 上
        else:
            headers['Authorization'] = 'Bearer %s' % token
            headers['Accept'] = 'application/vnd.github+json'
    if query_token:
        sep = '&' if '?' in url else '?'
        url = '%s%saccess_token=%s' % (url, sep, urllib.parse.quote(query_token))
    request = urllib.request.Request(_quote_url(url), data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            got = json.loads(exc.read().decode('utf-8', 'replace') or '{}')
            detail = str(got.get('message') or got.get('error_description')
                         or got.get('error') or '')
        except Exception:
            detail = ''
        return None, exc.code, _api_error(site, exc.code, detail)
    except socket.timeout:
        return None, 0, '连不上 %s（等超时了，网线 / 代理 / 需要梯子都有可能）' % label
    except urllib.error.URLError as exc:
        return None, 0, '连不上 %s：%s' % (label, getattr(exc, 'reason', None) or exc)
    except Exception as exc:                                # pragma: no cover
        return None, 0, '出错：%s' % exc
    try:
        return json.loads(raw.decode('utf-8', 'replace') or 'null'), 200, ''
    except ValueError:
        return None, 200, '%s 回的东西看不懂（不是 json）' % label


def _api(url, token='', method='GET', payload=None, timeout=API_TIMEOUT,
         site=SITE_GITHUB):
    """跟 _api_raw 一样，只是不要状态码。"""
    data, _code, why = _api_raw(url, token, method, payload, timeout, site)
    return data, why


def remote_sha(site, owner, repo, path, token='', ref=''):
    """仓库里那个文件现在的 sha（用来覆盖提交）；没有这个文件就空字符串。"""
    data, code, why = _api_raw(_contents_url(site, owner, repo, path, ref), token,
                               site=site)
    if code == 404:
        return '', ''                      # 没有这个文件，正常
    if data is None:
        return '', why
    if isinstance(data, dict):
        return str(data.get('sha') or ''), ''
    # Gitee 对「不存在的文件」不回 404，而是回一个空列表 —— 也算「没有这个文件」
    if isinstance(data, list) and not data:
        return '', ''
    return '', '文件列表看不懂（%s 多半是个目录）' % path


def repo_files(site, owner, repo, ref, token='', timeout=API_TIMEOUT):
    """
    一次请求把仓库里所有文件列出来。返回 ([{path, size, sha}, ...], 出错信息)。

    注意这儿给的 sha 是 git 的 blob 校验值，不是文件内容的 sha1 —— 索引里那个
    sha1 得自己算（见 _sha1），所以只拿它来判断「文件变没变」。
    """
    url = '%s/repos/%s/%s/git/trees/%s?recursive=1' % (_api_base(site), owner, repo,
                                                       urllib.parse.quote(str(ref)))
    data, why = _api(url, token, timeout=timeout, site=site)
    if data is None:
        return [], why
    tree = data.get('tree') if isinstance(data, dict) else None
    if not isinstance(tree, list):
        return [], '读不懂仓库的文件列表'
    out = []
    for item in tree:
        if not isinstance(item, dict) or item.get('type') != 'blob':
            continue
        name = str(item.get('path') or '')
        if not name:
            continue
        out.append({'path': name, 'size': int(item.get('size') or 0),
                    'sha': str(item.get('sha') or '')})
    return out, ''


def list_dir(site, owner, repo, path, token='', branch='', timeout=API_TIMEOUT):
    """
    列出仓库里某个目录的文件（contents 接口）。

    返回 ([{path, size, sha, url}, ...], 出错信息)。目录不存在（Gitee 回空数组、
    GitHub 回 404）都当「空目录」，不算错 —— 音游成绩那种目录一开始就是空的。

    注意：Gitee 这个接口匿名调用会 403，必须带令牌。
    """
    url = _contents_url(site, owner, repo, path, branch)
    data, code, why = _api_raw(url, token, timeout=timeout, site=site)
    if code == 404:
        return [], ''
    if data is None:
        return [], why
    if isinstance(data, dict):                     # 给的是单个文件
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        return [], '文件列表看不懂（%s）' % path
    out = []
    for item in items:
        if not isinstance(item, dict) or item.get('type') not in (None, 'file', 'blob'):
            continue
        name = str(item.get('path') or item.get('name') or '')
        if not name:
            continue
        out.append({'path': name,
                    'size': int(item.get('size') or 0),
                    'sha': str(item.get('sha') or ''),
                    'url': str(item.get('download_url') or '')})
    return out, ''


def read_text_file(site, owner, repo, path, token='', branch='', timeout=API_TIMEOUT):
    """
    读仓库里一个文本文件（contents 接口，拿 base64 再解）。

    为什么不用 raw 直链：Gitee 的 raw 对中文文本会过内容审核（回 451）——
    音游成绩里带着用户填的中文名字，走 raw 十有八九读不回来。
    """
    url = _contents_url(site, owner, repo, path, branch)
    data, code, why = _api_raw(url, token, timeout=timeout, site=site)
    if isinstance(data, dict) and data.get('content'):
        try:
            return base64.b64decode(data['content']).decode('utf-8', 'replace'), ''
        except Exception as exc:
            return '', '内容解不开：%s' % exc
    if code == 404:
        return '', '仓库里没有 %s' % path
    if isinstance(data, list):
        return '', '%s 是个目录' % path
    return '', why or ('读不到 %s' % path)


def is_midi(path):
    """这个文件名像不像曲子。"""
    return str(path).lower().endswith(MIDI_SUFFIX)


def put_file(site, owner, repo, path, data, token, message='', branch='', sha='',
             timeout=API_TIMEOUT):
    """
    把一个文件写进仓库（有就覆盖，得先给 sha）。返回 (网页地址, 出错信息)。
    """
    payload = {
        'message': message or ('AutoPlay：更新 %s' % os.path.basename(path)),
        'content': base64.b64encode(data).decode('ascii'),
    }
    if branch:
        payload['branch'] = branch
    if sha:
        payload['sha'] = sha
    # GitHub 新建 / 覆盖都用 PUT；Gitee 新建用 POST、覆盖才用 PUT
    method = 'POST' if (site == SITE_GITEE and not sha) else 'PUT'
    got, why = _api(_contents_url(site, owner, repo, path), token, method=method,
                    payload=payload, timeout=timeout, site=site)
    if got is None:
        return '', why
    url = ''
    if isinstance(got, dict):
        url = str(((got.get('content') or {}).get('html_url')) or '')
    return url, ''


def _old_index_songs(site, owner, repo, branch, token, timeout=API_TIMEOUT):
    """把仓库里现有的 library.json 读回来（读不到就空表）：重排索引时靠它留住标题。"""
    data, _code, _why = _api_raw(_contents_url(site, owner, repo, INDEX_NAME, branch),
                                 token, timeout=timeout, site=site)
    if not isinstance(data, dict) or not data.get('content'):
        return {}
    try:
        text = base64.b64decode(data['content']).decode('utf-8', 'replace')
    except Exception:
        return {}
    out = {}
    for song in parse_index(text) or []:
        out[str(song.get('file') or '')] = song
    return out


def refresh_index(owner, repo, branch, token='', known_sha1=None, timeout=API_TIMEOUT,
                  meta=None, site=SITE_GITHUB):
    """
    按仓库里现有的文件重新生成 library.json 并提交。返回 (说明, 出错信息)。

    标题 / 艺术家尽量沿用旧索引里写的（改过标题的这次也保得住）；旧索引里查不到
    的（= 刚传上来的新曲子）用 meta 里给的，再没有才拿文件名当标题。文件大小用
    仓库给的，sha1 沿用旧的（大小没变就说明内容没换），新传的那首由 known_sha1 直接给。
    """
    token = str(token or '').strip() or get_token(site)
    if not owner or not repo:
        return '', '不知道要写进哪个仓库'
    if not token:
        return '', '程序里没有可用的%s令牌（打包时 %s 没带上？）' % (
            'Gitee' if site == SITE_GITEE else 'GitHub',
            'gitee_token_local.py' if site == SITE_GITEE else 'github_token_local.py')
    files, why = repo_files(site, owner, repo, branch, token, timeout=timeout)
    if why:
        return '', why
    old = _old_index_songs(site, owner, repo, branch, token, timeout=timeout)
    known_sha1 = dict(known_sha1 or {})
    meta = dict(meta or {})
    songs = []
    for item in files:
        path = item['path']
        if not is_midi(path) or path.lower() == INDEX_NAME.lower():
            continue
        if path not in known_sha1:
            # 仓库的「文件列表」接口带缓存：刚删掉的文件，它过一会儿还照样列出来
            # （Gitee / GitHub 都实测过）。挨个问一下「这个文件真的还在吗」，省得
            # 索引里留下一条点不动的幽灵条目 —— 谁点了都只会白等到超时。
            exists, why_check = remote_sha(site, owner, repo, path, token, branch)
            if not why_check and not exists:
                continue
        prev = old.get(path) or {}
        fresh = meta.get(path) or {}
        song = {
            'title': str(prev.get('title') or fresh.get('title')
                        or os.path.splitext(os.path.basename(path))[0]),
            'artist': str(prev.get('artist') or fresh.get('artist') or ''),
            'file': path,
            'size': int(item['size']),
            'sha1': '',
        }
        if path in known_sha1:
            song['sha1'] = str(known_sha1[path])
        elif prev and int(prev.get('size') or 0) == song['size']:
            song['sha1'] = str(prev.get('sha1') or '')
        songs.append(song)
    songs.sort(key=lambda s: s['file'])
    if not songs:
        return '', '这个仓库里一个 midi 都没有，没什么好写的'
    blob = json.dumps({'version': 1, 'songs': songs}, ensure_ascii=False,
                      indent=2).encode('utf-8')
    sha, why = remote_sha(site, owner, repo, INDEX_NAME, token, branch)
    if why:
        return '', why
    _url, why = put_file(site, owner, repo, INDEX_NAME, blob, token, branch=branch,
                         sha=sha,
                         message='AutoPlay：更新曲库索引（%d 首）' % len(songs),
                         timeout=timeout)
    if why:
        return '', why
    save_index(songs)                       # 顺便也当成本地缓存
    return '曲库索引已更新：共 %d 首' % len(songs), ''


def upload_song(local, title='', artist='', remote='', url=None, token='',
                timeout=API_TIMEOUT, site=''):
    """
    把一首 midi 传上曲库，然后重排索引。返回 (说明, 出错信息)。

    remote 不写就用本地文件名放到仓库根目录（索引里的 file 也是相对路径）。
    传上去 = 公开：这个仓库是开源共享曲库，所有人都看得到、下得走。
    """
    site2, owner, repo, branch, why = backend_of(url)
    if why:
        return '', why
    site = site or site2
    token = str(token or '').strip() or get_token(site)
    if not token:
        return '', '程序里没有可用的%s令牌（打包时 %s 没带上？）' % (
            'Gitee' if site == SITE_GITEE else 'GitHub',
            'gitee_token_local.py' if site == SITE_GITEE else 'github_token_local.py')
    local = str(local or '')
    try:
        with open(local, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        return '', '读不了这个文件：%s' % exc
    remote = str(remote or '').strip().lstrip('/') or os.path.basename(local)
    if not is_midi(remote):
        remote += '.mid'
    sha, why = remote_sha(site, owner, repo, remote, token, branch)
    if why:
        return '', why
    pages, why = put_file(site, owner, repo, remote, data, token, branch=branch, sha=sha,
                          message='AutoPlay：上传 %s' % os.path.basename(remote),
                          timeout=timeout)
    if why:
        return '', why
    told, why = refresh_index(owner, repo, branch, token,
                              known_sha1={remote: hashlib.sha1(data).hexdigest()},
                              timeout=timeout,
                              meta={remote: {'title': title, 'artist': artist}},
                              site=site)
    if why:
        return '曲子传上去了（%s），但索引没更新：%s' % (remote, why), ''
    return '已上传 %s；%s' % (remote, told), ''


def source_repo(site=None):
    """某套曲库的仓库坐标（从它的默认索引地址里认出来）。返回 (owner, repo, branch, 出错信息)。"""
    return repo_of(default_index_url(site or source_name()))


def upload_song_all(local, title='', artist='', remote='', timeout=API_TIMEOUT,
                    sites=None):
    """
    一次把一首曲子传到**所有**曲库（默认 Gitee + GitHub 都传）。返回 (说明, 出错信息)。

    两边都成功才算成功；某一边没传成，不会连累另一边（成功的已经在仓库里了），
    返回的文字里会分别写清楚两边的情况。
    """
    lines = []
    ok = 0
    tried = 0
    for site in (sites or SITE_ORDER):
        owner, repo, branch, why = source_repo(site)
        if why:
            lines.append('%s：跳过（%s）' % (SITE_LABELS.get(site, site), why))
            continue
        tried += 1
        told, bad = upload_song(local, title, artist, remote, url=default_index_url(site),
                                timeout=timeout, site=site)
        if bad:
            lines.append('%s：没传成（%s）' % (SITE_LABELS.get(site, site), bad))
        else:
            ok += 1
            lines.append('%s：%s' % (SITE_LABELS.get(site, site), told))
    if not tried:
        return '', '两套曲库都没配地址，没地方传'
    if ok:
        return '\n'.join(lines), ''
    return '', '\n'.join(lines)


def refresh_index_all(timeout=API_TIMEOUT, sites=None):
    """把每套曲库的索引都按仓库里的现有文件重排一遍。返回 (说明, 出错信息)。"""
    lines = []
    ok = 0
    for site in (sites or SITE_ORDER):
        owner, repo, branch, why = source_repo(site)
        if why:
            lines.append('%s：跳过（%s）' % (SITE_LABELS.get(site, site), why))
            continue
        told, bad = refresh_index(owner, repo, branch, timeout=timeout, site=site)
        if bad:
            lines.append('%s：没改成（%s）' % (SITE_LABELS.get(site, site), bad))
        else:
            ok += 1
            lines.append('%s：%s' % (SITE_LABELS.get(site, site), told))
    return '\n'.join(lines), ('' if ok else '一套都没排成')
