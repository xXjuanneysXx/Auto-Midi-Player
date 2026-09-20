# -*- coding: utf-8 -*-
r"""
上传口令的示例文件（给拿到源码的人看的）
=======================================

想自己用上传中转，就把这个文件复制成 `relay_key_local.py`，把下面那行填成
你在 Cloudflare 上给 Worker 设的 `UPLOAD_KEY`。

`relay_key_local.py` 被 .gitignore 挡着，**不要提交**。
中转怎么搭：见 `中转上传\部署说明.md`。
"""

RELAY_KEY = ''