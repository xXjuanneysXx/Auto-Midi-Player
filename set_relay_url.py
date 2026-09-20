# -*- coding: utf-8 -*-
"""
设置「上传中转」地址（和可选的口令）并重新打包
==============================================

用法（在项目目录下）：

    python -X utf8 set_relay_url.py https://xxx.workers.dev
    python -X utf8 set_relay_url.py https://xxx.workers.dev --key 你的口令
    python -X utf8 set_relay_url.py https://xxx.workers.dev --no-build      # 只写地址不打包
    python -X utf8 set_relay_url.py https://xxx.workers.dev --strip-token   # 顺手清掉内置令牌
    python -X utf8 set_relay_url.py --clear                                 # 清掉地址

它会做四件事：

1. 试着问一下这个中转（`GET /health`，带上你给的口令），告诉你通不通、里头曲库有几首；
2. 地址写进 `relay_source.py`（**这个文件进 git**，所以只放地址，不放密秘）；
3. 口令写进 `relay_key_local.py`（**这个文件不进 git**，和 `github_token_local.py`
   一个待遇：`.gitignore` 里已经写好了）；
4. `--strip-token` 的话，把 `github_token_local.py` 里的令牌清空 ——
   **打算把安装包公开分发就该这么做**：令牌待在服务器上，安装包里一行都没有，
   用户照样能上传。最后跑 `make_installer.py all` 重新打两个安装包。

不想重新打包、只想先试试：往 `%LOCALAPPDATA%\\AutoPlay\\library\\relay.txt`
写一行地址（口令写 `relay_key.txt`），重启程序即可。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import relay                                                # noqa: E402
import set_library_url                                      # noqa: E402  （借它的「重新打包」）

SOURCE_PY = os.path.join(HERE, 'relay_source.py')
TOKEN_PY = os.path.join(HERE, 'github_token_local.py')
# 口令是秘密：和令牌一样写进这个不进 git 的文件
KEY_PY = os.path.join(HERE, 'relay_key_local.py')

TEMPLATE = '''# -*- coding: utf-8 -*-
r"""
上传中转的默认地址（打包时由「设置上传中转并重新打包.bat」写进来）
=================================================================

空的 = 没配过：程序会退回去用内置令牌（`github_token_local.py`），
两个都没有的话，界面上只是提示一句「没配上传中转」，其它功能一概不受影响。

要填的格式（就是你部署完 Worker 拿到的那个地址，别带结尾的斜杠）：

    https://autoplay-library.你的账号.workers.dev

上传口令**不写在这里**（这个文件要进 git）：见同目录的 `relay_key_local.py`。

也可以不改这里，直接在 `%LOCALAPPDATA%\\AutoPlay\\library\\relay.txt` 里写一行
地址（改完重启就生效，不用重新打包）。

怎么搭：见 `中转上传\\部署说明.md`。
"""

RELAY_URL = __URL__

# 口令现在放在 relay_key_local.py（不进 git）；这儿留一个空的只为兼容
RELAY_KEY = ''
'''

KEY_TEMPLATE = '''# -*- coding: utf-8 -*-
r"""
上传口令（打包时带进程序）
=========================

对应中转那边的 `UPLOAD_KEY`。它的作用只是挡住「随手知道了地址就往里塞东西」的人，
**不是安全措施** —— 口令打进 exe 就一定能被抠出来（和程序里任何常量一样）。
好在它换不来你的 GitHub 令牌：令牌只存在 Cloudflare 那边。

**这个文件不进 git**（见 .gitignore），和 `github_token_local.py` 一个待遇。
要换口令：改这儿的 RELAY_KEY 再重新打包；或者往
`%LOCALAPPDATA%\\AutoPlay\\library\\relay_key.txt` 写一行（重启就生效，不用打包）。
"""

RELAY_KEY = __KEY__
'''


def write_source(url):
    """把中转地址写进 relay_source.py（这个文件进 git，所以只写地址）。"""
    text = TEMPLATE.replace('__URL__', repr(url or ''))
    with open(SOURCE_PY, 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)


def write_key(key):
    """把口令写进 relay_key_local.py（这个文件不进 git）。"""
    with open(KEY_PY, 'w', encoding='utf-8', newline='') as handle:
        handle.write(KEY_TEMPLATE.replace('__KEY__', repr(key or '')))


def strip_token():
    """把 github_token_local.py 里的令牌清空（只动 TOKEN 这一行）。"""
    if not os.path.isfile(TOKEN_PY):
        print('· 没有 github_token_local.py，跳过')
        return False
    with open(TOKEN_PY, 'r', encoding='utf-8') as handle:
        lines = handle.readlines()
    out = []
    hit = False
    for line in lines:
        if line.lstrip().startswith('TOKEN') and '=' in line:
            out.append("TOKEN = ''\n")
            hit = True
        else:
            out.append(line)
    if hit:
        with open(TOKEN_PY, 'w', encoding='utf-8', newline='') as handle:
            handle.writelines(out)
        print('· 内置令牌已清空（令牌只留在中转那边）')
    return hit


def ask(prompt, default=''):
    try:
        got = input(prompt).strip()
    except EOFError:
        got = ''
    return got or default


def main(argv):
    args = [a for a in argv[1:]]
    build = '--no-build' not in args
    strip = '--strip-token' in args
    args = [a for a in args if a not in ('--no-build', '--strip-token')]

    key = ''
    if '--key' in args:
        at = args.index('--key')
        if at + 1 < len(args):
            key = args[at + 1]
        del args[at:at + 2]

    url = relay.normalize(args[0]) if args else ''
    if not url and not args:
        print(__doc__)
        url = relay.normalize(ask('中转地址（直接回车 = 清掉）：'))
        if url and not key:
            key = ask('上传口令（没有就回车）：')

    if not url:
        write_source('')
        print('· 已清掉中转地址（程序会退回去用内置令牌 / 提示没配）')
        if build:
            return set_library_url.build()
        return 0

    # 先问一句「你活着没」，省得把打错的地址（或错的口令）打进包里。
    # 这里用显式地址 + 口令去问，不动本机配置。
    print('· 正在问中转：%s' % url)
    info, why = relay.ping(url=url, key=key)
    if info is None:
        print('  ✗ 没通：%s' % why)
        print('  （地址写错了？Worker 还没部署？Cloudflare 的 *.workers.dev 被墙？口令不对？）')
        if ask('  照样写进去并打包吗？[y/N] ', 'n').lower() not in ('y', 'yes', '是'):
            print('· 那就算了，什么都没改')
            return 1
    else:
        print('  ✓ %s' % str(info.get('told') or '通了'))

    write_source(url)
    print('· 地址已写进 relay_source.py（这个文件进 git，只有地址）')
    if key:
        write_key(key)
        print('· 口令已写进 relay_key_local.py（这个文件被 .gitignore 挡着，不进 git）')
    if strip:
        strip_token()
    if build:
        return set_library_url.build()
    print('· 没重新打包（--no-build）。程序里想立刻生效：')
    print('  往 %LOCALAPPDATA%\\AutoPlay\\library\\relay.txt 写一行地址，重启程序')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))