# -*- coding: utf-8 -*-
r"""
上传中转：让装了你软件的人也能往公共曲库传曲子，而安装包里不带任何令牌
=====================================================================

为什么要有它
------------
往 GitHub 仓库里写文件需要令牌（PAT）。令牌一旦打进安装包就等于公开：
解开 exe 就能拿到，拿它删你曲库里的文件都行；GitHub 还会主动扫公开仓库里的
PAT 并自动吊销。所以「内置令牌」这条路走不远。

中转的做法
----------

    你的程序  --POST 曲子-->  中转（Worker，令牌存在服务器上）  --PUT-->  GitHub

* 令牌只在 Cloudflare 那边，安装包里一行都没有；
* 用户还是**什么都不用填**，打开就能传（体验和内置令牌一模一样）；
* 要换令牌、要止损，改服务器上的环境变量就行 —— 不用重新打包、不用惊动用户；
* 顺手还能做校验（只收 midi、限大小、限频率），防止有人拿它当免费网盘。

怎么搭
------
见 `中转上传\部署说明.md`（十分钟，免费）。搭完拿到地址，形如
`https://autoplay-library.你的账号.workers.dev`，然后：

    设置上传中转并重新打包.bat            （双击，按提示粘贴地址）
    python -X utf8 set_relay_url.py <地址>                   等价
    python -X utf8 set_relay_url.py <地址> --strip-token     顺便清掉内置令牌
    python -X utf8 set_relay_url.py --clear                  清掉地址

不想重新打包、只想本机先试试：往 `%LOCALAPPDATA%\AutoPlay\library\relay.txt`
写一行地址，重启程序就生效。

没配地址的时候这套逻辑完全不参与：程序退回去用内置令牌（如果有），
什么都没有就照旧提示一句，本地曲库、演奏、编辑器都不受影响。
"""

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

import library

USER_AGENT = library.USER_AGENT
# 传一首曲子要写两个文件（曲子 + 索引），服务器那边也可能在排队，超时放宽点
UPLOAD_TIMEOUT = 120.0
# 只是问一句「你活着没」：这个得快
PING_TIMEOUT = 15.0
# 中转地址存在哪（本机覆盖用，优先级比打包进去的高）
RELAY_NAME = 'relay.txt'
# 口令的本机覆盖文件（打包进去的那份在 relay_key_local.py）
KEY_NAME = 'relay_key.txt'

# 打包时写进来的默认地址（relay_source.py 由 set_relay_url.py 生成）
try:
    from relay_source import RELAY_URL as BAKED_URL
except Exception:                                   # pragma: no cover
    BAKED_URL = ''

# 上传口令：它是个密密，不能跟着源码进 git，
# 所以和令牌一个待遇放在 relay_key_local.py（见 .gitignore）。
try:
    from relay_key_local import RELAY_KEY as LOCAL_KEY
except Exception:                                   # pragma: no cover
    LOCAL_KEY = ''

# 早期版本把口令写在 relay_source.py，这儿继续认（兼容）
try:
    from relay_source import RELAY_KEY as BAKED_KEY
except Exception:                                   # pragma: no cover
    BAKED_KEY = ''


def local_file():
    """本机那个「中转地址」文件（优先级比打包进去的高）。"""
    return os.path.join(library.cache_dir(), RELAY_NAME)


def key_file():
    """本机那个「上传口令」文件。"""
    return os.path.join(library.cache_dir(), KEY_NAME)


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read().strip()
    except OSError:
        return ''


def _write(path, text):
    try:
        os.makedirs(library.cache_dir(), exist_ok=True)
        if text:
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write(text + '\n')
        elif os.path.isfile(path):
            os.remove(path)
        return True
    except OSError:
        return False


def normalize(url):
    """把用户粘进来的地址收拾干净；不像个 http 地址就返回空串。"""
    url = str(url or '').strip().strip('"').strip("'")
    if not url:
        return ''
    if not url.lower().startswith(('http://', 'https://')):
        url = 'https://' + url
    while url.endswith('/'):
        url = url[:-1]
    return url


def baked_url():
    """打包时写进程序里的那个地址（没有就空串）。"""
    return normalize(BAKED_URL)


def get_url():
    """现在用哪个中转地址：本地覆盖文件 > 打包时写进去的默认值。"""
    local = normalize(_read(local_file()))
    return local or baked_url()


def set_url(url):
    """把中转地址写到本地覆盖文件里（不用重新打包）。"""
    return _write(local_file(), normalize(url))


def get_key():
    """
    上传口令（可选的第二道门，挡住随手知道地址就往里塞东西的人）。

    优先级：本机 relay_key.txt > relay_key_local.py（打包带进去的）> relay_source.RELAY_KEY（老写法）。
    """
    return (_read(key_file()) or str(LOCAL_KEY or '').strip()
            or str(BAKED_KEY or '').strip())


def set_key(key):
    return _write(key_file(), str(key or '').strip())


def has_url():
    """配没配中转。配了的话，上传就走它，程序里的令牌根本不碰。"""
    return bool(get_url())


