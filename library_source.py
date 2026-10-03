# -*- coding: utf-8 -*-
r"""
联网曲库的默认地址（打包时由「设置联网曲库并重新打包.bat」写进来）
=================================================================

空的 = 没配过：程序里的「联网曲库」会提示还没有地址，本地曲库照常能用。

程序里有两套曲库，界面上能一键切换，默认走**国内（Gitee）**：

    GITEE_INDEX_URL    国内曲库（默认）
    INDEX_URL          GitHub 曲库（备用；国内直连 raw 经常连不上，会试 jsDelivr 镜像）

要填的格式：

    https://gitee.com/<用户名>/<仓库>/raw/<分支>/library.json
    https://cdn.jsdelivr.net/gh/<用户名>/<仓库>@<分支>/library.json
    https://raw.githubusercontent.com/<用户名>/<仓库>/<分支>/library.json

也可以不改这里，直接在 %LOCALAPPDATA%\AutoPlay\library\source.txt 里写一行地址
（改完重启就生效，不用重新打包），或者在界面上切换曲库。
"""

# GitHub 曲库（备用源）
INDEX_URL = 'https://raw.githubusercontent.com/xXjuanneysXx/midi-music/main/library.json'

# 国内曲库（Gitee）—— 默认用这个
GITEE_INDEX_URL = 'https://gitee.com/juanneys/midi-music/raw/master/library.json'