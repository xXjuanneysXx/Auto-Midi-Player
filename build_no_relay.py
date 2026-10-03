# -*- coding: utf-8 -*-
r"""
打一版「不带上传中转」的安装包
==============================

为什么要有它：中转（Cloudflare Worker）在有些网络下连不上，那就退回老路子最稳 ——
令牌内置在程序里，程序直接调 GitHub API 传曲子（`library.upload_song`）。

这个脚本按顺序做四件事：

1. 把 `relay_source.py` / `relay_key_local.py` 原样备份到「中转配置备份」文件夹
   （只在里面真有地址 / 口令时才备，免得把好配置覆盖成空的）；
2. 把这两个文件里的 `RELAY_URL` / `RELAY_KEY` 清空 —— 打包带进程序的就是空的，
   程序启动后 `relay.has_url()` 为假，上传自动走内置令牌那条老路；
3. 跑 `make_installer.py all`，两个安装包进「发布」文件夹；
4. 把备份放回去（加了 `--permanent` 就不放回去）。

用法（项目目录下）：

    python -X utf8 build_no_relay.py                # 打一版不带中转的，打完把配置还回来
    python -X utf8 build_no_relay.py --permanent    # 顺便永久清掉中转配置（以后默认就不带）
    python -X utf8 build_no_relay.py --restore      # 只把中转配置从备份里恢复回来，不打包
    python -X utf8 build_no_relay.py --no-build     # 清掉配置但不打包（调试用）

「发布中转版」那个文件夹是手动备份，本脚本一概不碰。
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import set_library_url                                       # noqa: E402  （借它的「重新打包」）

SOURCE_PY = os.path.join(HERE, 'relay_source.py')
KEY_PY = os.path.join(HERE, 'relay_key_local.py')
BACKUP_DIR = os.path.join(HERE, '中转配置备份')
FILES = (SOURCE_PY, KEY_PY)


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return handle.read()
    except OSError:
        return ''


def _set(path, name, value):
    """把文件里 `NAME = ...` 这一行改成新值；没有就补一行。"""
    text = _read(path)
    lines = text.split('\n') if text else []
    hit = False
    for i, line in enumerate(lines):
        if line.startswith(name) and '=' in line:
            lines[i] = "%s = %r" % (name, value)
            hit = True
    if not hit:
        lines.append("%s = %r" % (name, value))
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write('\n'.join(lines))
    return hit


def _has_real_config():
    """中转配置里有没有真东西（空的不算，免得把好备份覆盖掉）。"""
    url_ok = "RELAY_URL = '" in _read(SOURCE_PY) and "RELAY_URL = ''" not in _read(SOURCE_PY)
    key_ok = "RELAY_KEY = '" in _read(KEY_PY) and "RELAY_KEY = ''" not in _read(KEY_PY)
    return url_ok or key_ok


def backup():
    if not _has_real_config():
        print('· 中转配置已经是空的，不用备份')
        return False
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for path in FILES:
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(BACKUP_DIR, os.path.basename(path)))
    print('· 中转配置已备份到「%s」' % os.path.basename(BACKUP_DIR))
    return True


def clear():
    _set(SOURCE_PY, 'RELAY_URL', '')
    _set(KEY_PY, 'RELAY_KEY', '')
    print('· 已清空 RELAY_URL / RELAY_KEY（这版不带中转）')


def restore():
    got = 0
    for path in FILES:
        saved = os.path.join(BACKUP_DIR, os.path.basename(path))
        if os.path.isfile(saved):
            shutil.copy2(saved, path)
            got += 1
    if got:
        print('· 已从备份恢复中转配置（%d 个文件）' % got)
    else:
        print('· 备份里什么都没有，没动')
    return got


def main(argv):
    args = set(argv[1:])
    if '--restore' in args:
        return 0 if restore() else 0

    print('=== 打一版不带上传中转的安装包 ===')
    backup()
    clear()
    if '--permanent' in args:
        print('· --permanent：配置不还回去了（以后 make_installer 默认也是不带中转的）')
    if '--no-build' in args:
        return 0
    code = set_library_url.build()
    if code == 0 and '--permanent' not in args:
        restore()
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))