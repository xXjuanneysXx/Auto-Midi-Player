# -*- coding: utf-8 -*-
r"""
把「发布\分片\*」作为附件传到 Gitee 的 vX.Y.Z Release
====================================================

在线安装程序（不挂 payload 的那个小 exe）启动后会去拉曲库仓库根目录的 payload.json，
照里面的地址下切片。这个脚本就是「把切片传上去」那一步，省得在网页上一个一个点
（完全版 6 片 + 精简版 2 片，一共 8 个附件；Gitee 单个附件上限 100 MB，所以按
32 MB 切）。

用法（项目目录下）：

    python -X utf8 上传分片到Gitee.py --dry-run     # 只说要传什么，不真传
    python -X utf8 上传分片到Gitee.py               # 传切片（已传过的跳过）
    python -X utf8 上传分片到Gitee.py --manifest    # 顺手把 payload.json 发到两个曲库仓库
    python -X utf8 上传分片到Gitee.py --patches     # 顺手把 更新包\<版本>\*.zip（增量差分包）也传上去
    python -X utf8 上传分片到Gitee.py --list        # 看看这个 Release 现在挂了哪些附件
    python -X utf8 上传分片到Gitee.py --tag v1.1.0  # 换一个版本

它做的事：

1. 读 `曲库索引\payload.json`，知道「哪一版、哪几片、每片多大」（清单是
   `make_parts.py` 写的，切片也是它切的）；
2. 找 Gitee 上对应的 Release —— 没有就按 `--tag` 建一个；
3. 逐个传附件：**同名同大小的已经在了就跳过**，所以重跑不会重复传，传一半断了
   接着跑就行；
4. `--manifest`：把 payload.json 用 contents 接口写进 Gitee / GitHub 两个曲库仓库
   （Gitee 用 URL 上的 access_token，GitHub 用 Authorization 头，都在 `library.py` 里）。
5. `--patches`：把 `更新包\<版本>\` 里的增量差分包（`AutoPlay-patch-*.zip`）也传成
   这个 Release 的附件 —— `update.json` 里写的下载地址就是它们。

令牌在 `gitee_token_local.py`（跟曲库上传共用同一个，不进 git）。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import requests                                                    # noqa: E402

MANIFEST = os.path.join(HERE, '曲库索引', 'payload.json')
CHUNK_DIR = os.path.join(HERE, '发布', '分片')
OWNER = 'juanneys'
REPO = 'midi-music'
API = 'https://gitee.com/api/v5/repos/%s/%s' % (OWNER, REPO)


def log(text):
    print(text, flush=True)


def gitee_token():
    """Gitee 令牌：读 gitee_token_local.py（跟曲库上传用的是同一个）。"""
    try:
        value = str(__import__('gitee_token_local').TOKEN or '').strip()
    except Exception:
        return ''
    return '' if value in ('xxx', 'your-token') else value


def load_manifest():
    with open(MANIFEST, encoding='utf-8') as handle:
        return json.load(handle)


def find_release(tag, token, create=True):
    """找 tag 对应的 Release；没有就建一个。返回 (release dict, 出错信息)。"""
    url = API + '/releases/tags/' + tag
    response = requests.get(url, params={'access_token': token}, timeout=30)
    if response.status_code == 200:
        data = response.json()
        if isinstance(data, dict) and data.get('id'):
            return data, ''
        # Gitee 在「没有这个 tag」时也会回 200，body 是 null —— 当成没有接着建
    elif response.status_code != 404:
        return None, '查 Release 失败：HTTP %s %s' % (response.status_code, response.text[:200])
    if not create:
        return None, 'Gitee 上还没有 %s 这个 Release' % tag
    log('· Gitee 上还没有 %s，建一个' % tag)
    response = requests.post(API + '/releases', timeout=30, data={
        'access_token': token,
        'tag_name': tag,
        'name': tag,
        'target_commitish': 'master',
        'body': 'AutoPlay %s：在线安装程序的切片、增量更新包' % tag,
    })
    if response.status_code not in (200, 201):
        return None, '建 Release 失败：HTTP %s %s' % (response.status_code, response.text[:300])
    return response.json(), ''


def list_attachments(release_id, token):
    response = requests.get(API + '/releases/%s/attach_files' % release_id,
                            params={'access_token': token}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError('列附件失败：HTTP %s %s'
                           % (response.status_code, response.text[:200]))
    data = response.json()
    return data if isinstance(data, list) else []


def upload_attachment(release_id, path, token):
    """传一个附件。大文件给足超时（32 MB 在国内上传要一会儿）。"""
    name = os.path.basename(path)
    with open(path, 'rb') as handle:
        response = requests.post(
            API + '/releases/%s/attach_files' % release_id,
            params={'access_token': token},
            files={'file': (name, handle, 'application/octet-stream')},
            timeout=(30, 3600))
    if response.status_code in (200, 201):
        return True, ''
    return False, 'HTTP %s %s' % (response.status_code, response.text[:300])


def push_manifest():
    """把 payload.json 写进两个曲库仓库（Gitee / GitHub），路径就是仓库根目录。"""
    import library
    with open(MANIFEST, 'rb') as handle:
        data = handle.read()
    code = 0
    for site in (library.SITE_GITEE, library.SITE_GITHUB):
        label = library.SITE_LABELS.get(site, site)
        info, why = library.backend_of(library.default_index_url(site))
        if info is None:
            log('  [%s] 跳过：%s' % (label, why))
            code = 1
            continue
        owner, repo, branch = info
        token = library.get_token(site)
        if not token:
            log('  [%s] 跳过：没有令牌' % label)
            code = 1
            continue
        url, bad = library.put_file(site, owner, repo, 'payload.json', data, token,
                                    branch=branch, timeout=60,
                                    message='AutoPlay：更新在线安装清单 payload.json')
        log('  [%s] %s' % (label, bad or ('OK ' + url)))
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description='把切片传到 Gitee Releases')
    parser.add_argument('--tag', default='', help='Release 标签，默认 v<清单里的版本>')
    parser.add_argument('--dir', default=CHUNK_DIR, help='切片放哪个目录，默认 发布\\分片')
    parser.add_argument('--dry-run', action='store_true', help='只列要传什么，不真传')
    parser.add_argument('--list', action='store_true', help='只列 Release 上已有的附件')
    parser.add_argument('--manifest', action='store_true', help='顺手把 payload.json 发到两个曲库仓库')
    parser.add_argument('--patches', action='store_true',
                        help='顺手把 更新包\\<版本>\\*.zip（增量差分包）也传上来')
    parser.add_argument('--no-create', action='store_true', help='Release 不存在时不自动建')
    args = parser.parse_args(argv)

    token = gitee_token()
    if not token:
        log('[x] 没找到 Gitee 令牌（gitee_token_local.py 里的 TOKEN）')
        return 1
    if not os.path.isfile(MANIFEST):
        log('[x] 没有 %s —— 先跑 python -X utf8 make_parts.py' % MANIFEST)
        return 1
    manifest = load_manifest()
    tag = args.tag or ('v' + str(manifest.get('app_version') or '').strip())
    if tag == 'v':
        log('[x] 清单里没写版本号，用 --tag 指定一个')
        return 1

    wanted = []
    for key, info in (manifest.get('editions') or {}).items():
        for part in (info.get('parts') or []):
            wanted.append((part['name'], os.path.join(args.dir, part['name']),
                           int(part['size']), info.get('label') or key))
    if args.patches:
        patch_dir = os.path.join(HERE, '更新包', tag.lstrip('vV'))
        names = sorted(os.listdir(patch_dir)) if os.path.isdir(patch_dir) else []
        for name in names:
            if name.lower().endswith('.zip'):
                path = os.path.join(patch_dir, name)
                wanted.append((name, path, os.path.getsize(path), '差分包'))
        if not names:
            log('[!] 没找到 %s —— 先跑一遍 make_update.py' % patch_dir)
    log('· 清单：%s（%d 个附件）' % (tag, len(wanted)))

    if args.dry_run:
        for name, path, size, label in wanted:
            exists = os.path.isfile(path)
            log('    %-34s %-6s %s' % (name, label,
                                       '本地有（%.1f MB）' % (size / 1048576.0) if exists
                                       else '本地缺文件：%s' % path))
        if args.manifest:
            log('· 还会把 %s 发到两个曲库仓库根目录' % os.path.relpath(MANIFEST, HERE))
        return 0

    release, why = find_release(tag, token, create=not args.no_create)
    if release is None:
        log('[x] %s' % why)
        return 1
    release_id = release.get('id')
    log('· Release %s（id=%s，%s）' % (release.get('tag_name'), release_id,
                                       release.get('name') or ''))
    existing = list_attachments(release_id, token)
    if args.list:
        for item in existing:
            log('    %-34s %s' % (item.get('name'),
                                  '%.1f MB' % (int(item.get('size') or 0) / 1048576.0)))
        return 0
    have = dict((item.get('name'), int(item.get('size') or 0)) for item in existing)

    code = 0
    for index, (name, path, size, label) in enumerate(wanted, 1):
        if have.get(name) == size:
            log('  [%d/%d] %s 已经在上面了，跳过' % (index, len(wanted), name))
            continue
        if not os.path.isfile(path):
            log('  [%d/%d] [x] 本地没有 %s（先跑 make_parts.py）' % (index, len(wanted), path))
            code = 1
            continue
        if os.path.getsize(path) != size:
            log('  [%d/%d] [x] %s 大小不对（本地 %d，清单写的 %d）——重新跑 make_parts.py'
                % (index, len(wanted), name, os.path.getsize(path), size))
            code = 1
            continue
        log('  [%d/%d] 传 %s（%.1f MB）…' % (index, len(wanted), name, size / 1048576.0))
        ok, bad = upload_attachment(release_id, path, token)
        if ok:
            log('        好了')
        else:
            log('        [x] %s' % bad)
            code = 1

    if args.manifest:
        log('· 把 payload.json 发到曲库仓库：')
        code = max(code, push_manifest())
    log('')
    log('附件地址示例：https://gitee.com/%s/%s/releases/download/%s/<片名>' % (OWNER, REPO, tag))
    log('清单里的 base_urls 就是这个前缀，对不上的话改 make_parts.py 再切一次。')
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
