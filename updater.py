# -*- coding: utf-8 -*-
r"""
AutoPlay 增量更新器
===================

主程序（`main.py` 里点「立即更新」）把差分包下好、校验完，就把这个 exe 从安装目录
**复制到 %TEMP%\AutoPlayUpdate\** 再启动 —— 因为它待会儿要覆盖安装目录里的自己，
正跑着的 exe 锁着自己，不先搬走换不了。

它做的事（纯标准库，不带 Qt，很小）：

    等主程序退出（等它带上来的 --pid）
      -> 读差分包里的 _patch\manifest.json（哪些文件该是什么 sha256、哪些删掉）
      -> 把要被覆盖 / 删掉的文件先备份到临时目录
      -> 逐个覆盖（写一个核一个 sha256，全对才往下走）
      -> 删掉新版本里没有的文件
      -> 成了：删备份、把主程序重新拉起来
         败了：用备份原样还原、把新加的文件删掉、把主程序拉起来、弹一句说明

用法（一般由主程序自动调用，也可以手动跑）：

    AutoPlayUpdater.exe --patch <差分包.zip> --target <安装目录> --version 1.0.3 ^
        --exe AutoPlay.exe --pid 1234 --timeout 300 --relaunch

日志写在两处：`%TEMP%\AutoPlayUpdate\updater.log`（这个跑完还在，方便排查）和
`%LOCALAPPDATA%\AutoPlay\AutoPlay.log`（跟主程序一份，用户看日志就能看到更新过程）。
"""

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

MANIFEST_NAME = '_patch/manifest.json'
FORMAT_PATCH = 'autoplay-patch'
CHUNK = 1 << 20

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
DETACHED_PROCESS = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)

MB_ICONERROR = 0x10
MB_ICONINFORMATION = 0x40
MB_SETFOREGROUND = 0x10000

_SILENT = [False]                      # --silent：不弹对话框（自动测试 / 排错用）

SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF


def log_path():
    return os.path.join(tempfile.gettempdir(), 'AutoPlayUpdate', 'updater.log')


def app_log_path():
    root = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(root, 'AutoPlay', 'AutoPlay.log')


def log(text):
    """往两个日志文件里各追一行：临时目录那份跑完还在，用户那份跟主程序在一起。"""
    line = '[%s] [更新器] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text)
    for path in (log_path(), app_log_path()):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(line)
        except OSError:
            pass
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass


def message(text, flags=MB_ICONINFORMATION):
    """Updater 不带 Qt，跟用户说话只能靠系统对话框。"""
    if _SILENT[0]:
        log('（静默模式，不弹框）%s' % str(text).replace('\n', ' / '))
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, 'AutoPlay 更新', flags | MB_SETFOREGROUND)
    except Exception:
        pass


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_member(zf, name):
    """算 zip 里某个成员解出来的 sha256（不整个读进内存）。"""
    digest = hashlib.sha256()
    with zf.open(name) as source:
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def normalize(name):
    """zip 里的相对路径归一化；挡掉 .. 那种往上穿的。"""
    rel = str(name or '').replace('\\', '/').strip().lstrip('/')
    parts = [part for part in rel.split('/') if part not in ('', '.')]
    if not parts or '..' in parts:
        return ''
    return '/'.join(parts)


def safe_join(root, rel):
    parts = normalize(rel).split('/')
    return os.path.join(root, *parts)


# ---------- 等主程序退出 ----------

