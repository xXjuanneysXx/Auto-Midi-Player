# -*- coding: utf-8 -*-
r"""
上传口令（打包时带进程序）
=========================

对应中转那边的 UPLOAD_KEY。它的作用只是挡住「随手知道了地址就往里塞东西」的人，
**不是安全措施** —— 口令打进 exe 就一定能被抠出来（和程序里任何常量一样）。
好在它换不来你的 GitHub 令牌：令牌只存在 Cloudflare 那边。

这里是**空占位符**（公开源码里就该是空的）。想自己用上传中转：
把你在 Cloudflare 给 Worker 设的口令填到下面，然后重新打包。

`relay_key_local.py` 被 .gitignore 挡着，**不要提交**。示例见 relay_key_local.example.py。
中转怎么搭：见 `中转上传\部署说明.md`。
"""

RELAY_KEY = ''