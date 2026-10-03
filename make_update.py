# -*- coding: utf-8 -*-
r"""
发版端：生成「增量更新」的清单和差分包（make_update.py）
======================================================

每次发新版，打完安装包之后再跑一遍这个脚本，它会：

1. 把 `build_installer\payload-<完全版/精简版>.zip` 里 **app\ 那棵树**（也就是
   装到安装目录里的那些文件）逐个算 sha256，存成一份**清单**：
   `更新包\manifests\<版本>-<full|lite>.json`；
2. 跟上一版的清单比一比，找出**内容变了的文件**（增量文件 —— 判定方法见
   `docs\增量更新设计.md` 第五节：不看时间、不看大小，**只看 sha256**，
   一样就是没变）；
3. 把「变了的文件」装进一个 zip，就是**差分包**：
   `更新包\<版本>\AutoPlay-patch-<edition>-<旧版本>-to-<新版本>.zip`。
   zip 里还有一份 `_patch\manifest.json`：告诉更新器每个文件应该是什么 sha256、
   以及哪些文件**删掉了**（新版本里没有了）；
4. 写一份 `update.json`（总清单）：新版本号、每个文件的 sha256、差分包地址、
   完整安装包地址。客户端拉的就是它。

用法（项目目录下）：

    # 1) 先给「上一版」留一份清单（只算清单，不生成差分包）
    python -X utf8 make_update.py --version 1.0.2 --manifest-only

    # 2) 发新版（打完包之后）：生成 1.0.2 -> 1.0.3 的差分包 + update.json
    python -X utf8 make_update.py --version 1.0.3 --old 1.0.2 ^
        --patch-base "https://gitee.com/juanneys/midi-music/releases/download/v1.0.3" ^
        --package-base "https://github.com/xXjuanneysXx/Auto-Midi-Player/releases/download/v1.0.3"

`--patch-base` / `--package-base` 是「文件传上去之后能下载到的地址前缀」，脚本拿它
拼出 update.json 里的 url。文件传到哪儿见 `docs\增量更新设计.md` 第六节。
"""

import argparse
import hashlib
import json
import os
import time
import urllib.parse
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD_DIR = os.path.join(HERE, 'build_installer')
RELEASE_DIR = os.path.join(HERE, '发布')
OUT_DIR = os.path.join(HERE, '更新包')
MANIFEST_DIR = os.path.join(OUT_DIR, 'manifests')
EDITIONS = (('full', '完全版'), ('lite', '精简版'))
PACKAGE_STEM = {'full': 'AutoPlay 完全版 安装程序', 'lite': 'AutoPlay 精简版 安装程序'}
FORMAT_UPDATE = 'autoplay-update'
FORMAT_PATCH = 'autoplay-patch'
CHUNK = 1 << 20


def log(text):
    print(text, flush=True)


# ---------- 清单 ----------

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_zip_member(zf, info):
    digest = hashlib.sha256()
    with zf.open(info) as source:
        while True:
            chunk = source.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def payload_path(key):
    return os.path.join(PAYLOAD_DIR, 'payload-%s.zip' % key)


def payload_manifest(zip_path):
    """
    payload.zip -> {相对安装目录的路径: {'size': n, 'sha256': '...'}}

    只看 `app/` 那棵树（装到安装目录根上的程序本体）：`songs/` 是曲库，用户自己也会
    往里放曲子，更新时一律不碰（要新曲子走「联网曲库」）。
    """
    out = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            if info.is_dir() or not name.startswith('app/'):
                continue
            rel = name[4:]
            if not rel or '..' in rel.split('/'):
                continue
            out[rel] = {'size': info.file_size, 'sha256': sha256_zip_member(zf, info)}
    return out


def manifest_path(version, key):
    return os.path.join(MANIFEST_DIR, '%s-%s.json' % (version, key))