def wait_pid(pid, timeout):
    """等 pid 退出：退出了返回 True，等超时返回 False。进程本来就不在也算 True。"""
    pid = int(pid or 0)
    if pid <= 0:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True                     # 已经没了（或者打不开，那就直接往下走）
        try:
            ms = INFINITE if timeout <= 0 else int(timeout * 1000)
            return kernel32.WaitForSingleObject(handle, ms) == WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def stop_running(target, exe_name):
    """兜底：等不到就自己收尸（一般走不到，主程序是主动退的）。"""
    path = os.path.join(target, exe_name)
    try:
        subprocess.run(['taskkill', '/f', '/im', exe_name], creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    log('把还在跑的 %s 结束了（%s）' % (exe_name, path))


def relaunch(target, exe_name):
    """把更新好的主程序拉起来（用资源管理器起，跟用户双击一样）。"""
    path = os.path.join(target, exe_name)
    if not os.path.isfile(path):
        log('找不到 %s，没法重启' % path)
        return False
    try:
        os.startfile(path)                  # noqa: S606  就这一次，故意的
        log('已经重启：%s' % path)
        return True
    except Exception:
        pass
    try:
        subprocess.Popen([path], cwd=target, close_fds=True,
                         creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
        log('已经重启（Popen）：%s' % path)
        return True
    except OSError as error:
        log('重启失败：%s' % error)
        return False


# ---------- 差分包 ----------

def read_patch(path):
    """读差分包里的 _patch\\manifest.json。"""
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read(MANIFEST_NAME)
    except KeyError:
        return None, '差分包里没有 %s（这不像 AutoPlay 的差分包）' % MANIFEST_NAME
    except Exception as error:
        return None, '差分包打不开：%s' % error
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except ValueError as error:
        return None, '差分包里的清单看不懂：%s' % error
    if not isinstance(data, dict) or not isinstance(data.get('files'), dict):
        return None, '差分包里的清单格式不对'
    fmt = data.get('format')
    if fmt and fmt != FORMAT_PATCH:
        return None, '差分包格式不对（format=%s）' % fmt
    return data, ''


def backup(target, rels, backup_dir):
    """把将要被动到的文件先拷一份出来。返回备份好了的清单。"""
    saved = []
    for rel in rels:
        source = safe_join(target, rel)
        if not os.path.isfile(source):
            continue
        dest = os.path.join(backup_dir, *rel.split('/'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(source, dest)
        saved.append(rel)
    return saved


def restore(target, backup_dir, saved, added):
    """还原：备份过的拷回去，这次新加的文件删掉。"""
    for rel in saved:
        source = os.path.join(backup_dir, *rel.split('/'))
        dest = safe_join(target, rel)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(source, dest)
        except OSError as error:
            log('还原 %s 失败：%s' % (rel, error))
    for rel in added:
        try:
            os.remove(safe_join(target, rel))
        except OSError:
            pass


def apply_patch(zip_path, target, manifest):
    """
    覆盖 + 删除。返回 (写成功的文件, 出错信息)。

    先把整包逐文件核一遍 sha256（全对才动盘）—— 免得写了一半才发现包是坏的，
    那时候想还原也麻烦。写盘时再核一遍，最后删除新版本里没有的文件。
    """
    entries = dict(manifest.get('files') or {})
    removed = [normalize(rel) for rel in (manifest.get('removed') or [])]
    removed = [rel for rel in removed if rel]
    written = []
    with zipfile.ZipFile(zip_path) as zf:
        inside = {}
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            if info.is_dir() or name.startswith('_patch/'):
                continue
            rel = normalize(name)
            if rel:
                inside[rel] = name
        for rel, info in sorted(entries.items()):
            if rel not in inside:
                return written, '差分包里缺 %s' % rel
            want = str((info or {}).get('sha256') or '').lower()
            if want and sha256_member(zf, inside[rel]) != want:
                return written, '%s 的内容跟清单对不上（包坏了？）' % rel
        for rel, info in sorted(entries.items()):
            dest = safe_join(target, rel)
            os.makedirs(os.path.dirname(dest) or target, exist_ok=True)
            with zf.open(inside[rel]) as source, open(dest, 'wb') as out:
                while True:
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
            want = str((info or {}).get('sha256') or '').lower()
            if want and sha256_file(dest) != want:
                return written, '%s 写进去之后对不上（磁盘满了？被安全软件拦了？）' % rel
            written.append(rel)
    for rel in removed:
        dest = safe_join(target, rel)
        try:
            os.remove(dest)
            log('删掉（新版本里没有了）：%s' % rel)
        except OSError:
            pass
    return written, ''


# ---------- 主流程 ----------

def parse_args(argv):
    parser = argparse.ArgumentParser(description='AutoPlay 增量更新器')
    parser.add_argument('--patch', required=True, help='差分包 zip 的路径')
    parser.add_argument('--target', required=True, help='安装目录')
    parser.add_argument('--version', default='', help='要升到的版本号')
    parser.add_argument('--exe', default='AutoPlay.exe', help='主程序文件名')
    parser.add_argument('--pid', type=int, default=0, help='等这个进程退出')
    parser.add_argument('--timeout', type=float, default=300.0, help='最多等这么久（秒）')
    parser.add_argument('--relaunch', action='store_true', help='完事了把主程序拉起来')
    parser.add_argument('--silent', action='store_true', help='不弹对话框（测试用）')
    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    _SILENT[0] = bool(args.silent)
    target = os.path.abspath(args.target)
    log('=' * 60)
    log('开始增量更新：包=%s，安装目录=%s，目标版本=%s，主程序 pid=%d'
        % (args.patch, target, args.version or '?', args.pid))

    if not os.path.isfile(args.patch):
        log('差分包不存在，退出')
        message('差分包不见了：\n%s\n\n这次不更新了，可以回程序里重新点一次。' % args.patch,
                MB_ICONERROR)
        return 1
    if not os.path.isdir(target):
        log('安装目录不存在，退出')
        message('安装目录不见了：\n%s' % target, MB_ICONERROR)
        return 1

    if not wait_pid(args.pid, args.timeout):
        log('等主程序退出超时（%.0f 秒），先自己收尸' % args.timeout)
        stop_running(target, args.exe)
        time.sleep(1.5)

    manifest, why = read_patch(args.patch)
    if why:
        log('包读不了：%s' % why)
        message('这次更新失败：%s\n\n可以去下载页下完整安装包（装的时候会覆盖旧版）。' % why,
                MB_ICONERROR)
        if args.relaunch:
            relaunch(target, args.exe)
        return 1
    to_version = str(manifest.get('to') or args.version or '')
    log('清单好了：要换 %d 个文件，删 %d 个'
        % (len(manifest.get('files') or {}), len(manifest.get('removed') or [])))

    backup_dir = tempfile.mkdtemp(prefix='AutoPlayBackup-')
    touched = list(manifest.get('files') or {}) + list(manifest.get('removed') or [])
    saved = backup(target, [normalize(rel) for rel in touched], backup_dir)
    log('备份了 %d 个文件到 %s' % (len(saved), backup_dir))
    written, why = apply_patch(args.patch, target, manifest)
    if why:
        log('更新失败：%s —— 开始还原' % why)
        restore(target, backup_dir, saved, [rel for rel in written
                                            if rel not in saved])
        shutil.rmtree(backup_dir, ignore_errors=True)
        log('已经还原到更新前的样子')
        message('增量更新没成功，已经还原回原来的版本（没坏东西）。\n\n'
                '原因：%s\n\n'
                '可以回程序里再点一次「更新」，或者去下载页下完整安装包。' % why,
                MB_ICONERROR)
        if args.relaunch:
            relaunch(target, args.exe)
        return 1

    shutil.rmtree(backup_dir, ignore_errors=True)
    log('更新完成：换了 %d 个文件，现在是 v%s' % (len(written), to_version or '?'))
    if args.relaunch:
        if not relaunch(target, args.exe):
            message('更新好了，但自动重启没起来，麻烦手动开一下 AutoPlay。')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
