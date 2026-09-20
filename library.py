# -*- coding: utf-8 -*-
"""
联网曲库：曲子放在 GitHub 上，程序自己去拉
==========================================

没有服务器也能共享曲库：把 midi 传到一个 GitHub 仓库里，再放一个**索引文件**
`library.json`，程序读这一个网址就知道有哪些曲子、每首多大、在仓库里叫什么。

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

索引地址写在哪儿
----------------
1. `library_source.py` 里的 `INDEX_URL` —— 打包的时候定下来的默认值（见
   「设置联网曲库并重新打包.bat」，给它一个链接它就重打包）；
2. `%LOCALAPPDATA%\\AutoPlay\\library\\source.txt` —— 本地覆盖，改完重启就生效，
   不用重新打包（自己搭了个别的仓库、或者源挂了想换一个，用这个最快）。

推荐写成国内更稳的镜像地址（GitHub 的 raw 在国内经常连不上）：

    https://cdn.jsdelivr.net/gh/<用户名>/<仓库>@<分支>/library.json
    https://raw.githubusercontent.com/<用户名>/<仓库>/<分支>/library.json

没有 library.json 也能用（兜底）
-------------------------------
索引文件是「给程序看的一份歌单」，得先有人把它放进仓库。要是仓库里**只有
midi、没有索引**，程序不会干等着：它发现索引拉不到，就**干脆直接问 GitHub 这个
仓库里有哪些文件**（GitHub 的文件列表 API），把里面所有 `.mid / .midi` 当成歌单。
所以一个「只往里丢 midi」的仓库开箱就能用，不用额外维护 library.json。

（这一下是匿名请求，GitHub 对匿名调用有次数限制 —— 一小时几十次，刷新歌单够用了。
真要传曲子、改索引，还是得有令牌，见下面。）

拉不到会怎么样
--------------
**什么都不影响**：下载过的曲子都在本地缓存里，程序自带的 `songs\\` 也照常能用。
拉索引失败只是「联网曲库」那个窗口里多一行说明，别的功能一点不碰。

上传（把曲子传回仓库）
----------------------
程序也能往这个仓库里传东西 —— 用 GitHub 的 Contents API，要靠一个**访问令牌**
（Contents 读写）。这个令牌是**内置在程序里**的：见同目录的 github_token_local.py
（那个文件不进 git，只有自己这台机器和自己打出来的包里有）。
界面上没有「填令牌」这一项，打开就能传。

想让程序用别的令牌：改 github_token_local.py 里的 TOKEN，重新打包。

传一首曲子做两件事：把 midi 写进仓库，再把 library.json 按仓库里现有的文件重新
生成一遍（所以索引不会写着写着就和仓库对不上）。

曲库是公开的、大家一起用的仓库：传上去就是分享出去，所有人都能看见、能下载。

两条路，任选：

    library.upload_song(本地文件, 标题, 艺术家)
    library.refresh_index(owner, repo, branch)             # 只重排索引，不传新文件
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
# 下载一首曲子最多等这么久
SONG_TIMEOUT = 30.0
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


def _local_appdata():
    return os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()


def cache_dir():
    """下载下来的曲子放哪儿。"""
    return os.path.join(_local_appdata(), APP_FOLDER, 'library')


def source_file():
    """本地那个「索引地址」文件（优先级比打包进去的高）。"""
    return os.path.join(cache_dir(), 'source.txt')


def source_url():
    """现在用哪个索引地址：本地覆盖文件 > 打包时写进去的默认值。"""
    try:
        with open(source_file(), 'r', encoding='utf-8') as handle:
            text = handle.read().strip()
        if text:
            return text
    except OSError:
        pass
    return str(BAKED_URL or '').strip()


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


def mirrors(url):
    """
    同一个东西可以试的几个地址。

    GitHub 的 raw 在国内经常连不上，所以给 raw 地址自动补一个 jsDelivr 的镜像
    （同一个仓库、同一个文件，CDN 分发，一般能通）。列表里的顺序就是尝试顺序。
    """
    out = [url]
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


def _try_all(url, timeout, what):
    """挨个试镜像，成功就返回 (内容, 出错信息)。"""
    last = ''
    for candidate in mirrors(url):
        try:
            return _get(candidate, timeout), ''
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
        if not path:
            continue
        out.append({
            'title': str(item.get('title') or os.path.splitext(os.path.basename(path))[0]),
            'artist': str(item.get('artist') or ''),
            'file': path,
            'size': int(item.get('size') or 0),
            'sha1': str(item.get('sha1') or '').strip().lower(),
        })
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
    return (songs if isinstance(songs, list) else []), when


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


def songs_from_repo(url=None, timeout=INDEX_TIMEOUT, token=''):
    """
    兜底：不问索引文件，直接问 GitHub「这个仓库里有哪些文件」。

    用它的好处是仓库里**只有 midi 也能用**（不用先准备 library.json）。
    `file` 写相对路径，所以下面那套「索引地址 + file」拼下载地址的逻辑一个字都不用改。

    返回 (歌单, 出错信息)。匿名也能调，但有次数限制（一小时几十次）。
    """
    owner, repo, branch, why = repo_of(url)
    if why:
        return [], why
    files, why = repo_files(owner, repo, branch, token,
                            timeout=max(float(timeout or 0), API_TIMEOUT))
    if why:
        return [], why
    songs = []
    for item in files:
        path = str(item.get('path') or '')
        if not is_midi(path):
            continue
        songs.append({
            'title': os.path.splitext(os.path.basename(path))[0],
            'artist': '',
            'file': path,
            'size': int(item.get('size') or 0),
            'sha1': '',
        })
    songs.sort(key=lambda song: song['file'])
    if not songs:
        return [], '这个仓库里一个 midi 都没有（%s）' % repo
    return songs, ''


def fetch_index(url=None, timeout=INDEX_TIMEOUT):
    """
    拉索引。返回 (歌单, 出错信息)。

    出错分两种，调用方看歌单是不是空的就知道了：网断了 -> 空歌单 + 一句话；
    拉到了 -> 歌单 + 空字符串。拉到的会顺手存一份当缓存。

    索引拉不到（仓库里还没有 library.json）或者根本不是 json 时，会自动退到
    songs_from_repo()：直接列仓库里的 midi 当歌单。所以「只丢 midi 不写索引」
    的仓库照样能用。
    """
    url = url if url is not None else source_url()
    url = str(url or '').strip()
    if not url:
        return [], '还没有设置联网曲库的地址'
    raw, why = _try_all(url, timeout, '索引')
    songs = None
    if raw is not None:
        songs = parse_index(raw.decode('utf-8', 'replace'))
    if songs:
        save_index(songs)
        return songs, ''
    # 索引这条路走不通（还没有这个文件 / 不是 json / 拿回来是网页）——列仓库兜底
    fallback, why2 = songs_from_repo(url, timeout)
    if fallback:
        save_index(fallback)
        return fallback, ''
    if raw is None:
        return [], why2 or why
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
    raw, why = _try_all(url, timeout, '这个文件')
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

def repo_of(url=None):
    """
    从索引地址里认出「谁的、哪个仓库、哪个分支」。返回 (owner, repo, branch, 出错信息)。

    认识的写法（就是你填在「曲库地址」里的那种）：

        https://raw.githubusercontent.com/<owner>/<repo>/<branch>/library.json
        https://cdn.jsdelivr.net/gh/<owner>/<repo>@<branch>/library.json
        https://github.com/<owner>/<repo>/blob/<branch>/library.json
    """
    url = str(url if url is not None else source_url()).strip()
    if not url:
        return '', '', '', '还没设置联网曲库的地址（先在曲库地址里填一个）'
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return '', '', '', '这个地址看不懂：%s' % url
    host = parts.netloc.lower()
    bits = [b for b in parts.path.split('/') if b]
    if host.endswith('raw.githubusercontent.com') and len(bits) >= 3:
        return bits[0], bits[1], bits[2], ''
    if host.endswith('jsdelivr.net') and len(bits) >= 3 and bits[0] == 'gh':
        repo, _, branch = bits[2].partition('@')
        return bits[1], repo, branch or 'main', ''
    if host.endswith('github.com') and len(bits) >= 4 and bits[2] in ('blob', 'raw', 'tree'):
        return bits[0], bits[1], bits[3], ''
    return '', '', '', ('认不出这是哪个 GitHub 仓库：%s\n'
                        '（索引地址应该形如 https://raw.githubusercontent.com/你/仓库/main/library.json）' % url)


def token_file():
    """访问令牌存在哪个文件里（只在本机）。"""
    return os.path.join(cache_dir(), TOKEN_NAME)


def has_builtin_token():
    """内置了令牌没（没内置的话上传那套就白搭，日志里说一声）。"""
    return bool(get_token())


def get_token():
    """
    现在用哪个令牌：**内置的那个优先**，没有才看本机那份（github_token.txt）。

    令牌写死在 `github_token_local.py` 里，界面上不让人填 —— 打开就能传。
    想换一个：改那个文件的 `TOKEN`，重新打包。
    """
    builtin = str(BAKED_TOKEN or '').strip()
    if builtin and builtin != TOKEN_PLACEHOLDER:
        return builtin
    try:
        with open(token_file(), 'r', encoding='utf-8') as handle:
            return handle.read().strip()
    except OSError:
        return ''


def set_token(token):
    """把备用令牌存到本机（传空字符串就是清掉）；内置那个能用时用不到它。"""
    token = str(token or '').strip()
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        if token:
            with open(token_file(), 'w', encoding='utf-8') as handle:
                handle.write(token + '\n')
        elif os.path.isfile(token_file()):
            os.remove(token_file())
        return True
    except OSError:
        return False


def _contents_url(owner, repo, path, ref=''):
    """Contents API 里某个文件 / 某个目录的地址。"""
    url = '%s/repos/%s/%s/contents/%s' % (GITHUB_API, owner, repo,
                                          urllib.parse.quote(str(path).strip('/')))
    if ref:
        url += '?ref=' + urllib.parse.quote(str(ref))
    return url


def _api_error(code, detail=''):
    """把 GitHub 的报错翻成人话（几个常见的坑各给一句提示）。"""
    hints = {
        401: '令牌不对或者过期了，去 GitHub 重新生成一个',
        403: '没权限（令牌要勾 Contents 读写；也可能是短时间调太多次被限流了）',
        404: '找不到这个仓库 / 分支 / 文件（私有仓库没给令牌也会 404）',
        409: '仓库里已经有一个同名文件了',
        422: 'GitHub 不接受这次提交（同名文件的 sha 对不上，重试一次多半就好）',
    }
    hint = hints.get(code, '')
    return 'GitHub 回了 %s%s%s' % (code, ('：%s' % detail) if detail else '',
                                   ('（%s）' % hint) if hint else '')


def _api_raw(url, token='', method='GET', payload=None, timeout=API_TIMEOUT):
    """
    调一次 GitHub API。返回 (解析出来的内容, HTTP 状态码, 出错信息)。

    连不上那种（网络层）状态码给 0，调用方看状态码就知道「是没这个文件，还是网断了」。
    """
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/vnd.github+json'}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = 'Bearer %s' % token
    request = urllib.request.Request(_quote_url(url), data=body, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            got = json.loads(exc.read().decode('utf-8', 'replace') or '{}')
            detail = str(got.get('message') or '')
        except Exception:
            detail = ''
        return None, exc.code, _api_error(exc.code, detail)
    except socket.timeout:
        return None, 0, '连不上 GitHub（等超时了，网线 / 代理 / 需要梯子都有可能）'
    except urllib.error.URLError as exc:
        return None, 0, '连不上 GitHub：%s' % (getattr(exc, 'reason', None) or exc)
    except Exception as exc:                                # pragma: no cover
        return None, 0, '出错：%s' % exc
    try:
        return json.loads(raw.decode('utf-8', 'replace') or 'null'), 200, ''
    except ValueError:
        return None, 200, 'GitHub 回的东西看不懂（不是 json）'


def _api(url, token='', method='GET', payload=None, timeout=API_TIMEOUT):
    """跟 _api_raw 一样，只是不要状态码。"""
    data, _code, why = _api_raw(url, token, method, payload, timeout)
    return data, why


def remote_sha(owner, repo, path, token='', ref=''):
    """仓库里那个文件现在的 sha（用来覆盖提交）；没有这个文件就空字符串。"""
    data, code, why = _api_raw(_contents_url(owner, repo, path, ref), token)
    if code == 404:
        return '', ''                      # 没有这个文件，正常
    if data is None:
        return '', why
    if isinstance(data, dict):
        return str(data.get('sha') or ''), ''
    return '', '文件列表看不懂（%s 多半是个目录）' % path


def repo_files(owner, repo, ref, token='', timeout=API_TIMEOUT):
    """
    一次请求把仓库里所有文件列出来。返回 ([{path, size, sha}, ...], 出错信息)。

    注意这儿给的 sha 是 git 的 blob 校验值，不是文件内容的 sha1 —— 索引里那个
    sha1 得自己算（见 _sha1），所以只拿它来判断「文件变没变」。
    """
    url = '%s/repos/%s/%s/git/trees/%s?recursive=1' % (GITHUB_API, owner, repo,
                                                       urllib.parse.quote(str(ref)))
    data, why = _api(url, token, timeout=timeout)
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


def is_midi(path):
    """这个文件名像不像曲子。"""
    return str(path).lower().endswith(MIDI_SUFFIX)


def put_file(owner, repo, path, data, token, message='', branch='', sha='',
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
    got, why = _api(_contents_url(owner, repo, path), token, method='PUT',
                    payload=payload, timeout=timeout)
    if got is None:
        return '', why
    url = ''
    if isinstance(got, dict):
        url = str(((got.get('content') or {}).get('html_url')) or '')
    return url, ''


def _old_index_songs(owner, repo, branch, token, timeout=API_TIMEOUT):
    """把仓库里现有的 library.json 读回来（读不到就空表）：重排索引时靠它留住标题。"""
    data, _code, _why = _api_raw(_contents_url(owner, repo, INDEX_NAME, branch),
                                 token, timeout=timeout)
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
                  meta=None):
    """
    按仓库里现有的文件重新生成 library.json 并提交。返回 (说明, 出错信息)。

    标题 / 艺术家尽量沿用旧索引里写的（改过标题的这次也保得住）；旧索引里查不到
    的（= 刚传上来的新曲子）用 meta 里给的，再没有才拿文件名当标题。文件大小用
    仓库给的，sha1 沿用旧的（大小没变就说明内容没换），新传的那首由 known_sha1 直接给。
    """
    token = str(token or '').strip() or get_token()
    if not owner or not repo:
        return '', '不知道要写进哪个仓库'
    if not token:
        return '', '程序里没有可用的 GitHub 令牌（打包时 github_token_local.py 没带上？）'
    files, why = repo_files(owner, repo, branch, token, timeout=timeout)
    if why:
        return '', why
    old = _old_index_songs(owner, repo, branch, token, timeout=timeout)
    known_sha1 = dict(known_sha1 or {})
    meta = dict(meta or {})
    songs = []
    for item in files:
        path = item['path']
        if not is_midi(path) or path.lower() == INDEX_NAME.lower():
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
    sha, why = remote_sha(owner, repo, INDEX_NAME, token, branch)
    if why:
        return '', why
    _url, why = put_file(owner, repo, INDEX_NAME, blob, token, branch=branch, sha=sha,
                         message='AutoPlay：更新曲库索引（%d 首）' % len(songs),
                         timeout=timeout)
    if why:
        return '', why
    save_index(songs)                       # 顺便也当成本地缓存
    return '曲库索引已更新：共 %d 首' % len(songs), ''


def upload_song(local, title='', artist='', remote='', url=None, token='',
                timeout=API_TIMEOUT):
    """
    把一首 midi 传上曲库，然后重排索引。返回 (说明, 出错信息)。

    remote 不写就用本地文件名放到仓库根目录（索引里的 file 也是相对路径）。
    传上去 = 公开：这个仓库是开源共享曲库，所有人都看得到、下得走。
    """
    owner, repo, branch, why = repo_of(url)
    if why:
        return '', why
    token = str(token or '').strip() or get_token()
    if not token:
        return '', '程序里没有可用的 GitHub 令牌（打包时 github_token_local.py 没带上？）'
    local = str(local or '')
    try:
        with open(local, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        return '', '读不了这个文件：%s' % exc
    remote = str(remote or '').strip().lstrip('/') or os.path.basename(local)
    if not is_midi(remote):
        remote += '.mid'
    sha, why = remote_sha(owner, repo, remote, token, branch)
    if why:
        return '', why
    pages, why = put_file(owner, repo, remote, data, token, branch=branch, sha=sha,
                          message='AutoPlay：上传 %s' % os.path.basename(remote),
                          timeout=timeout)
    if why:
        return '', why
    told, why = refresh_index(owner, repo, branch, token,
                              known_sha1={remote: hashlib.sha1(data).hexdigest()},
                              timeout=timeout,
                              meta={remote: {'title': title, 'artist': artist}})
    if why:
        return '曲子传上去了（%s），但索引没更新：%s' % (remote, why), ''
    return '已上传 %s；%s' % (remote, told), ''
