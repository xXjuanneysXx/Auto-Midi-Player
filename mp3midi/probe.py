# -*- coding: utf-8 -*-
"""看看本机装了哪些「音频转 MIDI」后端。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audio2midi


def main():
    print('音频转 MIDI 后端：')
    for name, ok in audio2midi.describe_backends().items():
        print('  %-12s %s' % (name, '可以用' if ok else '没装'))
    print()
    print('装哪个后端（国内用清华镜像快很多）：')
    print('  # 推荐：有伴奏 / 编曲也能转（ONNX 推理，不需要 TensorFlow）')
    print('  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-deps basic-pitch')
    print('  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \\')
    print('      librosa pretty_midi mir_eval resampy scikit-learn onnxruntime')
    print('  # 只转独奏 / 清唱：装 librosa 就够了（自动用上 pyin）')
    print('  pip install -i https://pypi.tuna.tsinghua.edu.cn/simple librosa')
    return 0


if __name__ == '__main__':
    sys.exit(main())
