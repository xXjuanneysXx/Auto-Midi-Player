# -*- coding: utf-8 -*-
r"""
看一眼 Gitee Releases 的附件配额，顺手清掉没人引用的差分包
=========================================================

Gitee 免费仓库的 **Releases 附件总容量是 1 GB**（所有 Release 加起来），满了以后
任何上传都会回 `HTTP 400 验证失败：文件大小已超出仓库附件配额：1 GB` ——
**跟文件大小无关，只差 1 MB 也传不上去**。v1.1.1 发版时就撞上这个（1022.7 / 1024 MB）。

这个脚本干两件事：

1. 列每个 Release 挂了几个附件、共多少 MB，还有总占用 / 剩余；
2. 算出**没有任何清单引用**的附件（拿 `曲库索引\update.json` 的 patches + 
   `曲库索引\payload.json` 的切片 + 在线安装程序 exe 当「引用源」），
   默认只列出来，加 `--go` 才真删。

什么时候会有「没人引用」的差分包？`make_update.py` 会把**改动集合完全一样**的
几份差分组合成一份（方案 C，见 `docs\发版策略.md`），那些多出来的重复包就没人引用了；
再就是 `min_supported` 往前推之后，早年的老包也会变成没人引用。

用法（项目目录下）：

    python -X utf8 清理Gitee附件.py            # 只看：占用多少、哪些没人引用
    python -X utf8 清理Gitee附件.py --go       # 真删

⚠ 删之前它会先跟「线上清单里引用的文件名」对一遍，只有**一条都没引用**的才删；
   切片、完整安装包、在线安装程序一律不碰（那是给人下载的，不是给清单引用的）。
"""

import argparse
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests                                                    # noqa: E402

OWNER = 'juanneys'
REPO = 'midi-music'
API = 'https://gitee.com/api/v5/repos/%s/%s' % (OWNER, REPO)
INDEX = os.path.join(HERE, '曲库索引')
QUOTA_MB = 1024


def log(text):
    print(text, flush=True)


def token_of():
    try:
        return str(__import__('gitee_token_local').TOKEN or '').strip()
    except Exception:
        return ''


def releases(token):
    got = requests.get(API + '/releases', params={'access_token': token, 'per_page': 100},
                       timeout=30)
    got.raise_for_status()
    data = got.json()
    return data if isinstance(data, list) else []


def attachments(release, token):
    got = requests.get(API + '/releases/%s/attach_files' % release['id'],
                       params={'access_token': token}, timeout=30)
    if got.status_code != 200:
        return []
    data = got.json()
    return [a for a in (data if isinstance(data, list) else []) if a.get('name')]


def referenced():
    """线上清单引用到的文件名：update.json 的差分包 + payload.json 的切片。"""
    out = set()
    try:
        with open(os.path.join(INDEX, 'update.json'), encoding='utf-8') as handle:
            update = json.load(handle)
    except (OSError, ValueError):
        update = {}
    for entry in (update.get('editions') or {}).values():
        for item in ((entry or {}).get('patches') or []):
            url = str((item or {}).get('url') or '')
            if url:
                out.add(urllib.parse.unquote(url.rsplit('/', 1)[-1]))
    try:
        with open(os.path.join(INDEX, 'payload.json'), encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        payload = {}
    for entry in (payload.get('editions') or {}).values():
        for part in ((entry or {}).get('parts') or []):
            if part.get('name'):
                out.add(str(part['name']))
    return out


def keep_anyway(name):
    """这些东西是给人下载的（不是给清单引用的），一律不删。"""
    return name.startswith(('AutoPlay-full-', 'AutoPlay-lite-', 'AutoPlay 在线安装程序'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='看 Gitee 附件配额、清掉没人引用的差分包')
    parser.add_argument('--go', action='store_true', help='真删（不给就只看）')
    args = parser.parse_args(argv)

    token = token_of()
    if not token:
        log('[x] 没找到令牌（gitee_token_local.py）')
        return 1

    rows = []
    total = 0
    for release in releases(token):
        files = attachments(release, token)
        size = sum(int(a.get('size') or 0) for a in files)
        total += size
        rows.append((release.get('tag_name'), release['id'], files, size))
    for tag, _rid, files, size in rows:
        log('%-10s %2d 个附件 %8.1f MB' % (tag, len(files), size / 1048576.0))
    log('-' * 46)
    log('合计 %.1f MB / 配额 %d MB（还剩 %.1f MB）'
        % (total / 1048576.0, QUOTA_MB, QUOTA_MB - total / 1048576.0))

    used = referenced()
    dead = []
    for tag, rid, files, _size in rows:
        for item in files:
            name = str(item.get('name'))
            if name in used or keep_anyway(name):
                continue
            dead.append((tag, rid, item.get('id'), name, int(item.get('size') or 0)))
    log('')
    if not dead:
        log('没有没人引用的附件 —— 不用清。')
        return 0
    freed = sum(x[4] for x in dead)
    log('没人引用的附件：%d 个，%.1f MB' % (len(dead), freed / 1048576.0))
    for tag, _rid, _fid, name, size in dead:
        log('    %-46s %-8s %6.1f MB' % (name, tag, size / 1048576.0))
    if not args.go:
        log('')
        log('（只看：加 --go 才真删）')
        return 0
    log('')
    code = 0
    for _tag, rid, fid, name, _size in dead:
        got = requests.delete('%s/releases/%s/attach_files/%s' % (API, rid, fid),
                              params={'access_token': token}, timeout=60)
        ok = got.status_code in (200, 204)
        code = code or (0 if ok else 1)
        log('  删 %-46s %s' % (name, 'OK' if ok else 'HTTP %s %s'
                               % (got.status_code, got.text[:150])))
    log('腾出 %.1f MB' % (freed / 1048576.0))
    return code


if __name__ == '__main__':
    raise SystemExit(main())