# -*- coding: utf-8 -*-
r"""
本地测试「在线安装程序」（不发任何东西到网上）
=============================================

在线安装程序要联网拉 `payload.json` 和切片才能装。正式测试前不方便先把 235 MB 的切片
传到 Gitee，所以这个脚本在**本机**临时把这两样伺候好：

1. 读 `曲库索引\payload.json`，把每个版本的 `base_urls` 改成 `http://127.0.0.1:<端口>`；
2. 起一个只读的本地 HTTP 服务：`/payload.json` 给改过的清单，其它路径给 `发布\分片\` 里的片；
3. 用环境变量 `AUTOPLAY_PAYLOAD_INDEX` 把安装程序的清单地址指到这个本地服务，
   然后把「在线安装程序」打开（就是 `发布\AutoPlay 在线安装程序 vX.Y.Z.exe`）。

接下来就跟用户拿到的一样：选版本 / 目录 → 点「开始安装」→ 看它下载、解压、建快捷方式。
想测断点续传：下到一半直接关掉窗口，再跑一遍这个脚本，看它是不是从断掉的片接着下
（已经下完的片会显示「已经下过了，跳过」）。

用法（项目目录下）：

    python -X utf8 测试在线安装.py                # 起服务 + 打开在线安装程序
    python -X utf8 测试在线安装.py --no-launch    # 只起服务，自己开 exe
    python -X utf8 测试在线安装.py --port 8123    # 指定端口
    python -X utf8 测试在线安装.py --exe dist_installer\AutoPlay-安装程序.exe

装出来的东西在界面上选的目录里（默认 `%LOCALAPPDATA%\AutoPlay`）；
下下来的片在 `%LOCALAPPDATA%\AutoPlay\下载缓存\<版本>` —— 测试完想清干净就把这两个删了。
"""

import argparse
import functools
import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, '曲库索引', 'payload.json')
CHUNK_DIR = os.path.join(HERE, '发布', '分片')
RELEASE_DIR = os.path.join(HERE, '发布')


def log(text):
    print(text, flush=True)


def rewrite_manifest(port):
    """把清单里的下载地址换成 127.0.0.1，返回 (改过的整段 json, 版本号)。"""
    with open(MANIFEST, encoding='utf-8') as handle:
        data = json.load(handle)
    base = 'http://127.0.0.1:%d' % port
    for info in (data.get('editions') or {}).values():
        info['base_urls'] = [base]
    return json.dumps(data, ensure_ascii=False, indent=2), str(data.get('app_version') or '')


class Handler(http.server.SimpleHTTPRequestHandler):
    """`/payload.json` 给内存里那份改过的清单，别的都从 发布\\分片\\ 拿。"""

    def __init__(self, *args, manifest_text='', **kwargs):
        self.manifest_text = manifest_text
        super().__init__(*args, directory=CHUNK_DIR, **kwargs)

    def log_message(self, fmt, *args):
        log('    · %s' % (fmt % args))

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == '/payload.json':
            blob = self.manifest_text.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(blob)))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            self.wfile.write(blob)
            return
        return super().do_GET()


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地测在线安装程序')
    parser.add_argument('--port', type=int, default=8123)
    parser.add_argument('--exe', default='', help='要开哪个 exe，默认「发布\\在线安装程序」')
    parser.add_argument('--no-launch', action='store_true', help='只起服务，不开 exe')
    args = parser.parse_args(argv)

    for path in (MANIFEST, CHUNK_DIR):
        if not os.path.exists(path):
            log('[x] 没有 %s —— 先跑 python -X utf8 make_parts.py' % path)
            return 1
    text, version = rewrite_manifest(args.port)
    httpd = http.server.ThreadingHTTPServer(
        ('127.0.0.1', args.port), functools.partial(Handler, manifest_text=text))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log('· 本地服务起好了：http://127.0.0.1:%d/payload.json（切片在 发布\\分片）' % args.port)
    log('· 清单版本：%s' % version)

    if args.no_launch:
        log('· 自己开那个 exe 也行 —— 记得先设环境变量：')
        log('    $env:AUTOPLAY_PAYLOAD_INDEX = "http://127.0.0.1:%d/payload.json"' % args.port)
        input('按回车结束（会关掉本地服务）…')
        return 0

    exe = args.exe or os.path.join(RELEASE_DIR, 'AutoPlay 在线安装程序 v%s.exe' % version)
    if not os.path.isfile(exe):
        log('[x] 没找到 %s —— 先跑 python -X utf8 make_installer.py online' % exe)
        return 1
    env = dict(os.environ)
    env['AUTOPLAY_PAYLOAD_INDEX'] = 'http://127.0.0.1:%d/payload.json' % args.port
    log('· 打开：%s' % exe)
    log('· 装完 / 测完，直接关掉那个窗口就行（本地服务跟着这个脚本一起退）')
    subprocess.run([exe], env=env)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
