# -*- coding: utf-8 -*-
r"""
把「曲库索引」里的那几个文件推到两个曲库仓库（Gitee + GitHub）
==============================================================

发版时除了切片和差分包，还得把这几份「程序联网读的 json / md」更新上去：

    payload.json    在线安装清单（配合 make_parts.py 切的片）
    notice.json     公告
    version.json    版本信息（最新版本号 / 更新说明 / 几个按钮的链接）
    update.json     增量更新清单（make_update.py 生成的，客户端读它决定下哪个差分包）
    README.md       曲库仓库的说明（给人看的）

    --manifests     再带上 曲库索引\manifests\<最新版>-*.json（留档用，客户端不读）

都是**两个仓库都写**：国内走 Gitee、海外 / 备用走 GitHub。

用法（项目目录下）：

    python -X utf8 推送曲库索引.py --dry-run      # 只说要改哪些，不推
    python -X utf8 推送曲库索引.py                # 推
    python -X utf8 推送曲库索引.py --only payload.json update.json
    python -X utf8 推送曲库索引.py --manifests

跟仓库里内容**一模一样**的会跳过（比对 git blob sha1），所以重跑不会刷一堆空提交。
令牌用的是程序里那两个（`gitee_token_local.py` / `github_token_local.py`），
都只在本地，不进 git。
"""

import argparse
import base64
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import library                                                     # noqa: E402

INDEX_DIR = os.path.join(HERE, '曲库索引')
DEFAULT_FILES = ('payload.json', 'notice.json', 'version.json', 'update.json', 'README.md')


def log(text):
    print(text, flush=True)


def git_blob_sha(data):
    """GitHub / Gitee 的 contents 接口里那个 sha 就是 git 的 blob sha1。"""
    head = ('blob %d\0' % len(data)).encode('utf-8')
    return hashlib.sha1(head + data).hexdigest()


def latest_version():
    """update.json 里的最新版本号（--manifests 用）。"""
    try:
        with open(os.path.join(INDEX_DIR, 'update.json'), encoding='utf-8') as handle:
            return str((json.load(handle) or {}).get('latest') or '').strip()
    except (OSError, ValueError):
        return ''


def manifest_files(version):
    folder = os.path.join(INDEX_DIR, 'manifests')
    if not os.path.isdir(folder):
        return []
    return [('manifests/' + name, os.path.join(folder, name))
            for name in sorted(os.listdir(folder))
            if version and name.startswith(version + '-') and name.endswith('.json')]


def remote_sha(site, owner, repo, branch, path, token, timeout=30):
    """仓库里这个文件现在的 sha（没有 / 读不到就给空的）。"""
    url = library._contents_url(site, owner, repo, path, branch)
    data, _code, _why = library._api_raw(url, token, timeout=timeout, site=site)
    if isinstance(data, dict):
        return str(data.get('sha') or '')
    return ''


def push(site, pairs, dry_run=False, timeout=60):
    label = library.SITE_LABELS.get(site, site)
    _site, owner, repo, branch, why = library.backend_of(library.default_index_url(site))
    if not owner or not repo:
        log('  [%s] 跳过：%s' % (label, why))
        return 1
    token = library.get_token(site)
    if not token:
        log('  [%s] 跳过：没有令牌' % label)
        return 1
    log('  [%s] %s/%s@%s' % (label, owner, repo, branch))
    code = 0
    for remote, local in pairs:
        with open(local, 'rb') as handle:
            data = handle.read()
        want = git_blob_sha(data)
        have = remote_sha(site, owner, repo, branch, remote, token)
        if have == want:
            log('    %-24s 没变，跳过' % remote)
            continue
        if dry_run:
            log('    %-24s 要更新（%.1f KB）' % (remote, len(data) / 1024.0))
            continue
        url, bad = library.put_file(site, owner, repo, remote, data, token, branch=branch,
                                    sha=have, timeout=timeout,
                                    message='AutoPlay：更新 %s' % remote)
        if bad:
            log('    %-24s [x] %s' % (remote, bad))
            code = 1
        else:
            log('    %-24s OK %s' % (remote, url))
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description='把曲库索引推到两个曲库仓库')
    parser.add_argument('--only', nargs='*', default=[], help='只推这几个（默认推一整套）')
    parser.add_argument('--manifests', action='store_true', help='连 manifests\\<最新版>-*.json 一起推')
    parser.add_argument('--dry-run', action='store_true', help='只说要改哪些')
    args = parser.parse_args(argv)

    names = tuple(args.only) if args.only else DEFAULT_FILES
    pairs = []
    for name in names:
        path = os.path.join(INDEX_DIR, name)
        if not os.path.isfile(path):
            log('[x] 没有 %s' % path)
            return 1
        pairs.append((name.replace('\\', '/'), path))
    if args.manifests:
        version = latest_version()
        extra = manifest_files(version)
        if not extra:
            log('[!] 曲库索引\\manifests 里没有 %s-*.json' % (version or '?'))
        pairs.extend(extra)
        log('· 带上 %d 份清单（%s）' % (len(extra), version or '?'))
    log('· 要推 %d 个文件：%s' % (len(pairs), '、'.join(name for name, _p in pairs)))

    code = 0
    for site in (library.SITE_GITEE, library.SITE_GITHUB):
        code = max(code, push(site, pairs, dry_run=args.dry_run))
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
