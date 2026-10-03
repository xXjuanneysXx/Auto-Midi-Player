# -*- coding: utf-8 -*-
r"""
客户端：增量更新（比对 / 下载 / 校验 / 交给更新器）
==================================================

流程（跟 `docs\增量更新设计.md` 第六节一一对应）：

1. 拉 `update.json`（走曲库那套地址：Gitee 优先、GitHub 兜底，见 notice.py）；
2. 拿上面列的 **文件清单**（每个文件的 sha256），跟本地对一遍 —— 本地哪个文件
   内容不一样（或者缺了），就记下来；
3. 有 **从我现在这个版本升上去的差分包**，就下它（只几十 MB），下完先核 sha256；
4. 解到临时目录，启动 `AutoPlayUpdater.exe`（它会等主程序退出再去换文件），
   然后主程序自己退出 —— 更新器换完会把程序重新拉起来。

这条路走不通（比如差了太多版本、或者没有对应的差分包），就退回「下载完整安装包」，
按钮文案也会跟着变成「去下载」。

这个模块只负责「算 + 下 + 校验」，真正换文件的是 updater.py。
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

import edition
import library
import notice

UPDATER_EXE = 'AutoPlayUpdater.exe'
APP_EXE = 'AutoPlay.exe'
PATCH_DIR_NAME = 'update'          # 下载的差分包放在 %LOCALAPPDATA%\AutoPlay\update\
DOWNLOAD_TIMEOUT = 30.0            # 单个请求最多等这么久（下载中是「每读一块」的超时）
TOTAL_TIMEOUT = 900.0              # 一整个包最多下这么久（15 分钟，正常几十秒就该完事）
CHUNK = 1 << 20


def edition_key():
    """这一版是 'full' 还是 'lite'（对应 update.json 里的 editions 键）。"""
    return 'lite' if edition.LITE else 'full'


def edition_label():
    return edition.EDITION


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def patch_dir():
    return os.path.join(library.cache_dir(), PATCH_DIR_NAME)


# ---------- 清单 ----------

def manifest(timeout=notice.DEFAULT_TIMEOUT):
    """拉 update.json（拿不到给 ({}, 原因)）。"""
    return notice.update_info(timeout=timeout)


def edition_entry(data, key=None):
    """update.json -> 我这一版的条目。"""
    editions = (data or {}).get('editions') or {}
    got = editions.get(key or edition_key()) or {}
    return got if isinstance(got, dict) else {}


def listed_files(entry):
    """条目里的 files 数组 -> {相对路径: sha256}。"""
    out = {}
    for item in (entry.get('files') or []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get('path') or '').replace('\\', '/').lstrip('/')
        if rel:
            out[rel] = str(item.get('sha256') or '')
    return out


def local_differs(install_dir, files):
    """本地跟清单对不上的文件（内容不一样 / 缺文件）—— 更新时要换的就是这些。"""
    out = []
    for rel, want in sorted(files.items()):
        path = os.path.join(install_dir, *rel.split('/'))
        if not os.path.isfile(path):
            out.append(rel)
        elif want and sha256_file(path) != want:
            out.append(rel)
    return out


def patch_for(data, current, key=None):
    """有没有「从 current 这一版直接升上去」的差分包。"""
    for item in (edition_entry(data, key).get('patches') or []):
        if isinstance(item, dict) and str(item.get('from') or '') == str(current):
            return item
    return None


def plan(current, install_dir, data=None):
    """
    我该走哪条路。返回的 dict 里 mode 是：

        none   已经是最新（或者文件本来就一样，不用换）
        patch  有差分包，可以增量更新
        full   没有差分包，只能下完整安装包
        error  拉不到清单 / 清单不完整（why 里是原因）

    data 已经由调用方拉好了就直接传进来（省一次网络请求，也免得前后两次拿到
    的清单不是同一份）。
    """
    if data:
        data, why = dict(data), ''
    else:
        data, why = manifest()
    if not data:
        return {'mode': 'error', 'why': why or '拉不到更新清单'}
    latest = str(data.get('latest') or '')
    entry = edition_entry(data)
    files = listed_files(entry)
    if not files:
        return {'mode': 'error', 'why': '更新清单里这一版没有文件列表', 'latest': latest,
                'data': data}
    if not notice.is_newer(latest, current):
        return {'mode': 'none', 'latest': latest, 'data': data, 'files': files}
    if not local_differs(install_dir, files):
        return {'mode': 'none', 'latest': latest, 'data': data, 'files': files,
                'why': '程序文件已经跟最新版一模一样'}
    patch = patch_for(data, current)
    if patch and patch.get('url'):
        return {'mode': 'patch', 'latest': latest, 'patch': patch, 'data': data,
                'files': files}
    return {'mode': 'full', 'latest': latest, 'data': data, 'files': files,
            'package': entry.get('package') or {}}


# ---------- 下载 / 解包 ----------

def download(url, dest, progress=None, timeout=DOWNLOAD_TIMEOUT,
             expect_size=0, expect_sha='', total_timeout=TOTAL_TIMEOUT):
    """
    下差分包：流式写盘、回调进度、下完核 sha256。
    返回 (本地路径, 出错信息)；出错时路径是空的。

    两道闸：`timeout` 管「一块数据最多等多久」（卡住的连接会在这儿断），
    `total_timeout` 管「一整个包最多下多久」—— 免得网速慢到离谱时界面一直挂着进度。
    """
    import urllib.request
    import time as _time
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    request = urllib.request.Request(library._quote_url(url),
                                     headers={'User-Agent': library.USER_AGENT})
    got = 0
    started = _time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get('Content-Length') or 0) or int(expect_size or 0)
            with open(dest, 'wb') as out:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    if total_timeout and _time.monotonic() - started > total_timeout:
                        return '', ('下了太久还没下完（超过 %.0f 分钟），先放弃这次更新；'
                                    '可以过会儿再点一次，或者去下载页拿完整安装包。'
                                    % (total_timeout / 60.0))
                    out.write(chunk)
                    got += len(chunk)
                    if progress is not None:
                        try:
                            progress(got, total)
                        except Exception:
                            pass
    except Exception as error:                     # 网络层的错都在这里
        return '', '下载失败：%s' % (getattr(error, 'reason', None) or error)
    if expect_size and got != int(expect_size):
        return '', '下下来的大小不对（%d 字节，清单里写的是 %d）' % (got, int(expect_size))
    if expect_sha and sha256_file(dest) != str(expect_sha).lower():
        try:
            os.remove(dest)
        except OSError:
            pass
        return '', '下下来的差分包 sha256 对不上（文件坏了或者被人换过），这次不更新'
    return dest, ''


def extract(zip_path, dest_dir):
    """把差分包解到临时目录（挡掉 .. 那种穿越路径）。返回 (解出来几个文件, 出错信息)。"""
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename.replace('\\', '/')
                if info.is_dir():
                    continue
                parts = [part for part in name.split('/') if part not in ('', '.')]
                if not parts or '..' in parts:
                    continue
                target = os.path.join(dest_dir, *parts)
                folder = os.path.dirname(target)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                with zf.open(info) as source, open(target, 'wb') as out:
                    while True:
                        chunk = source.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                count += 1
    except Exception as error:
        return 0, '差分包解不开：%s' % error
    if not count:
        return 0, '差分包里什么都没有'
    return count, ''


# ---------- 启动更新器 ----------

def launch_updater(zip_path, install_dir, version, pid=None, relaunch=True, timeout=180):
    """
    启动 AutoPlayUpdater.exe：它先等主程序退出，再解压覆盖、失败回滚、重启。

    注意更新器是**复制到临时目录再跑**的 —— 它待会儿要覆盖安装目录里的自己，
    正跑着的 exe 锁着自己，不先搬走就换不了。
    """
    source = os.path.join(install_dir, UPDATER_EXE)
    if not os.path.isfile(source):
        return '', ('安装目录里没有 %s —— 老版本没有带更新器，请下载完整安装包。'
                    % UPDATER_EXE)
    try:
        work = os.path.join(tempfile.gettempdir(), 'AutoPlayUpdate')
        os.makedirs(work, exist_ok=True)
        runner = os.path.join(work, UPDATER_EXE)
        shutil.copy2(source, runner)
    except Exception as error:
        return '', '更新器搬不出来：%s' % error
    args = [runner, '--patch', zip_path, '--target', install_dir, '--version', str(version),
            '--exe', APP_EXE, '--pid', str(int(pid or 0)), '--timeout', str(int(timeout))]
    if relaunch:
        args.append('--relaunch')
    try:
        flags = 0x00000008 | 0x00000200        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, close_fds=True, creationflags=flags)
    except Exception as error:
        return '', '更新器起不来：%s' % error
    return runner, ''
