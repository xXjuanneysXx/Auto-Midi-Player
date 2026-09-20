# -*- coding: utf-8 -*-
"""
命令行转换：midi -> 简谱 txt

    python convert.py 1.mid
    python convert.py *.mid

输出文件名为  TONIC<tonic> <midi 文件名>.txt ，与 midi 文件放同一个目录。
多音轨文件用到的音轨不是第 0 条时，文件名里会补一个 #t<音轨号>。
"""

import os
import sys

import jianpu
import midi_analyze


def setup_console():
    """Windows 控制台默认 gbk，直接打印中文可能报错，这里改成 utf-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def convert(path, out_dir=None):
    """分析 midi 并写出简谱，返回 (Analysis, 谱面路径)。"""
    analysis = midi_analyze.analyze(path)
    pairs = midi_analyze.build_score(analysis.notes, analysis.tonic)
    score_path = jianpu.write_score(path, analysis.tonic, pairs, out_dir,
                                    track_index=analysis.track_index)
    return analysis, pairs, score_path


def main(argv):
    if not argv:
        print('用法：python convert.py <midi 文件> [更多 midi 文件...]')
        return 1
    failures = 0
    for path in argv:
        print('=' * 64)
        print('文件：%s' % path)
        if not os.path.isfile(path):
            print('找不到文件')
            failures += 1
            continue
        try:
            analysis, pairs, score_path = convert(path)
        except midi_analyze.MidiError as exc:
            print('失败：%s' % exc)
            failures += 1
            continue
        print(midi_analyze.format_report(analysis, score_path))
        preview = ' '.join('%s:%.3f' % (token, dur) for token, dur in pairs[:12])
        print('谱面开头：%s …' % preview)
    return 1 if failures else 0


if __name__ == '__main__':
    setup_console()
    sys.exit(main(sys.argv[1:]))
