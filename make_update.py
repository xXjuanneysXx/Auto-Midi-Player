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

    # 3) `--old` 可以给好几个：每个旧版本各做一份「直达到新版」的累积差分包
    python -X utf8 make_update.py --version 1.0.6 --old 1.0.5 1.0.4 1.0.3 ...

`--patch-base` / `--package-base` 是「文件传上去之后能下载到的地址前缀」，脚本拿它
拼出 update.json 里的 url。文件传到哪儿见 `docs\增量更新设计.md` 第六节。

新写的 update.json 会把**上一版清单里挂着的差分包接着带上**（那些包已经传上去了，
不用重做），所以清单里会同时有「一跳一跳的老包」和「直达到最新版的累积包」：
客户端优先走一跳到位的，接不上才顺着老包一跳一跳过去。
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

    路径按**不区分大小写**比（Windows 的安装目录本来就不区分）：PyInstaller
    打包时同一个 DLL 有时候写成 `VCRUNTIME140.dll`、有时候写成
    `vcruntime140.dll`，要是当成「删一个、加一个」，更新器会先把这个文件写
    进去、回头又按「删除」列表把它删掉 —— 这个坑真踩过（v1.0.6 的 lite 包
    实测，装完少两个 VC 运行库）。返回的「变了」用的是新版清单里的写法。
    """
    old_by_lower = {}
    for rel, info in (old_files or {}).items():
        old_by_lower.setdefault(rel.lower(), (rel, info))
    changed = []
    for rel, info in sorted(new_files.items()):
        hit = old_by_lower.get(rel.lower())
        if not hit or (hit[1] or {}).get('sha256') != info.get('sha256'):
            changed.append(rel)
    new_by_lower = set(rel.lower() for rel in new_files)
    removed = [rel for rel in sorted(old_files or {}) if rel.lower() not in new_by_lower]
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


def _ver_tuple(text):
    """版本号 -> 元组，用来把差分包排个好看的顺序（1.0.3 排在 1.0.10 前面）。"""
    out = []
    for chunk in str(text or '').strip().lstrip('vV').split('.'):
        digits = ''.join(char for char in chunk if char.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def prev_update_json(old_versions):
    """
    上一版发版时写的 update.json（里面挂着更早的差分包）。

    那些包已经传上去了，这一版不用重做，原样抄进新清单就行 —— 这样手上是
    更老版本的用户（比如 1.0.3）就能顺着 1.0.3->1.0.4->1.0.5->1.0.6 这条
    链一跳一跳接过来，不用去下完整安装包。
    """
    for version in sorted(old_versions, key=_ver_tuple, reverse=True):
        path = os.path.join(OUT_DIR, version, 'update.json')
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get('editions'), dict):
            return path, data['editions']
    return '', {}


def carry_patches(previous, key):
    """把上一版清单里挂着的差分包原样抄过来（已经传上去的，不重做）。"""
    out = []
    for item in (((previous or {}).get(key) or {}).get('patches') or []):
        if not isinstance(item, dict):
            continue
        src = str(item.get('from') or '').strip()
        dst = str(item.get('to') or '').strip()
        url = str(item.get('url') or '').strip()
        if not (src and dst and url):
            continue
        out.append({'from': src, 'to': dst, 'url': url,
                    'size': item.get('size') or 0,
                    'sha256': str(item.get('sha256') or ''),
                    'removed': list(item.get('removed') or [])})
    return out


def package_info(version, key):
    """完整安装包（发布\\ 里那个 exe）的地址 / 大小 / sha256。"""
    name = '%s v%s.exe' % (PACKAGE_STEM[key], version)
    path = os.path.join(RELEASE_DIR, name)
    if not os.path.isfile(path):
        return None, name
    return {'path': path, 'name': name, 'size': os.path.getsize(path),
            'sha256': sha256_file(path)}, name


def build_update_json(version, patches, out_dir, patch_base, package_base,
                      notes='', min_supported='', previous=None):
    """
    patches: {完全版/精简版: [这次新做的差分包条目]}
    previous: 上一版 update.json 里的 editions —— 它挂着的旧差分包原样接着用。
    """
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
        buckets = {}
        for item in carry_patches(previous, key):
            buckets[(item['from'], item['to'])] = item
        for got in (patches.get(key) or []):
            buckets[(got['from'], got['to'])] = {
                'from': got['from'], 'to': got['to'],
                'url': _url(patch_base, got['name']),
                'size': got['size'], 'sha256': got['sha256'],
                'removed': got['removed'],
            }
        # 排序：同一个 from 里，**to 最新的排最前面**。
        # 为什么重要：老客户端（v1.0.6 以前）的 patch_for() 只认「第一条
        # from == 自己版本」的包，要是 1.0.4→1.0.5 排在 1.0.4→1.0.6 前面，
        # 1.0.4 的用户就先升到 1.0.5 去了，得再点一次更新才能到最新版。
        entry['patches'] = [buckets[tag] for tag in sorted(
            buckets, key=lambda tag: (_ver_tuple(tag[0]), tuple(-n for n in _ver_tuple(tag[1]))))]
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
    parser.add_argument('--old', default=[], nargs='*',
                        help='要拿来做差分包的旧版本号，可以给多个，例如 '
                             '--old 1.0.5 1.0.4 1.0.3（不给就不做差分包）。'
                             '给多个就是「累积差分包」：每个旧版本各做一份直达到新版本的包')
    parser.add_argument('--carry', default='',
                        help='从哪个 update.json 里接着抄旧差分包（默认自动认 --old 里最新那版的）')
    parser.add_argument('--manifest-only', action='store_true', help='只生成清单')
    parser.add_argument('--patch-base', default='', help='差分包上传后的地址前缀')
    parser.add_argument('--package-base', default='', help='完整安装包上传后的地址前缀')
    parser.add_argument('--notes', default='', help='写进 update.json 的更新说明')
    parser.add_argument('--min-supported', default='', help='低于这个版本必须走完整包')
    args = parser.parse_args(argv)

    version = str(args.version).strip().lstrip('vV')
    old_versions = []
    for raw in (args.old or []):
        for chunk in str(raw).replace(',', ' ').split():
            text = chunk.strip().lstrip('vV')
            if text and text not in old_versions:
                old_versions.append(text)

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

    if args.manifest_only or not old_versions:
        log('只生成清单（没给 --old 或者指定了 --manifest-only），到这儿就完事。')
        return 0

    out_dir = os.path.join(OUT_DIR, version)
    os.makedirs(out_dir, exist_ok=True)
    made = {}
    for key, label in EDITIONS:
        for old_version in old_versions:
            old_files = load_manifest(old_version, key)
            if not old_files:
                log('[!] %s 没有 %s 版的清单（%s），这个旧版本不做增量，客户端会用完整包。'
                    % (label, old_version, manifest_path(old_version, key)))
                continue
            changed, removed = diff(old_files, new_manifests[key])
            if not changed and not removed:
                log('[=] %s 跟 %s 一模一样，没有增量可做。' % (label, old_version))
                continue
            path, name = build_patch(payload_path(key), out_dir, key, version, old_version,
                                     changed, removed, new_manifests[key])
            made.setdefault(key, []).append({
                'from': old_version, 'to': version, 'name': name,
                'size': os.path.getsize(path), 'sha256': sha256_file(path),
                'removed': removed, 'changed': len(changed)})
            log('%s 差分包好了：%s（%s -> %s，改了 %d 个、删了 %d 个，%.1f MB）'
                % (label, path, old_version, version, len(changed), len(removed),
                   os.path.getsize(path) / 1048576.0))

    carry_path, previous = ('', {})
    if args.carry:
        try:
            with open(args.carry, encoding='utf-8') as handle:
                carry_path = args.carry
                previous = (json.load(handle) or {}).get('editions') or {}
        except (OSError, ValueError) as error:
            log('[!] --carry 那份读不了（%s），这版只放新做的差分包。' % error)
            carry_path, previous = '', {}
    else:
        carry_path, previous = prev_update_json(old_versions)
    if carry_path:
        log('旧差分包接着用：%s（清单里已经挂着的那些，不重做）' % carry_path)

    path, data = build_update_json(version, made, out_dir,
                                   args.patch_base, args.package_base,
                                   notes=args.notes, min_supported=args.min_supported,
                                   previous=previous)
    total = sum(len((entry.get('patches') or [])) for entry in data['editions'].values())
    log('update.json 好了：%s（latest=%s，清单里差分包 %d 条）'
        % (path, version, total))
    log('')
    log('接下来把这些传上去（见 docs\\增量更新设计.md）：')
    log('  差分包  -> Gitee Releases 附件（+ GitHub Releases 各一份）')
    log('  安装包  -> GitHub Releases（tag v%s）' % version)
    log('  update.json / version.json / notice.json -> 曲库仓库根目录（Gitee + GitHub）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
