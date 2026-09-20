# -*- coding: utf-8 -*-
"""
.mproj 工程文件关联：双击工程文件直接开本程序
============================================

干什么用的
----------
在「简谱编辑器」里存的工程文件（.mproj）以前只能先开程序再「打开工程」。关联好
之后双击它就行：程序被叫起来，并且直接落在编辑器那一页、打开这个工程。

怎么关联
--------
全部写在 HKCU\\Software\\Classes 下面（**当前用户**，不需要管理员权限）：

    HKCU\\Software\\Classes\\.mproj                    默认值 = AutoPlay.mproj
    HKCU\\Software\\Classes\\.mproj\\OpenWithProgids     AutoPlay.mproj
    HKCU\\Software\\Classes\\AutoPlay.mproj              默认值 = 简谱工程文件
        DefaultIcon           "<exe>",0
        shell\\open\\command    "<exe>" "%1"

改完要喊一声 SHChangeNotify，不然资源管理器还按老样子显示图标，「打开方式」
里也看不到本程序。

装 / 卸
-------
安装程序里勾了「关联工程文件」就会调 register()；卸载时那份 bat 里会调
`reg delete` 把同样的位置删掉（见 installer.make_uninstaller）。程序里那个
「关联 .mproj 工程文件」勾选框也是调这儿。
"""

import ctypes
import os

try:
    import winreg
except ImportError:                               # pragma: no cover
    winreg = None


SUFFIX = '.mproj'
PROGID = 'AutoPlay.mproj'
TYPE_NAME = '简谱工程文件'
SUFFIX_KEY = r'Software\Classes\.mproj'
PROGID_KEY = r'Software\Classes' + '\\' + PROGID
OPEN_KEY = PROGID_KEY + r'\shell\open\command'
ICON_KEY = PROGID_KEY + r'\DefaultIcon'
OPEN_WITH_KEY = SUFFIX_KEY + r'\OpenWithProgids'
# 安装程序卸载时那份 bat 里要删的东西，跟这儿保持一致
UNINSTALL_BAT_LINES = (
    'reg delete "HKCU\\%s" /f >nul 2>&1' % PROGID_KEY,
    'reg delete "HKCU\\%s" /v "%s" /f >nul 2>&1' % (OPEN_WITH_KEY, PROGID),
    'reg query "HKCU\\%s" /ve 2>nul | findstr /i "%s" >nul && '
    'reg delete "HKCU\\%s" /f >nul 2>&1' % (SUFFIX_KEY, PROGID, SUFFIX_KEY),
)


def available():
    """能不能改注册表（非 Windows 或者没有 winreg 就不能）。"""
    return winreg is not None


def exe_of(module_file=None):
    """
    该拿哪个 exe 去关联。

    打包后就是自己那个 exe；源码运行时返回空字符串 —— 拿 python.exe 去关联
    .mproj 只会把工程文件跟解释器绑在一起，那是坑，宁可不关联。
    """
    import sys
    if getattr(sys, 'frozen', False):
        return os.path.abspath(sys.executable)
    return ''


def _set(key_path, name, value, kind=None):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ if kind is None else kind, value)


def _get(key_path, name=''):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, name)
            return value
    except OSError:
        return None


def _delete_value(key_path, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def _delete_tree(key_path):
    """删掉一个键连同它下面的所有东西（不存在就算了）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(key_path + '\\' + sub)
    except OSError:
        return
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError:
        pass


def notify_shell():
    """告诉资源管理器「文件关联变了」，不然图标和「打开方式」还是旧的。"""
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:                             # pragma: no cover
        pass


def register(exe=None, notify=True):
    """把 .mproj 关联到 exe（不传就用自己），返回实际用的 exe。"""
    if winreg is None:
        raise RuntimeError('这个系统没有 winreg，改不了文件关联')
    target = os.path.abspath(exe or exe_of())
    if not target or not os.path.isfile(target):
        raise RuntimeError('程序本体不在，没法关联：%s' % (target or '(没找到)'))
    _set(SUFFIX_KEY, '', PROGID)
    _set(OPEN_WITH_KEY, PROGID, b'', winreg.REG_NONE)
    _set(PROGID_KEY, '', TYPE_NAME)
    _set(ICON_KEY, '', '"%s",0' % target)
    _set(OPEN_KEY, '', '"%s" "%%1"' % target)
    if notify:
        notify_shell()
    return target


def unregister(notify=True):
    """取消关联（只删自己的那几项，不动别人的）。"""
    if winreg is None:
        return False
    _delete_tree(PROGID_KEY)
    _delete_value(OPEN_WITH_KEY, PROGID)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, OPEN_WITH_KEY)
    except OSError:
        pass
    # 默认值是我们写的才清掉：用户后来自己改关联到别的程序，那就别动
    if str(_get(SUFFIX_KEY, '') or '').lower() == PROGID.lower():
        _delete_value(SUFFIX_KEY, '')
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, SUFFIX_KEY)
    except OSError:
        pass
    if notify:
        notify_shell()
    return True


def registered_exe():
    """现在 .mproj 关联到哪个 exe（没关联返回空字符串）。"""
    if winreg is None:
        return ''
    command = _get(OPEN_KEY, '')
    if not command:
        return ''
    command = str(command).strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 1 else ''
    return command.split(' ')[0]


def is_registered(exe=None):
    """关联没关联（传 exe 就顺便看看是不是关联到这个 exe）。"""
    current = registered_exe()
    if not current:
        return False
    if not exe:
        return True
    return os.path.normcase(os.path.abspath(current)) == os.path.normcase(os.path.abspath(exe))