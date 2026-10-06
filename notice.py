# -*- coding: utf-8 -*-
r"""
公告 / 版本信息（联网拉取）
==========================

跟曲库放同一个仓库、同一套地址推导：仓库根目录下

    version.json   版本信息（最新版本号、更新说明、几个链接）
    notice.json    公告（一个数组，可以放多条）
    update.json    增量更新配置（**现在只预埋，这个模块还不读它**）

为什么不用 raw 直链：Gitee 的 raw 会对中文文本过内容审核（回 451），所以国内这套
跟 library.json 一样走 contents API（拿 base64 再解）；GitHub 那边先试 raw，
不行再走 API。两套曲库会互相兜底 —— 国内那套拉不到就试 GitHub 那套。

这个模块只负责「把 json 拿回来并解出来」，不含任何界面代码，也不联网重试太狠：
超时都是几秒，拉不到就让调用方安静地忽略（公告/版本拉不到不影响任何功能）。
"""

import base64
import json
import os
import time

import library

VERSION_NAME = 'version.json'
NOTICE_NAME = 'notice.json'
UPDATE_NAME = 'update.json'

FORMAT_VERSION = 'autoplay-version'
FORMAT_NOTICE = 'autoplay-notice'
FORMAT_UPDATE = 'autoplay-update'

DEFAULT_TIMEOUT = 5.0        # 单个地址最多等这么久（公告是小文件，几秒足够）
ATTEMPTS = 3                 # 一轮拉不到再试一轮 —— Gitee 偶尔会抽一下 SSL
TOTAL_BUDGET = 9.0           # 一个文件从头试到尾最多花这么久（界面别等太久）

CACHE_VERSION = 'version.cache.json'     # 上次拉到的版本信息（网络抽风时兜底）
CACHE_NOTICE = 'notice.cache.json'       # 上次拉到的公告
CACHE_TEXT = 'text.cache.%s'            # 拉到的纯文本（快速上手手册）先缓存一份

_SITE_SHORT = {library.SITE_GITEE: 'Gitee', library.SITE_GITHUB: 'GitHub'}


# ---------- 地址推导 ----------

def _sites():
    """先试「当前这套曲库」，再拿另一套兜底。返回 [(site, owner, repo, branch), ...]。"""
    order = [library.source_name()]
    for key in library.SITE_ORDER:
        if key not in order:
            order.append(key)
    out = []
    for key in order:
        url = library.source_url() if key == library.source_name() \
            else library.default_index_url(key)
        site, owner, repo, branch, why = library.backend_of(url)
        if why or not owner or not repo:
            continue
        out.append((site, owner, repo, branch or ('master' if site == library.SITE_GITEE
                                                  else 'main')))
    return out


def _raw_url(site, owner, repo, branch, name):
    if site == library.SITE_GITHUB:
        return 'https://raw.githubusercontent.com/%s/%s/%s/%s' % (owner, repo, branch, name)
    return 'https://gitee.com/%s/%s/raw/%s/%s' % (owner, repo, branch, name)


def _no_cache(url):
    """
    给地址加一个每次都变的查询参数，躲开 CDN 缓存。

    这几个 json 走的是 raw 直链 / contents 接口，两边都是 CDN，缓存 key 就是整个 URL。
    文件被覆盖之后，裸链接可能还会吐旧内容 —— 实测抓到过 40 分钟前的版本信息，
    更新时就成了「清单里写的大小跟下下来的包对不上」。加个时间戳，每次都是新 URL，
    CDN 只能回源（Gitee 读接口的 access_token 是后面拼上去的，不受影响）。
    """
    if not url:
        return url
    sep = '&' if '?' in url else '?'
    return '%s%s_=%d' % (url, sep, time.time())


def _from_api(site, owner, repo, branch, name, timeout):
    """contents 接口：拿回来的是 base64 的 content。

    Gitee 这个接口**匿名调会 403**（本程序实测），于是就走不成「国内曲库」，
    只能退到 GitHub raw —— 而 raw 在国内常连不上、还有几分钟的 CDN 缓存，
    新公告 / 新版本就会迟迟刷不出来。所以这里带上程序里内置的那个令牌
    （跟曲库读写用的是同一个，见 library.get_token）；令牌不行再退回匿名试一次。
    """
    url = _no_cache(library._contents_url(site, owner, repo, name, branch))
    token = ''
    try:
        token = str(library.get_token(site) or '')
    except Exception:
        token = ''
    data, code, why = library._api_raw(url, token, timeout=timeout, site=site)
    if token and not (isinstance(data, dict) and data.get('content')):
        data, code, why = library._api_raw(url, '', timeout=timeout, site=site)
    if isinstance(data, dict) and data.get('content'):
        try:
            text = base64.b64decode(data['content']).decode('utf-8', 'replace')
        except Exception:
            return None, '内容解不开（base64 坏了？）'
        return text, ''
    if code == 404:
        return None, '仓库里还没有这个文件（%s）' % name
    return None, why or '拉不到（%s）' % name


