# -*- coding: utf-8 -*-
"""
版本开关：一份代码打两个安装包。

「完全版」什么都有；「精简版」只去掉两样：
    - 音频转 MIDI（mp3 等音频 -> 单音 midi），
    - 简谱编辑器（钢琴卷帘手动改谱）。

跟奏、简谱生成、自动演奏、试听、内置曲库这些基础功能两个版本都有。

源码里这里永远写着 False（= 完全版）。打包精简版时 make_installer.py 会临时把它
改成 True 再让 PyInstaller 打一版，打完立刻改回来 —— 这样只用维护一份代码。
"""

LITE = False


def has_audio():
    """有没有「音频转 MIDI」这一块。"""
    return not LITE


def has_editor():
    """有没有「简谱编辑器」这一块。"""
    return not LITE


def has_recorder():
    """
    有没有「录制」（F10 把你弹的东西记成谱面）。

    跟编辑器绑在一起：录完就是一份没修过的谱，没有编辑器就只能干看着，
    所以精简版里录制也一起去掉。
    """
    return not LITE


EDITION = '精简版' if LITE else '完全版'
