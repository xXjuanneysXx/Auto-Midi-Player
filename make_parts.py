# -*- coding: utf-8 -*-
r"""
把 payload 切成小片 + 写下载清单（给「在线安装程序」用）
=======================================================

在线安装程序本身只有 46 MB 左右（Qt 安装器本体），不带几百兆的程序本体；它靠这份清单
知道「要下什么、下下来的东西对不对」。

这个脚本做三件事：

1. 把 `build_installer\payload-full.zip` / `payload-lite.zip` 按**固定大小切片**
   （默认 32 MB）放到 `发布\分片\`；
2. 算每片的 sha256 和整包的 sha256；
3. 写 `曲库索引\payload.json`（发到曲库仓库根目录，跟 version.json 一样联网拉）。

为什么切 32 MB：Gitee Releases 单个附件上限 100 MB，切小了断点续传代价小
（坏一片只用重下 32 MB），切太碎附件数量又多。完全版约 6 片、精简版 2 片。

用法（项目目录下）：

    python -X utf8 make_parts.py                 # 切片 + 写清单（默认 32 MB / v1.1.0）
    python -X utf8 make_parts.py --chunk-mb 64   # 想切大点
    python -X utf8 make_parts.py --list          # 只把「要传哪几个文件」列出来
"""

import argparse
import hashlib
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(HERE, 'build_installer')
INDEX_DIR = os.path.join(HERE, '曲库索引')
CHUNK_DIR = os.path.join(HERE, '发布', '分片')
MANIFEST_NAME = 'payload.json'

# 版本 / 两个 payload 在哪儿 / 附件地址的模板
def _app_version(fallback='1.1.0'):
    """程序版本号直接从 installer.py 里读 —— 省得两处手改忘了对上。"""
    try:
        with open(os.path.join(HERE, 'installer.py'), encoding='utf-8') as handle:
            for line in handle:
                if line.startswith('APP_VERSION'):
                    return line.split('=', 1)[1].strip().strip('\'"')
    except OSError:
        pass
    return fallback


DEFAULT_VERSION = _app_version()
DEFAULT_CHUNK_MB = 32
GITEE_RELEASE = 'https://gitee.com/juanneys/midi-music/releases/download/v%s'

EDITIONS = (
    ('full', '完全版', 'payload-full.zip'),
    ('lite', '精简版', 'payload-lite.zip'),
)


def log(text):
    print(text, flush=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def install_size(path):
    """payload zip 解压后大概占多少磁盘（给在线安装程序算空间用）。"""
    with zipfile.ZipFile(path) as archive:
        return sum(info.file_size for info in archive.infolist() if not info.is_dir())


def split_file(path, chunk, out_dir, prefix):
    """把一个文件切成小片，返回 [{'name','size','sha256','path'}, ...]。"""
    parts = []
    index = 0
    with open(path, 'rb') as handle:
        while True:
            blob = handle.read(chunk)
            if not blob:
                break
            index += 1
            name = '%s.%03d' % (prefix, index)
            dest = os.path.join(out_dir, name)
            with open(dest, 'wb') as out:
                out.write(blob)
            parts.append({'name': name, 'size': len(blob),
                          'sha256': hashlib.sha256(blob).hexdigest(), 'path': dest})
    return parts


def build(version=DEFAULT_VERSION, chunk_mb=DEFAULT_CHUNK_MB):
    chunk = int(chunk_mb) * 1024 * 1024
    os.makedirs(CHUNK_DIR, exist_ok=True)
    os.makedirs(INDEX_DIR, exist_ok=True)
    base = GITEE_RELEASE % version
    manifest = {
        'format': 'autoplay-payload',
        'version': 1,
        'app_version': version,
        'chunk_size': chunk,
        'note': '在线安装程序读它：每个版本的 payload 切片、每片多大、sha256 多少、去哪儿下。'
                '清单里没有的版本就是还没做在线安装包。',
        'mirrors': [],                     # 备用源（和 Gitee 同样放一份切片时往这儿加地址）
        'editions': {},
    }
    uploads = []
    for key, label, filename in EDITIONS:
        path = os.path.join(BUILD_DIR, filename)
        if not os.path.isfile(path):
            log('· 跳过 %s：找不到 %s（先跑 make_installer.py payload）' % (label, path))
            continue
        total = os.path.getsize(path)
        prefix = 'AutoPlay-%s-%s.zip' % (key, version)
        parts = split_file(path, chunk, CHUNK_DIR, prefix)
        for part in parts:
            uploads.append(part['path'])
        manifest['editions'][key] = {
            'label': label,
            'zip': filename,
            'size': total,
            'install_size': install_size(path),
            'sha256': sha256_file(path),
            'parts': [{'name': p['name'], 'size': p['size'], 'sha256': p['sha256']}
                      for p in parts],
            'base_urls': [base],
        }
        log('· %s：%s -> %d 片（%s）' % (label, filename, len(parts),
                                         ', '.join(p['name'] for p in parts)))
    dest = os.path.join(INDEX_DIR, MANIFEST_NAME)
    with open(dest, 'w', encoding='utf-8', newline='\n') as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    log('· 清单写好了：%s' % dest)
    log('· 切片在：%s' % CHUNK_DIR)
    log('')
    log('要传的文件（Gitee Releases → v%s 的附件）：' % version)
    for path in uploads:
        log('    %s' % path)
    log('还有这份清单要发到曲库仓库根目录：%s' % dest)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='把 payload 切片并写下载清单')
    parser.add_argument('--version', default=DEFAULT_VERSION, help='程序版本号，默认 %s' % DEFAULT_VERSION)
    parser.add_argument('--chunk-mb', type=int, default=DEFAULT_CHUNK_MB,
                        help='每片多大（MB），默认 %d' % DEFAULT_CHUNK_MB)
    parser.add_argument('--list', action='store_true', help='只列已经切好的片，不重新切')
    args = parser.parse_args(argv)
    if args.list:
        for name in sorted(os.listdir(CHUNK_DIR)) if os.path.isdir(CHUNK_DIR) else []:
            log(os.path.join(CHUNK_DIR, name))
        return 0
    return build(args.version, args.chunk_mb)


if __name__ == '__main__':
    sys.exit(main())
