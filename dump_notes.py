# -*- coding: utf-8 -*-
"""
调试用：把 midi 里的音符一个个列出来（什么时候响、响了多久、多高、多响）。

    python dump_notes.py 1.mid            # 自动挑主音轨
    python dump_notes.py 1.mid 2          # 指定第 2 条音轨
    python dump_notes.py 1.mid 2 40       # 只看前 40 个音
"""

import sys

import midi_analyze


def setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def main(argv):
    if not argv:
        print('用法：python dump_notes.py <midi 文件> [音轨序号] [最多显示几个]')
        return 1
    path = argv[0]
    midi = midi_analyze.load(path)
    tracks = midi_analyze.describe_tracks(midi)
    if len(argv) > 1:
        index = int(argv[1])
        reason = '手动指定'
    else:
        index, reason = midi_analyze.select_melody_track(tracks)
    limit = int(argv[2]) if len(argv) > 2 else 0

    notes = midi_analyze.extract_notes(midi, midi.tracks[index])
    print('文件 %s  tempo 轨 %d  tpb %d' % (path, len(midi.tracks), midi.ticks_per_beat))
    print('音轨 [%d] %s（%s）  音符 %d 个' % (index, tracks[index].name or '(无名)', reason, len(notes)))
    print('单音：%s%s' % (midi_analyze.is_monophonic(notes),
                        '' if midi_analyze.is_monophonic(notes) else '，重叠 %d 处' % midi_analyze.count_overlaps(notes)))
    print()
    print('%4s %9s %9s %9s %6s %6s %5s' % ('#', '开始(s)', '结束(s)', '时值(s)', '音名', 'MIDI', '力度'))
    print('-' * 58)
    for i, note in enumerate(notes[:limit] if limit else notes):
        print('%4d %9.3f %9.3f %9.3f %6s %6d %5d' % (
            i, note.start, note.end, note.duration,
            midi_analyze.pitch_name(note.pitch), note.pitch, note.velocity))
    return 0


if __name__ == '__main__':
    setup_console()
    sys.exit(main(sys.argv[1:]))
