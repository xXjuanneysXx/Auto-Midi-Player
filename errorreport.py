# -*- coding: utf-8 -*-
r"""
出错自动上报
============

程序没接住的异常（闪退、转谱炸了、更新炸了……）会打包成一个 json，传到曲库仓库
（默认 Gitee 的 `juanneys/midi-music`）里 `错误报告\` 目录下，作者打开仓库就能看到
别人机器上到底炸在哪儿 —— 不用等用户来群里描述。

上报什么
--------

* 程序版本、完全版 / 精简版、Python 版本、Windows 版本、是不是管理员；
* 「错误代码」（内容哈希前 6 位）+ 异常类型 + traceback（最多 60 行）；
* **路径里有没有中文**（一个布尔值）—— 别的用户的安装目录 / 歌名带中文经常是
  各种怪问题的来源，这个信号很有用，但不上传路径本身。

**不上报**：用户名、完整路径、磁盘 / 机器标识这些能定位到个人的东西。traceback 里
的 `C:\Users\张三\...` 一律被换成 `<path>`，`<home>` 也是。

开关
----

默认开启（设置里有勾选框可以关）。一次运行最多传 3 条，同一条错误只传一次，免得
死循环崩溃把仓库刷屏。仓库根目录的 `error_report.json` 能远程改开关 / 上报目录，
拉不到就用内置默认值；用户自己的选择存在本机，优先级最高。

传输
----

走 Gitee / GitHub 的 contents API（`library.put_file`），令牌是内置的那个
（见 gitee_token_local.py）。整个发送在后台线程里做，连不上就安静地算了，
绝不挡界面、绝不再抛异常。
"""

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback

import library


# 一次运行最多传几条（防死循环崩溃刷屏）
MAX_PER_RUN = 3
# traceback 最多留多少行 / 多少字符（太长仓库不好看）
MAX_LINES = 60
MAX_CHARS = 8000

# 拉不到仓库里的 error_report.json 时用这套
DEFAULT_CONFIG = {
    'format': 'autoplay-error-report',
    'enabled': True,
    'site': 'gitee',
    'owner': 'juanneys',
    'repo': 'midi-music',
    'branch': 'master',
    'dir': '错误报告',
    'note': '',
}

CONFIG_NAME = 'error_report.json'

# main.py 启动时填进来（免得这里反过来 import main 造成循环导入）
_app = {
    'version': '',
    'lite': False,
    'dir': '',
}

_lock = threading.Lock()
_sent = set()
_count = 0


def set_app_info(version='', lite=False, app_dir=''):
    """把程序自己的信息告诉上报模块（main.py 启动时调一次）。"""
    _app['version'] = str(version or '')
    _app['lite'] = bool(lite)
    _app['dir'] = str(app_dir or '')


# ---------- 配置文件（远程 + 本机覆盖） ----------

def _local_appdata():
    return os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')


def _state_dir():
    return os.path.join(_local_appdata(), 'AutoPlay')


def _remote_path():
    """仓库里那份配置在本机的缓存。"""
    return os.path.join(_state_dir(), 'error_report_remote.json')


def _local_path():
    """用户自己在设置里选的开 / 关。"""
    return os.path.join(_state_dir(), 'error_report_local.json')


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def config():
    """远端配置 + 内置默认值（不含用户本机的开关）。"""
    merged = dict(DEFAULT_CONFIG)
    merged.update(_read_json(_remote_path()))
    return merged


def enabled():
    """现在到底上不上报：本机选择 > 远端配置 > 默认开。"""
    local = _read_json(_local_path())
    if 'enabled' in local:
        return bool(local['enabled'])
    return bool(config().get('enabled', True))


def set_enabled(on):
    """设置里那个勾选框改的就是它（写本机文件，优先级最高）。"""
    return _write_json(_local_path(), {'enabled': bool(on)})


def refresh(timeout=None):
    """
    联网把仓库里的 error_report.json 拉回来（后台线程调，别挡界面）。

    拉到了就存本机一份；拉不到保留上一份 / 内置默认值，返回 False。
    """
    try:
        local = config()
        site = str(local.get('site') or 'gitee')
        owner, repo, branch, why = _config_repo(site)
        if not owner or not repo:
            return False
        token = ''
        try:
            token = library.get_token(site)
        except Exception:
            token = ''
        url = library._contents_url(site, owner, repo, CONFIG_NAME, branch)
        kwargs = {'site': site}
        if timeout:
            kwargs['timeout'] = timeout
        data, _code, _why = library._api_raw(url, token, **kwargs)
        if not isinstance(data, dict):
            return False
        raw = str(data.get('content') or '')
        if not raw:
            return False
        text = base64.b64decode(raw).decode('utf-8', 'replace')
        got = json.loads(text)
        if not isinstance(got, dict):
            return False
        merged = dict(DEFAULT_CONFIG)
        merged.update(got)
        _write_json(_remote_path(), merged)
        return True
    except Exception:
        return False


def _config_repo(site):
    """仓库坐标：优先用曲库现在用的那套，认不出来就退回内置默认值。"""
    try:
        owner, repo, branch, why = library.source_repo(site)
        if owner and repo:
            return owner, repo, branch or 'master', why
    except Exception:
        pass
    local = DEFAULT_CONFIG
    return local['owner'], local['repo'], local['branch'], ''


# ---------- 脱敏 ----------

