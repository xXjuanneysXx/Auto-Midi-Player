# -*- coding: utf-8 -*-
r"""
内置的 GitHub 令牌（想用「联网曲库 -> 上传」就填这儿）
=====================================================

「联网曲库 -> 上传 / 整理曲库…」要把曲子写进你的 GitHub 曲库仓库，靠的就是这个令牌。

默认是占位符 `GITHUB_PERSONAL_TOKEN`，等于**没填** —— 程序会把它当成「没有内置令牌」，
上传的时候提示一句；别的功能（下载曲库、演奏、编辑器……）一点不受影响。

怎么填
------
1. 去 GitHub 建一个令牌：Settings -> Developer settings -> Personal access tokens，
   勾上曲库仓库的 Contents 读写权限（classic 勾 repo 就行）；
2. 把下面的 GITHUB_PERSONAL_TOKEN 换成那串令牌，重新打包；
3. **别把填好的这个文件提交到公开仓库** —— 令牌一旦推到公开仓库，GitHub 的
   secret scanning 会直接把它作废（`.gitignore` 里有说明怎么写）；
4. 打包发给别人之前先把它改回占位符：令牌写死了就跟着安装包走，
   谁拿到包，谁就能往你的曲库里推东西。

换曲库仓库：见 library_source.py 里的 INDEX_URL。
"""

TOKEN = 'GITHUB_PERSONAL_TOKEN'
