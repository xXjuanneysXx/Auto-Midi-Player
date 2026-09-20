# 音频（mp3）转单音 MIDI

游戏里弹琴只要一条主旋律线 —— 这里的东西就是把 mp3 / wav 这类音频变成 MIDI：

* 第一条音轨 `melody lead`：**主旋律，单音、绝不重叠**（主程序默认弹这条）；
* 音轨 `melody2 loud` / `melody3 high`：两条原始候选（跟融合结果不一样时才写），
  觉得默认那条不对就在主程序的下拉框里换它；
* 最后一条 `full all-notes`：**完整转谱**（多音同时，人耳听着就是原曲的简化版），
  留着核对 / 丢进 DAW 改。

    python audio2midi.py 歌.mp3        # 生成同名的 歌.mid
    python probe.py                    # 看看本机装了哪些后端

主程序里也带着这个功能：界面上点「音频转 MIDI…」，或者在文件列表里双击蓝色的音频文件。

## 后端：用哪个算法认主旋律

默认 `auto` —— 装了哪个厉害的就用哪个：

| 后端 | 是什么 | 适合 | 装多大 |
| --- | --- | --- | --- |
| `basic-pitch`（推荐） | Spotify 开源的**多声部转写**模型，ONNX 推理 | 有伴奏、有编曲的歌也能抓出主旋律 | 约 50 MB（onnxruntime + 几个纯 Python 依赖） |
| `pyin` | librosa 的逐帧基频估计 | 独奏、清唱、单声部 | 约 100 MB |
| `yin`（兜底） | 本仓库自己实现的 YIN | 只有它在的时候用，单声部才准 | 0（只用 numpy） |

装 basic-pitch：**不需要 TensorFlow**。3.12 上 pip 解析不了它那条 TF 依赖，所以分两步装
（国内加清华镜像快很多）：

    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --no-deps basic-pitch
    pip install -i https://pypi.tuna.tsinghua.edu.cn/simple librosa pretty_midi mir_eval resampy scikit-learn onnxruntime

打包好的 `AutoPlay.exe`（v1.3 起）里已经带上了这一整套，不用自己装 —— 代价是 exe 会胖到
400 MB 左右。从源码跑 `python main.py` 才需要按上面这两行装。

### 直接 `pip install basic-pitch` 会报错（不是你环境的问题）

basic-pitch 0.4.0 的包里写着这么一条依赖：

    tensorflow<2.15.1,>=2.4.1; platform_system != "Darwin" and python_version >= "3.11"

Windows + Python 3.12 会命中它，可是 TensorFlow 2.15 根本不支持 3.12（2.16 才支持），
pip 装不上就**一路往回退版本**（0.3.3 → 0.2.6），那些老版本又要 `numpy<1.24`，3.12 上只能
源码编译 numpy 1.23.5，编译环境里的 setuptools 又太老，最后炸在
`AttributeError: module 'pkgutil' has no attribute 'ImpImporter'`。看着吓人，其实跟你的环境无关，
是 basic-pitch 自己的打包疏忽（同样的原因，它连 Windows 上的 `onnxruntime` 都没声明）。

所以**别再跑不带 `--no-deps` 的那条**。装上之后想确认一下，跑：

    python mp3midi\probe.py

三个后端都显示「可以用」就对了（`import basic_pitch` 时会打印三行 TensorFlow / CoreML /
tflite 没装的 WARNING，那是它在说「我要走 ONNX 那条路」，属于正常现象）。

为什么推荐它：

* 它是**训练过的转写模型**（钢琴、吉他、人声、一堆乐器一起响也行），不是「先砍频段再猜
  基频」。老版本那个「只留 200~2000 Hz 再估基频」的做法会把低音和镲直接砍没，默认已经
  不用了（只在兜底 YIN 上还留着 `--melody-focus` 开关）。
* 转写出来是多声部的，程序再**融合**出一条单音主旋律：先比响度分档，同一档里再比音高。
  单用「最响」会被伴奏里更响的和弦带跑，单用「最高」又会被模型偶尔冒出来的假音带走
  （又长又高、力度却不大 —— 有一首歌 22 秒的旋律就这么被吃掉了）。分档以后明显更响的
  赢、响度差不多的听音高的，两个毛病一起躲开。分档粒度是 `FUSE_LOUD_STEP`（默认 0.10）。
* 完整转谱一起写进同一个 .mid，不满意可以在主程序里换音轨，不用重新转。

### Melodia（真正的旋律提取）

经典算法（Salamon & Gómez），要装两样：

1. Sonic Annotator（命令行，Windows 有安装包）：
   https://github.com/sonic-visualiser/sonic-annotator/releases
2. Melodia vamp 插件：https://www.upf.edu/web/mtg/melodia
   把下载到的 `melodia-vamp-plugin.dll` 放进 Sonic Annotator 的 `vamp` 目录
   （一般在 `C:\Program Files\Sonic Visualiser\vamp\`）。

跑：

    sonic-annotator -l                                     # 先确认插件认出来了
    sonic-annotator -d vamp:melodia:melodia:melody "歌.mp3" -w csv --csv-basedir out

出来的 csv 是逐帧音高（Hz），还要再切成音符写成 MIDI。本程序没接这条路，
想接的话把 csv 读进来喂给 `audio2midi.notes_from_f0()` 就行。

### 不想敲命令

- **Sonic Visualiser**（免费）+ Melodia 插件：能看谱、能导出。
- **AnthemScore**、**Widi**、**Melodyne**：商业软件，质量更好但要钱。

## 已知的坑

- 鼓点、贝斯特别重的歌，装上 `demucs` 先把人声 / 主奏分出来再转会更干净（可选，不装也能用）。
- 跑调、忽快忽慢的现场版，转出来也不会好看。
- 只把音高对齐到半音，**不动节奏**（不量化），所以时值跟原唱一致。
- 转出来的音数一般比原曲「听起来」多不少（伴奏、装饰音都会被记下来）：手动挑主旋律是
  研究界的老大难问题，这个程序用的是「响度分档 + 音高」这个朴素规则，所以务必先按
  「♪ 试听」听一遍，不对就在音轨下拉框里换另一条（`melody2 loud` / `melody3 high`）。

## 文件

- `audio2midi.py`              转换本体，也能当命令行用
- `probe.py`                   看看本机有哪些后端
- `requirements-optional.txt`  可选后端的安装清单
- `测试/`                      自己合成的一段旋律（wav + mp3），拿来试功能

## 用法速查

    python audio2midi.py 歌.mp3                        # 主旋律 + 备用旋律（+ 完整转谱）
    python audio2midi.py 歌.mp3 --full                 # 连「完整转谱」那条轨一起写
    python audio2midi.py 歌.mp3 -o 输出.mid --min-note 0.12
    python audio2midi.py 歌.mp3 --backend yin          # 兜底算法（单声部音频）
    python audio2midi.py 歌.mp3 --melody-focus         # 兜底 yin 才有效：先截到 200~2000 Hz