# 绝对路径：盘符开头。目录段允许中文 / 空格，最后文件名那段**不吃中文** ——
# 这样「读取 C:\a\b.mp3 失败」只会把路径换成 <path>，后面的「失败」还留着。
_WIN_PATH = re.compile(r'[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*[^\\/:*?"<>|\r\n\u4e00-\u9fff]*')
# 剩下的相对路径 / UNC（带反斜杠或斜杠的都算）
_REL_PATH = re.compile(r'(?:[\\/][^\\/\s:*?"<>|]+)+')
# 万一还有光秃秃的文件名（D:\歌\我的歌.mp3 那种整段中文，上面两刀没切干净）
_REL_FILE = re.compile(r'[\w\u4e00-\u9fff][\w\u4e00-\u9fff .\-]*\.'
                       r'(?:mid|midi|kar|rmi|mp3|wav|flac|ogg|m4a|aac|wma|opus|aiff|mproj)\b',
                       re.IGNORECASE)
_FILE_URL = re.compile(r'file:///[^\s"\']+')
_UNC = re.compile(r'\\\\[^\\\s]+\\[^\\\s]+')
# 只用来判断「路径里有没有中文」：这次要放宽，中文文件名也算进来
_WIN_PATH_LOOSE = re.compile(r'[A-Za-z]:\\(?:[^\\/:*?"<>|\r\n]*\\)*[^\\/:*?"<>|\r\n]*')


def _cjk(text):
    return any('\u4e00' <= ch <= '\u9fff' for ch in str(text or ''))


def sanitize(text):
    """把能定位到个人的路径换成占位符。"""
    text = str(text or '')
    home = os.path.expanduser('~')
    if home:
        text = text.replace(home, '<home>')
        text = text.replace(home.replace('\\', '/'), '<home>')
    text = _FILE_URL.sub('<path>', text)
    text = _WIN_PATH.sub('<path>', text)
    text = _UNC.sub(r'\\\\<host>\\<share>', text)
    text = _REL_PATH.sub('<path>', text)
    text = _REL_FILE.sub('<path>', text)
    text = re.sub(r'(?:<path>){2,}', '<path>', text)      # 一刀切了两段的，并成一个
    return text


def paths_have_chinese(text):
    """traceback 里出现的路径有没有中文（只回布尔值，不回路径本身）。"""
    text = str(text or '')
    try:
        for match in _WIN_PATH_LOOSE.finditer(text):
            if _cjk(match.group(0)):
                return True
        for match in _REL_PATH.finditer(text):
            if _cjk(match.group(0)):
                return True
    except Exception:
        return False
    return False


# ---------- 组装 ----------

def _os_text():
    try:
        return 'Windows %s (%s)' % (sys.getwindowsversion().major,
                                    sys.getwindowsversion().build)
    except Exception:
        return sys.platform


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def make_report(text, where='', note=''):
    """把一段 traceback / 一句话组装成要上传的 json。"""
    text = str(text or '')
    lines = text.strip().splitlines()
    short = '\n'.join(lines[-MAX_LINES:])[:MAX_CHARS]
    seed = hashlib.sha1((short + '|' + str(where)).encode('utf-8', 'replace')).hexdigest()
    app_dir = _app.get('dir') or ''
    return {
        'format': 'autoplay-error-report',
        'code': 'AP-' + seed[:6].upper(),
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'app_version': _app.get('version') or '',
        'edition': 'lite' if _app.get('lite') else 'full',
        'python': sys.version.split()[0],
        'os': _os_text(),
        'admin': _is_admin(),
        'where': str(where or '程序'),
        'note': str(note or ''),
        'install_path_chinese': _cjk(app_dir),
        'path_chinese': paths_have_chinese(text),
        'error': sanitize(short),
    }


def _upload(report):
    """后台线程里把报告传上去（只 Gitee / GitHub 的 contents API）。"""
    try:
        cfg = config()
        site = str(cfg.get('site') or 'gitee')
        owner = str(cfg.get('owner') or '')
        repo = str(cfg.get('repo') or '')
        branch = str(cfg.get('branch') or '')
        folder = str(cfg.get('dir') or '错误报告').strip('/')
        if not owner or not repo:
            return
        token = ''
        try:
            token = library.get_token(site)
        except Exception:
            token = ''
        if not token:
            return
        name = '%s-%s.json' % (time.strftime('%Y%m%d-%H%M%S'), str(report.get('code') or '')[3:])
        path = '%s/%s' % (folder, name)
        body = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8')
        library.put_file(site, owner, repo, path, body, token,
                         branch=branch, message='AutoPlay 错误报告 %s' % report.get('code', ''))
    except Exception:
        pass


def _log_local(report):
    """本机也留一份，万一没传上去还能让用户手动发。"""
    try:
        path = os.path.join(_state_dir(), '错误报告.log')
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write('\n===== %s %s =====\n%s\n'
                         % (report.get('time'), report.get('code'), report.get('error')))
    except Exception:
        pass


def send(text, where='', note=''):
    """把一条错误丢到后台去传（永远不抛、不挡界面）。返回有没有真的排队。"""
    try:
        if not enabled():
            return False
        report = make_report(text, where=where, note=note)
        seed = str(report.get('code') or '')
        with _lock:
            global _count
            if seed in _sent or _count >= MAX_PER_RUN:
                return False
            _sent.add(seed)
            _count += 1
        _log_local(report)
        threading.Thread(target=_upload, args=(report,), daemon=True).start()
        return True
    except Exception:
        return False


def send_exception(kind, value, tb, where='程序'):
    """给 sys.excepthook / threading.excepthook 用的快捷方式。"""
    try:
        text = ''.join(traceback.format_exception(kind, value, tb))
    except Exception:
        text = str(value)
    return send(text, where=where)
