# -*- coding: utf-8 -*-
"""
AutoPlay 卸载程序
=================

安装程序把这份代码打成的 `uninstall.exe` 放进安装目录，旁边再写一份
`uninstall.json`（记着装到哪、删哪几个快捷方式、要不要顺带取消 .mproj 关联、
「应用和功能」里那项写在哪）。用户从「设置 → 应用」里点卸载，或者双击目录里的
uninstall.exe，都会走到这儿。

干的事跟以前那份 `卸载.bat` 一样：

    弹一句确认  ->  结束正在跑的 AutoPlay  ->  删快捷方式
    ->  （装的时候关联过才）取消 .mproj 关联  ->  删「应用和功能」里的表项
    ->  删掉整个安装目录

自己正跑在要删的目录里，最后一步交给一个延后的 `cmd`：先 ping 两下拖时间（那会儿
本进程早退出了），再整目录删掉 —— 不用像以前那样先把自己复制到 %TEMP% 再跑，
两个版本同时装也不会抢同一个临时文件。

默认装在 `%LOCALAPPDATA%` 时不需要管理员权限。只有「程序还在跑」（它本身是提权
运行的，不提权杀不掉）或者目录删不动（装在 Program Files）才会用「以管理员身份
运行」把自己再叫起来一次，提权后的自己带 `--elevated --yes`，不会再问第二遍。

只删程序本体和快捷方式；`%LOCALAPPDATA%\\AutoPlay` 下的设置 / 日志 / 录制 / 谱面
一律不动 —— 重新装回来还认得你。

打包：`make_installer.py` 里 `--onefile --windowed`，纯标准库，不拖 Qt。
"""

import ctypes
import json
import os
import subprocess
import sys

CONFIG_NAME = 'uninstall.json'
APP_EXE_DEFAULT = 'AutoPlay.exe'
TITLE_DEFAULT = 'AutoPlay'
UNINSTALL_KEY_DEFAULT = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPlay'

CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
DETACHED_PROCESS = getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)

MB_YESNO = 0x04
MB_ICONQUESTION = 0x20
MB_ICONINFORMATION = 0x40
MB_SETFOREGROUND = 0x10000
IDYES = 6


def message(text, flags=MB_ICONINFORMATION):
    """系统对话框：卸载程序不带 Qt，界面就靠它。"""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, '卸载 AutoPlay',
                                         flags | MB_SETFOREGROUND)
    except Exception:
        pass


def ask(text):
    """弹一句「是 / 否」，点了「是」返回 True。"""
    try:
        answer = ctypes.windll.user32.MessageBoxW(
            None, text, '卸载 AutoPlay',
            MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND)
    except Exception:
        return True
    return answer == IDYES


def is_elevated():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated():
    """用「以管理员身份运行」把自己再叫起来一次（提权后的自己带 --elevated）。"""
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', sys.executable, '--elevated --yes', None, 1)
    except Exception:
        return False
    return int(result) > 32                 # >32 才算叫起来了（<=32 是错误码）


def where_am_i():
    """本 exe 所在目录（PyInstaller 单文件拿到的也是 exe 的真实位置）。"""
    return os.path.dirname(os.path.abspath(sys.executable))


def read_config(folder):
    try:
        with open(os.path.join(folder, CONFIG_NAME), encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def app_running(name):
    """
    tasklist 看一眼这个进程在不在（不引第三方库）。

    输出按字节比，不解码：中文 Windows 的 tasklist 吐的是本地代码页，硬按 UTF-8 解
    会炸在线程里（这里踩过一次）。
    """
    try:
        result = subprocess.run(
            ['tasklist', '/fi', 'imagename eq %s' % name, '/fo', 'csv', '/nh'],
            creationflags=CREATE_NO_WINDOW, capture_output=True)
    except OSError:
        return False
    return name.lower().encode('ascii', 'ignore') in (result.stdout or b'').lower()


def must_elevate(folder, exe_name):
    """默认装在 %LOCALAPPDATA% 时不用提权；装在 Program Files 或者程序还在跑就得提权。"""
    if is_elevated():
        return False
    if app_running(exe_name):
        return True
    probe = os.path.join(folder, '.autoplay-uninstall-test')
    try:
        with open(probe, 'w') as handle:
            handle.write('ok')
        os.remove(probe)
        return False
    except OSError:
        return True


def kill_app(name):
    try:
        subprocess.run(['taskkill', '/f', '/im', name], creationflags=CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def delete_shortcuts(links):
    for link in links or ():
        try:
            os.remove(link)
        except OSError:
            pass


def unregister_assoc():
    """取消 .mproj 关联 —— 跟程序里那个勾选框共用同一份 fileassoc 逻辑。"""
    try:
        import fileassoc
    except ImportError:
        return
    try:
        fileassoc.unregister()
    except Exception:
        pass


def delete_registry(key_path):
    """删掉「应用和功能」里那一项（值连键一起）。"""
    import winreg

    def drop(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                while True:
                    try:
                        sub = winreg.EnumKey(key, 0)
                    except OSError:
                        break
                    drop(path + '\\' + sub)
        except OSError:
            return
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    drop(key_path)


def schedule_delete(folder):
    """
    自己就在这个目录里，直接删删不掉正在运行的 exe。

    交给一个延后的 cmd：ping 两下拖着时间（本进程这时已经退出了），再整目录删掉；
    没删干净的话隔两秒再补一刀（有时任务管理器还没把句柄放开）。
    """
    step = 'ping -n 3 127.0.0.1 >nul & rd /s /q "%s" >nul 2>&1' % folder
    try:
        subprocess.Popen('cmd /c %s & %s' % (step, step), shell=True,
                         creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
                         close_fds=True)
    except OSError:
        pass


def main(argv):
    folder = where_am_i()
    config = read_config(folder)
    if config is None:
        message('这个目录里没有卸载配置（%s），不像是正常的安装目录。\n\n'
                '想卸载的话，直接把这个目录删掉就行：\n%s' % (CONFIG_NAME, folder))
        return 1

    title = config.get('title') or TITLE_DEFAULT
    exe_name = config.get('exe') or APP_EXE_DEFAULT
    elevated = '--elevated' in argv
    if not ('--yes' in argv) and not ask(
            '要从这台电脑上卸载「%s」吗？\n\n'
            '· 程序目录：%s\n'
            '· 桌面 / 开始菜单里的快捷方式会一起删掉\n'
            '· 你的设置、日志、录制和谱面（在 %%LOCALAPPDATA%%\\AutoPlay 里）保留'
            % (title, folder)):
        return 0
    if not elevated and must_elevate(folder, exe_name):
        if relaunch_elevated():
            return 0
        message('卸载要结束正在运行的 AutoPlay，或者往 Program Files 里删东西，\n'
                '需要管理员权限。\n\n'
                '可以右键 uninstall.exe 选「以管理员身份运行」再来一次；\n'
                '或者先关掉 AutoPlay 再试。')
        return 1

    kill_app(exe_name)
    delete_shortcuts(config.get('shortcuts'))
    if config.get('assoc'):
        unregister_assoc()
    delete_registry(config.get('key') or UNINSTALL_KEY_DEFAULT)
    schedule_delete(folder)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
