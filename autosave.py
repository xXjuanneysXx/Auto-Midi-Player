# -*- coding: utf-8 -*-
r"""
自动保存：录制 / 编辑过的谱面，哪怕没按过「保存工程」，也悄悄留一份底。

存在哪儿
    %LOCALAPPDATA%\AutoPlay\autosave\
        index.json      记着每一份是谁、什么时候存的
        <钥匙>.mproj    谱面本体，就是「保存工程」那个格式（能直接被 load_project 读）

什么时候留底
    编辑器里的谱面处于「改过还没存」的时候就写，每几秒看一眼；内容没变就不重复写盘。

什么时候清掉
    存成工程 / 重新载入同一份谱面后（那一份的「没存」状态没了），
    或者程序正常退出时（main.MainWindow.quit_app 里清干净）。

程序被强杀 / 崩溃 / 断电 —— 没来得及清，下次启动 MainWindow 会把这份谱面摆回编辑器
（见 main.MainWindow._restore_autosave）。

刻意**不** import editor：精简版里没有 editor，这个模块两个版本都要能装上（见 AutoPlay.spec）。
"""

import hashlib
import json
import os
import threading
import time

ROOT = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'AutoPlay')
DIR = os.path.join(ROOT, 'autosave')
INDEX = os.path.join(DIR, 'index.json')

# 跟 editor.PROJECT_FORMAT / PROJECT_VERSION 写死成一样的：自动保存出来的文件
# 就是一个正经的工程文件，双击能开、能直接喂给 editor.load_project。
FORMAT = 'autoplay-editor'
VERSION = 1

KEEP = 20                   # 最多留这么多份，多出来的按时间从旧的开始清
_lock = threading.Lock()    # 同一个进程里可能有好几个编辑器窗口在写


# ---------- 小工具 ----------

def key_of(score):
    """一份谱面用哪个名字存：拿来源路径算个短钥匙（同一份档案每次算出来都一样）。"""
    source = (getattr(score, 'path', '') or '').strip()
    seed = source.lower() if source else 'untitled'
    return hashlib.md5(seed.encode('utf-8', 'replace')).hexdigest()[:12]


def name_of(score):
    """给人看的名字：来源文件名，实在没有就叫「未命名的谱面」。"""
    source = getattr(score, 'path', '') or ''
    base = os.path.basename(source) if source else ''
    if not base:
        return '未命名的谱面'
    if (getattr(score, 'track_name', '') or '') == '录音':
        return '录音 %s' % base
    return base


def score_data(score):
    """谱面 -> json 结构（跟「保存工程」一模一样，另外多留一个 autosave 标记）。"""
    return {
        'format': FORMAT,
        'version': VERSION,
        'saved': time.strftime('%Y-%m-%d %H:%M:%S'),
        'tonic': int(score.tonic),
        'bpm': float(score.bpm),
        'source': getattr(score, 'path', '') or '',
        'track_index': int(getattr(score, 'track_index', -1)),
        'track_name': getattr(score, 'track_name', '') or '',
        'autosave': True,
        'notes': [[round(note.start, 4), round(note.dur, 4), int(note.pitch)]
                  for note in score.ordered()],
    }


def signature(score):
    """
    内容指纹。

    自动保存是「每几秒看一眼」的，不比对一下的话，一份改过没存的谱面会被反复
    重写（写一次几十毫秒，白费电）。指纹一样就直接跳过。
    """
    notes = tuple((round(note.start, 4), round(note.dur, 4), int(note.pitch))
                  for note in score.ordered())
    return hash((int(score.tonic), round(float(score.bpm), 3), notes))


def _remove_file(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _read_index():
    try:
        with open(INDEX, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(entries):
    os.makedirs(DIR, exist_ok=True)
    tmp = INDEX + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(entries, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)


def _prune(entries):
    """份数超了就清最旧的那些（连文件一起）。"""
    if len(entries) <= KEEP:
        return
    order = sorted(entries.values(), key=lambda item: item.get('saved', 0.0))
    for item in order[:-KEEP]:
        _remove_file(item.get('path') or os.path.join(DIR, str(item.get('key')) + '.mproj'))
        entries.pop(item.get('key'), None)


# ---------- 对外 ----------

def dump(score):
    """把这份谱面存进自动保存，返回存到哪儿了（写不进去就返回 None）。"""
    if score is None or not getattr(score, 'notes', None):
        return None
    key = key_of(score)
    with _lock:
        try:
            os.makedirs(DIR, exist_ok=True)
            path = os.path.join(DIR, key + '.mproj')
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(score_data(score), handle, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            entries = _read_index()
            entries[key] = {'key': key, 'path': path, 'name': name_of(score),
                            'source': getattr(score, 'path', '') or '',
                            'tonic': int(score.tonic), 'notes': len(score.notes),
                            'saved': time.time(),
                            'when': time.strftime('%Y-%m-%d %H:%M:%S')}
            _prune(entries)
            _write_index(entries)
        except (OSError, ValueError, TypeError):
            return None
    return path


def drop(key):
    """这一份不用留了（存成工程了 / 又载入了同一份 midi）：连文件带记录一起清。"""
    if not key:
        return False
    with _lock:
        entries = _read_index()
        item = entries.pop(key, None)
        if item is None:
            return False
        _remove_file(item.get('path') or os.path.join(DIR, key + '.mproj'))
        try:
            _write_index(entries)
        except OSError:
            pass
    return True


def clear():
    """全清（正常退出程序时用）。返回清了几个文件。"""
    with _lock:
        try:
            names = os.listdir(DIR)
        except OSError:
            return 0
        count = 0
        for name in names:
            if name.endswith('.mproj') or name.endswith('.tmp') or name == 'index.json':
                _remove_file(os.path.join(DIR, name))
                count += 1
        return count


def pending():
    """
    还没清掉的自动保存，新的排在前面。

    只认文件还在的那些记录（用户自己把 autosave 目录掏空了也不会炸）。
    """
    out = []
    with _lock:
        for item in _read_index().values():
            if not isinstance(item, dict):
                continue
            path = item.get('path') or os.path.join(DIR, str(item.get('key')) + '.mproj')
            if os.path.isfile(path):
                out.append(dict(item, path=path))
    out.sort(key=lambda item: item.get('saved', 0.0), reverse=True)
    return out