def describe():
    """给界面看的一行说明。"""
    url = get_url()
    if not url:
        return '没配上传中转'
    host = urllib.parse.urlsplit(url).netloc or url
    return '上传中转：%s（令牌在服务器上，安装包里没有）' % host


# ----------------------------------------------------------------- 发请求

def _headers(ctype='', key=''):
    head = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if ctype:
        head['Content-Type'] = ctype
    key = str(key or '').strip() or get_key()
    if key:
        head['X-AutoPlay-Key'] = key
    return head


def _why(exc):
    """把各种网络异常翻成人话。"""
    if isinstance(exc, socket.timeout):
        return '连不上中转（等超时了，网线 / 代理 / 梯子都有可能）'
    if isinstance(exc, urllib.error.HTTPError):
        return '中转回了 %s' % exc.code
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, 'reason', None)
        if isinstance(reason, socket.timeout):
            return '连不上中转（等超时了，网线 / 代理 / 梯子都有可能）'
        return '连不上中转：%s' % (reason or exc)
    return '出错：%s' % exc


def _send(path, data=None, ctype='', timeout=UPLOAD_TIMEOUT, method=None,
          url='', key=''):
    """
    往中转发一个请求，返回 (解析好的 json 或 None, 出错信息)。

    中转回的 json 统一长这样：{"ok": true/false, "told": "...", "bad": "..."}。
    url / key 不给就用当前配好的那一套（配置脚本会把新地址直接传进来，
    省得为了问一句就先动本机配置）。
    """
    base = normalize(url) or get_url()
    if not base:
        return None, '还没配上传中转的地址（见 中转上传\\部署说明.md）'
    full = library._quote_url(base + path)
    request = urllib.request.Request(full, data=data, headers=_headers(ctype, key),
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            payload = json.loads(exc.read().decode('utf-8', 'replace'))
            detail = str(payload.get('bad') or payload.get('error') or '')
        except Exception:
            detail = ''
        if exc.code == 404:
            # 中转自己会给一句更像样的话（比如「能用的只有 /health、/upload、/reindex」），
            # 有就用它的；没有才说我们自己的猜测。
            return None, detail or '中转说没有这个接口（404）—— 地址是不是写错了？'
        return None, detail or _why(exc)
    except Exception as exc:
        return None, _why(exc)
    try:
        info = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return None, '中转回的东西看不懂（不是 json）—— 地址填的是中转首页吗？'
    if not isinstance(info, dict):
        return None, '中转回的东西看不懂'
    bad = str(info.get('bad') or '')
    if bad:
        return info, bad
    if info.get('ok') is False:
        return info, bad or '中转说没成'
    return info, ''


def ping(timeout=PING_TIMEOUT, url='', key=''):
    """问一句中转活着没。返回 (信息 dict 或 None, 出错信息)。"""
    return _send('/health', timeout=timeout, method='GET', url=url, key=key)


# ----------------------------------------------------------------- 上传

def _multipart(fields, filename, blob):
    """拼一个 multipart/form-data 请求体（标准库没有现成的，自己来）。"""
    boundary = '----AutoPlay' + uuid.uuid4().hex
    out = []
    for name, value in fields:
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                    % (boundary, name, value)).encode('utf-8'))
    out.append(('--%s\r\nContent-Disposition: form-data; name="file"; '
                'filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
                % (boundary, filename)).encode('utf-8'))
    out.append(blob)
    out.append(b'\r\n')
    out.append(('--%s--\r\n' % boundary).encode('utf-8'))
    return b''.join(out), 'multipart/form-data; boundary=%s' % boundary


def upload(local, title='', artist='', owner='', repo='', branch='',
           timeout=UPLOAD_TIMEOUT):
    """
    把一首 midi 交给中转，让它替你写进曲库仓库。返回 (说明, 出错信息)。

    和 `library.upload_song` 的返回值一样，所以界面那边可以当同一个东西用。
    文件名走单独一个字段，文件本身用纯 ascii 的假名 —— 中文文件名的编码
    问题（RFC 2231）就让服务器自己去头疼，别在客户端碰。
    """
    local = str(local or '')
    try:
        with open(local, 'rb') as handle:
            blob = handle.read()
    except OSError as exc:
        return '', '读不了这个文件：%s' % exc
    if not blob:
        return '', '这是个空文件'
    name = os.path.basename(local)
    fields = [('title', title or ''), ('artist', artist or ''), ('name', name),
              ('owner', owner or ''), ('repo', repo or ''), ('branch', branch or '')]
    body, ctype = _multipart(fields, 'song.mid', blob)
    info, why = _send('/upload', data=body, ctype=ctype, timeout=timeout)
    if info is None:
        return '', why
    return str(info.get('told') or ''), str(info.get('bad') or '')


def reindex(owner='', repo='', branch='', timeout=UPLOAD_TIMEOUT):
    """让中转按仓库里现有的文件重排一遍索引。返回 (说明, 出错信息)。"""
    payload = json.dumps({'owner': owner or '', 'repo': repo or '',
                          'branch': branch or ''}).encode('utf-8')
    info, why = _send('/reindex', data=payload, ctype='application/json',
                      timeout=timeout)
    if info is None:
        return '', why
    return str(info.get('told') or ''), str(info.get('bad') or '')