def _fetch_once(site, owner, repo, branch, name, timeout):
    """从某一套曲库里拉一个文件，返回 (文本, 出错信息)。"""
    why = ''
    raw = _no_cache(_raw_url(site, owner, repo, branch, name))
    if site == library.SITE_GITHUB:
        # GitHub 先走 raw（快、不限流）。这里**故意不带 jsDelivr 镜像**：
        # 它对分支地址是 12 小时长缓存，查询串又不进缓存键（_no_cache 的时间戳
        # 对它无效），版本信息从它那儿拿就是旧的 —— 界面会一直说「已是最新」。
        try:
            text, why = library._try_all(raw, timeout, name, total=timeout, mirror=False)
            if text:
                return text, ''
        except Exception as error:
            why = str(error)
    # Gitee 的 raw 会被审核挡掉，直接走 contents API（拿 base64 再解）；
    # GitHub 这边 raw 连不上时也走它 —— 带内置令牌，内容是实时的
    text, why_api = _from_api(site, owner, repo, branch, name, timeout)
    if text:
        return text, ''
    # 两条路都不通才轮到 jsDelivr 镜像兜底（可能旧，但总比什么都没有强）
    for mirror in library.mirror_urls(raw):
        try:
            text, why_mirror = library._try_all(mirror, timeout, name, total=timeout)
        except Exception as error:
            why_mirror = str(error)
        if text:
            return text, ''
    return '', why_api or why


def _fetch_text(name, timeout=DEFAULT_TIMEOUT):
    """
    在几套曲库之间挨个试，返回 (文本, 出错信息)。

    出错信息把每套曲库的原因都写出来（正在用的那套排最前，也就是「国内曲库」）——
    只报最后一套的话会出现「明明是 Gitee 连不上，却告诉你 GitHub 里没这个文件」。
    整趟有总预算 TOTAL_BUDGET，试到点就收，不让界面干等。
    """
    deadline = time.monotonic() + TOTAL_BUDGET
    last = {}
    for _attempt in range(ATTEMPTS):
        for site, owner, repo, branch in _sites():
            left = deadline - time.monotonic()
            if left <= 0.8:
                break
            share = max(1.5, min(float(timeout), left))
            text, why = _fetch_once(site, owner, repo, branch, name, share)
            if text:
                return text, ''
            last[site] = why or ('拉不到 %s' % name)
        if time.monotonic() >= deadline - 0.8:
            break
    parts = ['%s：%s' % (_SITE_SHORT.get(site, site), why) for site, why in last.items()]
    return '', '；'.join(parts) or ('拉不到 %s' % name)


def _fetch_json(name, timeout=DEFAULT_TIMEOUT):
    """拉一个 json 文件；_fetch_text 里已经带重试了，这里只管解析。"""
    text, why = _fetch_text(name, timeout=timeout)
    if not text:
        return None, why
    try:
        data = json.loads(text)
    except ValueError as error:
        return None, 'json 看不懂：%s' % error
    if isinstance(data, dict):
        return data, ''
    return None, 'json 不是一个对象'


# ---------- 对外 ----------

def version_info(timeout=DEFAULT_TIMEOUT):
    """
    版本信息（拿不到就返回 ({}, 原因)）。

    联网失败但本地存过上次的，就把上次那份拿出来用（why 里带着失败原因，界面上
    会写一句「这次没连上，下面是上次拉到的」）—— 免得网一抽，更新页就只剩报错。
    """
    data, why = _fetch_json(VERSION_NAME, timeout=timeout)
    if data is None:
        cached = _read_cache(CACHE_VERSION)
        if cached:
            cached['_cached'] = True
            return cached, why
        return {}, why
    if data.get('format') and data.get('format') != FORMAT_VERSION:
        return {}, '这不像版本信息文件（format=%s）' % data.get('format')
    _write_cache(CACHE_VERSION, data)
    return data, ''


def notice_info(timeout=DEFAULT_TIMEOUT):
    """公告（拿不到就返回 ([], 原因)；联网失败时返回上次拉到的那份）。"""
    data, why = _fetch_json(NOTICE_NAME, timeout=timeout)
    if data is None:
        cached = _read_cache(CACHE_NOTICE)
        if cached:
            return _notices_of(cached), why
        return [], why
    _write_cache(CACHE_NOTICE, data)
    return _notices_of(data), ''