def save_manifest(version, key, files):
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    path = manifest_path(version, key)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump({'version': version, 'edition': key, 'files': files},
                  handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write('\n')
    return path


def load_manifest(version, key):
    path = manifest_path(version, key)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except Exception:
        return None
    files = data.get('files')
    return files if isinstance(files, dict) else None


# ---------- 差异 ----------

def diff(old_files, new_files):
    """
    比两版清单，返回 (变了的文件, 删掉的文件)。

    **只比 sha256**：值不一样（或者新版本才有的文件）就算「变了」。
    """
    changed = [rel for rel, info in sorted(new_files.items())
               if not old_files or (old_files.get(rel) or {}).get('sha256') != info.get('sha256')]
    removed = [rel for rel in sorted(old_files or {}) if rel not in new_files]
    return changed, removed


def build_patch(payload, out_dir, key, version, old_version, changed, removed, new_files):
    name = 'AutoPlay-patch-%s-%s-to-%s.zip' % (key, old_version, version)
    out = os.path.join(out_dir, name)
    with zipfile.ZipFile(payload) as src, \
            zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
        for rel in changed:
            data = src.read(src.getinfo('app/' + rel))
            dst.writestr(rel, data)
        inside = {
            'format': FORMAT_PATCH,
            'edition': key,
            'from': old_version,
            'to': version,
            'files': {rel: new_files[rel] for rel in changed},
            'removed': removed,
        }
        dst.writestr('_patch/manifest.json',
                     json.dumps(inside, ensure_ascii=False, indent=2))
    return out, name


# ---------- update.json ----------

def _url(base, name):
    base = str(base or '').strip().rstrip('/')
    if not base:
        return name
    return '%s/%s' % (base, urllib.parse.quote(name))


def package_info(version, key):
    """完整安装包（发布\\ 里那个 exe）的地址 / 大小 / sha256。"""
    name = '%s v%s.exe' % (PACKAGE_STEM[key], version)
    path = os.path.join(RELEASE_DIR, name)
    if not os.path.isfile(path):
        return None, name
    return {'path': path, 'name': name, 'size': os.path.getsize(path),
            'sha256': sha256_file(path)}, name


def build_update_json(version, old_version, patches, out_dir, patch_base, package_base,
                      notes='', min_supported=''):
    editions = {}
    for key, label in EDITIONS:
        files = load_manifest(version, key) or {}
        pkg, _name = package_info(version, key)
        entry = {
            'label': label,
            'version': version,
            'package': ({'url': _url(package_base, pkg['name']), 'size': pkg['size'],
                         'sha256': pkg['sha256']} if pkg else {}),
            'files': [{'path': rel, 'size': info.get('size', 0),
                       'sha256': info.get('sha256', '')}
                      for rel, info in sorted(files.items())],
            'patches': [],
        }
        got = patches.get(key)
        if got:
            entry['patches'].append({
                'from': old_version, 'to': version,
                'url': _url(patch_base, got['name']),
                'size': got['size'], 'sha256': got['sha256'],
                'removed': got['removed'],
            })
        editions[key] = entry
    data = {
        'format': FORMAT_UPDATE,
        'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
        'latest': version,
        'min_supported': min_supported or version,
        'notes': notes,
        'editions': editions,
    }
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, 'update.json')
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return path, data


# ---------- 主流程 ----------

def main(argv=None):
    parser = argparse.ArgumentParser(description='生成增量更新清单 / 差分包 / update.json')
    parser.add_argument('--version', required=True, help='新版本号，例如 1.0.3')
    parser.add_argument('--old', default='', help='上一版版本号，例如 1.0.2（不给就不做差分包）')
    parser.add_argument('--manifest-only', action='store_true', help='只生成清单')
    parser.add_argument('--patch-base', default='', help='差分包上传后的地址前缀')
    parser.add_argument('--package-base', default='', help='完整安装包上传后的地址前缀')
    parser.add_argument('--notes', default='', help='写进 update.json 的更新说明')
    parser.add_argument('--min-supported', default='', help='低于这个版本必须走完整包')
    args = parser.parse_args(argv)

    version = str(args.version).strip().lstrip('vV')
    old_version = str(args.old).strip().lstrip('vV')

    new_manifests = {}
    for key, label in EDITIONS:
        payload = payload_path(key)
        if not os.path.isfile(payload):
            raise SystemExit('[x] 没找到 %s —— 先跑 python -X utf8 make_installer.py all' % payload)
        files = payload_manifest(payload)
        new_manifests[key] = files
        path = save_manifest(version, key, files)
        total = sum(info['size'] for info in files.values())
        log('%s 清单好了：%s（%d 个文件，共 %.1f MB）'
            % (label, path, len(files), total / 1048576.0))

    if args.manifest_only or not old_version:
        log('只生成清单（没给 --old 或者指定了 --manifest-only），到这儿就完事。')
        return 0

    out_dir = os.path.join(OUT_DIR, version)
    os.makedirs(out_dir, exist_ok=True)
    patches = {}
    for key, label in EDITIONS:
        old_files = load_manifest(old_version, key)
        if not old_files:
            log('[!] %s 没有 %s 版的清单（%s），这一版不做增量，客户端会用完整包。'
                % (label, old_version, manifest_path(old_version, key)))
            continue
        changed, removed = diff(old_files, new_manifests[key])
        if not changed and not removed:
            log('[=] %s 跟 %s 一模一样，没有增量可做。' % (label, old_version))
            continue
        path, name = build_patch(payload_path(key), out_dir, key, version, old_version,
                                 changed, removed, new_manifests[key])
        patches[key] = {'name': name, 'size': os.path.getsize(path),
                        'sha256': sha256_file(path), 'removed': removed,
                        'changed': len(changed)}
        log('%s 差分包好了：%s（改了 %d 个、删了 %d 个，%.1f MB）'
            % (label, path, len(changed), len(removed), os.path.getsize(path) / 1048576.0))

    path, data = build_update_json(version, old_version, patches, out_dir,
                                   args.patch_base, args.package_base,
                                   notes=args.notes, min_supported=args.min_supported)
    log('update.json 好了：%s（latest=%s，两版差分包 %d 个）' % (path, version, len(patches)))
    log('')
    log('接下来把这些传上去（见 docs\\增量更新设计.md）：')
    log('  差分包  -> Gitee Releases 附件（+ GitHub Releases 各一份）')
    log('  安装包  -> GitHub Releases（tag v%s）' % version)
    log('  update.json / version.json / notice.json -> 曲库仓库根目录（Gitee + GitHub）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())