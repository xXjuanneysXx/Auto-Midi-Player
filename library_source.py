# -*- coding: utf-8 -*-
r"""
联网曲库的默认地址（打包时由「设置联网曲库并重新打包.bat」写进来）
=================================================================

空的 = 没配过：程序里的「联网曲库」会提示还没有地址，本地曲库照常能用。

要填的格式（任选一个，推荐上面那个，国内更稳）：

    https://cdn.jsdelivr.net/gh/<用户名>/<仓库>@<分支>/library.json
    https://raw.githubusercontent.com/<用户名>/<仓库>/<分支>/library.json

也可以不改这里，直接在 %LOCALAPPDATA%\AutoPlay\library\source.txt 里写一行地址
（改完重启就生效，不用重新打包）。
"""

INDEX_URL = 'https://raw.githubusercontent.com/xXjuanneysXx/midi-music/main/library.json'