def update_file_name(version=None):
    """
    增量更新清单叫什么名字。默认 update.json；version.json 里写 update_manifest
    就能改（以后想把清单挪个位置不用改程序）。
    """
    data = version if isinstance(version, dict) else {}
    name = str(data.get('update_manifest') or '').strip()
    return name or UPDATE_NAME


def update_info(timeout=DEFAULT_TIMEOUT, version=None):
    """
    增量更新清单（update.json）。

    跟版本信息 / 公告不一样，这个文件**故意不缓存**：它是一条「更新指令」，
    拿一份旧的会算错「我该下哪个包」，甚至去下一个已经下架的差分包。
    拉不到就老实报错，让调用方退回「去下载完整安装包」那条路。
    """
    data, why = _fetch_json(update_file_name(version), timeout=timeout)
    if data is None:
        return {}, why
    fmt = data.get('format')
    if fmt and fmt != FORMAT_UPDATE:
        return {}, '这不像更新清单（format=%s）' % fmt
    return data, ''


def _safe_name(name):
    """把仓库里的路径压成一个能当文件名的东西（快速上手/手册.md -> 手册.md）。"""
    base = str(name or '').replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
    keep = ''.join(ch if (ch.isalnum() or ch in '._-') else '_' for ch in base)
    return keep or 'text'


def text_file(name, timeout=DEFAULT_TIMEOUT):
    """
    拉一个纯文本文件（快速上手手册是 md），返回 (文本, 出错信息)。

    拉到了就顺手缓存一份到本机：下次网络抽风 / 离线时，界面上还能看到上次那份，
    why 里带着这次失败的原因（界面自己决定要不要提一句）。
    """
    text, why = _fetch_text(name, timeout=timeout)
    if text:
        _write_text_cache(name, text)
        return text, ''
    cached = _read_text_cache(name)
    if cached:
        return cached, why
    return '', why


def _text_cache_path(name):
    return os.path.join(library.cache_dir(), CACHE_TEXT % _safe_name(name))


def _read_text_cache(name):
    try:
        with open(_text_cache_path(name), 'r', encoding='utf-8') as handle:
            return handle.read()
    except Exception:
        return ''


def _write_text_cache(name, text):
    try:
        os.makedirs(library.cache_dir(), exist_ok=True)
        with open(_text_cache_path(name), 'w', encoding='utf-8') as handle:
            handle.write(text)
    except OSError:
        pass


def _notices_of(data):
    """从 notice.json 里把公告数组理出来。"""
    raw = data.get('notices')
    if isinstance(raw, dict):
        raw = [raw]
    return [item for item in (raw or []) if isinstance(item, dict)]


# ---------- 本地缓存（网络抽风时的兜底） ----------

def _cache_path(name):
    return os.path.join(library.cache_dir(), name)


def _read_cache(name):
    try:
        with open(_cache_path(name), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_cache(name, data):
    try:
        os.makedirs(library.cache_dir(), exist_ok=True)
        with open(_cache_path(name), 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    except OSError:
        pass


def compare_versions(left, right):
    """比版本号：left 比 right 新返回 1，一样返回 0，旧返回 -1。"""

    def parts(text):
        out = []
        for chunk in str(text or '').strip().lstrip('vV').split('.'):
            digits = ''.join(c for c in chunk if c.isdigit())
            out.append(int(digits) if digits else 0)
        return out

    a, b = parts(left), parts(right)
    length = max(len(a), len(b))
    a += [0] * (length - len(a))
    b += [0] * (length - len(b))
    return (a > b) - (a < b)


def is_newer(latest, current):
    """latest 比 current 新？"""
    return compare_versions(latest, current) > 0


def links(info=None):
    """版本信息里的链接（缺什么就用内置的默认值兜底）。"""
    data = dict(info or {})
    raw = data.get('links') if isinstance(data.get('links'), dict) else {}
    out = dict(DEFAULT_LINKS)
    for key, value in raw.items():
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


# 内置兜底：万一 version.json 拉不到，这几个按钮也还能用
# 「去下载」现在跳的是 B 站主页（下载链接发在那儿）；想换回发行版页就改这里，
# 或者改曲库仓库 version.json 的 links.download_page（远端那份优先）。
DEFAULT_LINKS = {
    'bilibili': 'https://space.bilibili.com/341688158',
    'github': 'https://github.com/xXjuanneysXx/Auto-Midi-Player',
    'download_page': 'https://space.bilibili.com/341688158',
    'library': 'https://gitee.com/juanneys/midi-music',
}
