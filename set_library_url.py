# -*- coding: utf-8 -*-
"""
设置联网曲库地址并重新打包
==========================

用法（在项目目录下）：

    python -X utf8 set_library_url.py https://cdn.jsdelivr.net/gh/你/仓库@main/library.json
    python -X utf8 set_library_url.py --clear          # 清掉，退回「没配地址」的状态
    python -X utf8 set_library_url.py <地址> --no-build  # 只写地址，不重新打包

它会做三件事：

1. 试着拉一下这个地址，告诉你通不通、里面有几首歌（拉不通也会问你要不要照样写进去）；
2. 把地址写进 `library_source.py` 的 INDEX_URL（打包时带进程序里的默认值）；
3. 跑 `make_installer.py all`，把两个安装包重新打出来（就是「发布」文件夹里那两个）。

不想重新打包、只想先用用看：程序里也能改 —— 往
`%LOCALAPPDATA%\\AutoPlay\\library\\source.txt` 写一行地址，重启就生效。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_PY = os.path.join(HERE, 'library_source.py')
TEMPLATE = '''# -*- coding: utf-8 -*-
r"""
联网曲库的默认地址（打包时由「设置联网曲库并重新打包.bat」写进来）
=================================================================

空的 = 没配过：程序里的「联网曲库」会提示还没有地址，本地曲库照常能用。

要填的格式（任选一个，推荐上面那个，国内更稳）：

    https://cdn.jsdelivr.net/gh/<用户名>/<仓库>@<分支>/library.json
    https://raw.githubusercontent.com/<用户名>/<仓库>/<分支>/library.json

也可以不改这里，直接在 %LOCALAPPDATA%\\AutoPlay\\library\\source.txt 里写一行地址
（改完重启就生效，不用重新打包）。
"""

INDEX_URL = __URL__
'''


def write_url(url):
    """把地址写进 library_source.py。"""
    with open(SOURCE_PY, 'w', encoding='utf-8', newline='') as handle:
        handle.write(TEMPLATE.replace('__URL__', repr(url or '')))
    return SOURCE_PY


def probe(url):
    """试着拉一下索引，看看通不通、有几首歌。"""
    sys.path.insert(0, HERE)
    try:
        import library
    except Exception as exc:
        return None, '导入 library.py 失败：%s' % exc
    songs, why = library.fetch_index(url)
    return songs, why


def main(argv):
    args = [arg for arg in argv[1:] if not arg.startswith('--')]
    flags = {arg for arg in argv[1:] if arg.startswith('--')}
    url = (args[0] if args else '').strip()

    if '--clear' in flags:
        path = write_url('')
        print('已清掉联网曲库地址：%s' % path)
    elif not url:
        print(__doc__)
        print('没给地址。用法：python -X utf8 set_library_url.py <library.json 的地址>')
        return 1
    else:
        if not url.lower().startswith(('http://', 'https://')):
            print('这不像个网址：%s' % url)
            print('应该形如 https://cdn.jsdelivr.net/gh/你/仓库@main/library.json')
            return 1
        print('先试一下这个地址通不通…')
        songs, why = probe(url)
        if songs:
            print('通了：索引里有 %d 首歌' % len(songs))
            for song in songs[:8]:
                print('   - %s%s' % (song.get('title', ''),
                                     ('　/ %s' % song['artist']) if song.get('artist') else ''))
            if len(songs) > 8:
                print('   … 还有 %d 首' % (len(songs) - 8))
        else:
            print('没拉通：%s' % why)
            print('（地址照样会写进去 —— 也许只是这台机器连不上，或者你还没把仓库建好。）')
            if '--yes' not in flags:
                answer = input('照样写进去并打包？[y/N] ').strip().lower()
                if answer not in ('y', 'yes'):
                    print('那就先不动。')
                    return 1
        path = write_url(url)
        print('地址写好了：%s' % path)

    if '--no-build' in flags:
        print('--no-build：不重新打包。要打包就跑 python -u -X utf8 make_installer.py all')
        return 0

    return build()


def build():
    """跑一遍打包（两个安装包）。给 set_relay_url.py 也留个入口，省得抄一遍。"""
    print('开始重新打包（几分钟，中间别关窗口）…')
    os.chdir(HERE)
    code = os.system('python -u -X utf8 make_installer.py all')
    print('打包结束（退出码 %s）。两个安装包在「发布」文件夹里。' % code)
    return 0 if code == 0 else code


if __name__ == '__main__':
    sys.exit(main(sys.argv))