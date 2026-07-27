# coding=utf-8
"""压缩包密码破解工具 · 原生 Windows 11 风格（Edge WebView2 + Fluent Design）"""
import os
import re
import sys
import json
import string
import shutil
import itertools
import time
import threading
import importlib
import subprocess
import multiprocessing
from collections import deque

APP_TITLE = "压缩包密码破解"
REQUIRED_MODULES = ["pyzipper", "py7zr", "rarfile"]
PIP_NAMES = {"pyzipper": "pyzipper", "py7zr": "py7zr", "rarfile": "rarfile"}

# 支持的压缩包格式（用于 UI 提示与文件选择过滤器）
SUPPORTED_EXTS = [".zip", ".7z", ".rar"]
FORMAT_NAMES = {"zip": "ZIP", "7z": "7Z", "rar": "RAR"}


def _cfg_dir():
    """配置/依赖目录：程序启动位置（exe 所在目录），便于随程序携带。"""
    d = _app_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _migrate_legacy_config():
    """迁移旧版配置（%APPDATA%/ZipCracker）到程序启动位置，并清理旧目录。"""
    try:
        import shutil
        legacy = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ZipCracker")
        if not os.path.isdir(legacy):
            return
        dest = _app_dir()
        os.makedirs(dest, exist_ok=True)
        # 迁移配置文件（目标不存在才复制，避免覆盖新配置）
        src_cfg = os.path.join(legacy, "config.json")
        if os.path.isfile(src_cfg) and not os.path.isfile(os.path.join(dest, "config.json")):
            try:
                shutil.copy2(src_cfg, os.path.join(dest, "config.json"))
            except Exception:
                pass
        # 迁移已下载的依赖（UnRAR.exe / bkcrack.exe）
        for name in ("UnRAR.exe", "bkcrack.exe"):
            s = os.path.join(legacy, name)
            if os.path.isfile(s) and not os.path.isfile(os.path.join(dest, name)):
                try:
                    shutil.copy2(s, os.path.join(dest, name))
                except Exception:
                    pass
        # 清理旧目录
        try:
            shutil.rmtree(legacy, ignore_errors=True)
        except Exception:
            pass
    except Exception:
        pass


def load_pref():
    try:
        with open(os.path.join(_cfg_dir(), "config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pref(data):
    try:
        with open(os.path.join(_cfg_dir(), "config.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _point_on_screen(x, y):
    """判断点 (x,y) 是否落在任一显示器内（用于校验记忆坐标是否偏出屏幕）。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        h = user32.MonitorFromPoint(wintypes.POINT(x, y), 0)  # MONITOR_DEFAULTTONULL
        return h != 0
    except Exception:
        return True


def _apply_saved_pos(key, w, h):
    """返回传给 create_window 的位置字典。
    - 有记录且坐标仍落在屏幕内：返回记忆坐标（复用上次位置）；
    - 否则返回 {}：不传 x/y，由 pywebview 自动 CenterScreen 居中。

    注：曾尝试用 SystemParametersInfoW 计算坐标传入，但在高 DPI 下 pywebview
    会把坐标再乘 scale 放置，结果错位到右下角；而「不传坐标」时 pywebview 自身
    会正确居中（用户此前认可的『能居中』行为）。故无记录时交回 pywebview 处理。"""
    try:
        pos = load_pref().get(key)
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            x, y = int(pos[0]), int(pos[1])
            # 仅当窗口中心点仍落在某显示器上时才复用记忆坐标，否则回退居中
            if _point_on_screen(x + w // 2, y + h // 2):
                return {"x": x, "y": y}
    except Exception:
        pass
    return {}


def _auto_center(win, title, w, h):
    """窗口显示后用 pywebview 自身的 win.move 精确居中（传逻辑像素，内部自动 ×scale）。

    关键修正：本机实际 DPI 缩放为 2.0，但 GetDpiForWindow 在窗口早期会返回 96（=1.0），
    导致居中坐标被算小一倍、再被 win.move 放大后甩到右下角。因此**不依赖 DPI 探测**，
    而用「窗口实际物理渲染尺寸 ÷ create_window 传入的逻辑尺寸」反推真实 scale，
    再用所在显示器物理工作区算物理居中，转回逻辑坐标交给 win.move，闭环正确。"""
    import ctypes, time
    from ctypes import wintypes
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        hwnd = 0
        for _ in range(20):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            return
        MONITOR_DEFAULTTONEAREST = 2
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, wintypes.LPVOID]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

        user32.GetWindowRect.argtypes = [wintypes.HWND, wintypes.LPVOID]
        user32.GetWindowRect.restype = wintypes.BOOL
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        pr_w = max(1, r.right - r.left)
        pr_h = max(1, r.bottom - r.top)
        # 真实 scale = 实际物理渲染尺寸 / 逻辑尺寸（绕过 GetDpiForWindow 不准）
        sx = pr_w / float(w)
        sy = pr_h / float(h)
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return
        wa = mi.rcWork
        wa_w = wa.right - wa.left
        wa_h = wa.bottom - wa.top
        # 物理像素居中
        cx = wa.left + max(0, (wa_w - pr_w) // 2)
        cy = wa.top + max(0, (wa_h - pr_h) // 2)
        # 转回逻辑像素交给 win.move（内部再 ×scale 回物理，闭环正确）
        win.move(int(round(cx / sx)), int(round(cy / sy)))
    except Exception:
        pass


def _restore_pos(win, title, w, h, pos):
    """shown 回调：有记忆坐标则由 pywebview 直接 Manual 定位（已正确）；
    无记忆（首次/偏出屏幕）则重新精确居中。"""
    try:
        if pos:
            return
        _auto_center(win, title, w, h)
    except Exception:
        pass


def _save_win_pos(key, win, title=None):
    """关闭窗口时把当前坐标写回 config.json，下次启动复用（仍在屏幕内才有效）。
    pywebview 的 win.x/win.y 为逻辑像素，与 create_window 入参一致，无需再做转换。"""
    try:
        x = getattr(win, "x", None)
        y = getattr(win, "y", None)
        if x is None or y is None:
            return
        pref = load_pref()
        pref[key] = [int(x), int(y)]
        save_pref(pref)
    except Exception:
        pass


def detect_system_theme():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 1 else "dark"
        except Exception:
            pass
    return "light"


# ------------------------------------------------------------------
# 文件/文件夹选择：多方案自动回退（任一失败自动换下一个）
#   1) ctypes + comdlg32 / shell32（原生，需在 STA 单元中调用）
#   2) PowerShell + Windows.Forms（独立进程，最稳，不受本程序线程/COM 影响）
#   3) tkinter filedialog
#   4) pywebview 内置对话框（最后兜底）
# ------------------------------------------------------------------
def _co_sta_enter():
    """在进入需要 COM 的线程里初始化 STA；返回是否由本函数负责 Uninitialize。"""
    try:
        r = ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        return r == 0  # S_OK 表示本次初始化成功，需配对 Uninitialize
    except Exception:
        return False


def _co_sta_exit(owned):
    if owned:
        try:
            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass


def _owner_hwnd():
    """返回当前前台窗口句柄（即本程序主窗口），作为对话框所有者使之置顶。"""
    try:
        import webview as _wv
        if getattr(_wv, "windows", None):
            h = getattr(_wv.windows[0], "hwnd", None)
            if h:
                return int(h)
    except Exception:
        pass
    try:
        h = ctypes.windll.user32.GetForegroundWindow()
        if h:
            return int(h)
    except Exception:
        pass
    return 0


def _native_open_file(title, filter_pairs, initial_dir=None, owner=0):
    """filter_pairs: [(说明, 通配), ...]，返回选中的完整路径或 None。"""
    import ctypes
    from ctypes import wintypes

    class OPENFILENAME(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", ctypes.c_void_p),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    OFN_EXPLORER = 0x00080000
    OFN_FILEMUSTEXIST = 0x00001000
    OFN_PATHMUSTEXIST = 0x00000800
    OFN_NOCHANGEDIR = 0x00000008

    filt = "".join(f"{d}\0{m}\0" for d, m in filter_pairs) + "\0"
    buf = ctypes.create_unicode_buffer(4096)
    ofn = OPENFILENAME()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAME)
    ofn.lpstrFilter = filt
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 4096
    ofn.lpstrTitle = title
    ofn.hwndOwner = owner or 0
    ofn.nFilterIndex = 1
    if initial_dir and os.path.isdir(initial_dir):
        ofn.lpstrInitialDir = initial_dir
    ofn.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
    owned = _co_sta_enter()
    try:
        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
            return buf.value or None
        return None
    finally:
        _co_sta_exit(owned)


def _native_folder(title, initial_dir=None, owner=0):
    """选择文件夹，返回完整路径或 None。"""
    import ctypes
    from ctypes import wintypes

    class BROWSEINFO(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", ctypes.c_void_p),
            ("iImage", ctypes.c_int),
        ]

    BIF_RETURNONLYFSDIRS = 0x00000001
    BIF_NEWDIALOGSTYLE = 0x00000040
    BIF_EDITBOX = 0x00000010

    disp = ctypes.create_unicode_buffer(4096)
    bi = BROWSEINFO()
    bi.pszDisplayName = ctypes.cast(disp, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.hwndOwner = owner or 0
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
    _sh = ctypes.windll.shell32
    _ol = ctypes.windll.ole32
    _sh.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
    _sh.SHBrowseForFolderW.restype = ctypes.c_void_p
    _sh.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    _sh.SHGetPathFromIDListW.restype = wintypes.BOOL
    _ol.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    owned = _co_sta_enter()
    try:
        pidl = _sh.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None
        try:
            path = ctypes.create_unicode_buffer(4096)
            ok = _sh.SHGetPathFromIDListW(pidl, path)
            return path.value if ok else None
        finally:
            try:
                _ol.CoTaskMemFree(pidl)
            except Exception:
                pass
    finally:
        _co_sta_exit(owned)


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _ps_build_filter(filter_pairs):
    # Windows.Forms 用 '显示|掩码' 成对，多个掩码以 ';' 分隔
    return "|".join(f"{d}|{m}" for d, m in filter_pairs)


def _ps_run(script):
    try:
        # CREATE_NO_WINDOW=0x08000000：彻底不创建控制台窗口（比 STARTF_USESHOWWINDOW 更可靠）
        CREATE_NO_WINDOW = 0x08000000
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True, text=True, timeout=600,
            creationflags=CREATE_NO_WINDOW,
        )
        for c in reversed((out.stdout or "").splitlines()):
            c = c.strip()
            if not c:
                continue
            if re.match(r"^[A-Za-z]:\\", c) or c.startswith("\\\\") \
                    or os.path.isfile(c) or os.path.isdir(c):
                return c
    except Exception:
        pass
    return None


def _ps_open_file(title, filter_pairs, initial_dir=None, owner=0):
    flt = _ps_build_filter(filter_pairs)
    init = initial_dir if (initial_dir and os.path.isdir(initial_dir)) else ""
    # 简化：用 [Console]::Out.WriteLine 直接走 stdout，避免缓冲；不加 owner（让对话框顶层显示）
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;\n"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;\n"
        f"$d.Title={_ps_quote(title)};\n"
        f"$d.Filter={_ps_quote(flt)};\n"
        f"$d.InitialDirectory={_ps_quote(init)};\n"
        "$d.Multiselect=$false;$d.CheckFileExists=$true;$d.CheckPathExists=$true;\n"
        "$r=$d.ShowDialog();\n"
        "if($r -eq 'OK'){ [Console]::Out.WriteLine($d.FileName) };\n"
        "$d.Dispose();\n"
    )
    return _ps_run(script)


def _ps_folder(title, initial_dir=None, owner=0):
    init = initial_dir if (initial_dir and os.path.isdir(initial_dir)) else ""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;\n"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;\n"
        f"$d.Description={_ps_quote(title)};\n"
        f"$d.SelectedPath={_ps_quote(init)};\n"
        "$r=$d.ShowDialog();\n"
        "if($r -eq 'OK'){ [Console]::Out.WriteLine($d.SelectedPath) };\n"
        "$d.Dispose();\n"
    )
    return _ps_run(script)


def _tk_open_file(title, filter_pairs, initial_dir=None):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        ft = [(d, m.replace(";", " ")) for d, m in filter_pairs]
        p = filedialog.askopenfilename(
            title=title, initialdir=initial_dir or None, filetypes=ft or None)
        root.destroy()
        return p or None
    except Exception:
        return None


def _tk_folder(title, initial_dir=None):
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        p = filedialog.askdirectory(title=title, initialdir=initial_dir or None)
        root.destroy()
        return p or None
    except Exception:
        return None


# ------------------------------------------------------------------
# IFileDialog（Vista+ 现代文件对话框，与资源管理器用的是同一个）——
# 用原始 ctypes 操作 COM 接口，是 Windows 上最可靠的弹窗方式
# ------------------------------------------------------------------
def _ifiledialog(title, initial_dir=None, owner=0, pick_folder=False, filter_pairs=None):
    import ctypes
    from ctypes import (byref, c_void_p, c_ulong, c_ushort, c_ubyte, c_int,
                        c_wchar_p, c_uint, POINTER, WINFUNCTYPE)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                    ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

    # CLSID_FileOpenDialog = {DC1C5A9C-E88A-4DDE-A5A1-60F82A5A11B7}
    CLSID_FileOpenDialog = GUID(0xDC1C5A9C, 0xE88A, 0x4DDE,
        (0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x5A, 0x11, 0xB7))
    # IID_IFileDialog = {42F85136-DB7E-439C-85F1-E407C944DFB7}
    IID_IFileDialog = GUID(0x42F85136, 0xDB7E, 0x439C,
        (0x85, 0xF1, 0xE4, 0x07, 0xC9, 0x44, 0xDF, 0xB7))
    SIGDN_FILESYSPATH = 0x80058000
    FOS_PICKFOLDERS = 0x20
    FOS_FORCEFILESYSTEM = 0x40
    FOS_NOCHANGEDIR = 0x08

    ole32 = ctypes.windll.ole32
    # STA 单元（IFileDialog 需要）
    r = ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    co_owned = (r == 0)  # S_OK=0 才需要配对 Uninitialize
    try:
        ppv = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID_FileOpenDialog), None, 0x1,  # CLSCTX_INPROC_SERVER
            byref(IID_IFileDialog), byref(ppv))
        if hr != 0 or not ppv.value:
            return None
        # vtable: ppv[0] 是 vtable 指针
        vtbl = ctypes.cast(ctypes.cast(ppv, POINTER(c_void_p))[0], POINTER(c_void_p))
        # IFileDialog vtable 槽位（继承 IModalWindow）：Show=3, SetFileTypes=4,
        # SetFileTypeIndex=5, SetOptions=9, SetTitle=17, GetResult=20
        Show = WINFUNCTYPE(c_int, c_void_p, c_void_p)(vtbl[3])
        SetFileTypes = WINFUNCTYPE(c_int, c_void_p, c_uint, c_void_p)(vtbl[4])
        SetFileTypeIndex = WINFUNCTYPE(c_int, c_void_p, c_uint)(vtbl[5])
        SetOptions = WINFUNCTYPE(c_int, c_void_p, c_ulong)(vtbl[9])
        SetTitle = WINFUNCTYPE(c_int, c_void_p, c_wchar_p)(vtbl[17])
        GetResult = WINFUNCTYPE(c_int, c_void_p, POINTER(c_void_p))(vtbl[20])

        fos = FOS_FORCEFILESYSTEM | FOS_NOCHANGEDIR
        if pick_folder:
            fos |= FOS_PICKFOLDERS
        SetOptions(ppv, fos)

        if not pick_folder and filter_pairs:
            class COMDLG_FILTERSPEC(ctypes.Structure):
                _fields_ = [("pszName", c_wchar_p), ("pszSpec", c_wchar_p)]
            arr = (COMDLG_FILTERSPEC * len(filter_pairs))()
            for i, (n, s) in enumerate(filter_pairs):
                arr[i].pszName = n
                arr[i].pszSpec = s
            SetFileTypes(ppv, len(filter_pairs), ctypes.cast(arr, c_void_p))
            SetFileTypeIndex(ppv, 1)

        SetTitle(ppv, title)

        hr = Show(ppv, owner)
        if hr != 0:
            return None  # 用户取消或失败
        result = c_void_p()
        hr = GetResult(ppv, byref(result))
        if hr != 0 or not result.value:
            return None
        # IShellItem vtable: GetDisplayName=5
        si_vtbl = ctypes.cast(ctypes.cast(result, POINTER(c_void_p))[0], POINTER(c_void_p))
        GetDisplayName = WINFUNCTYPE(c_int, c_void_p, c_ulong, POINTER(c_wchar_p))(si_vtbl[5])
        name = c_wchar_p()
        hr = GetDisplayName(result, SIGDN_FILESYSPATH, byref(name))
        if hr == 0 and name.value:
            return name.value
        return None
    except Exception:
        return None
    finally:
        if co_owned:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass



def get_desktop_dir():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
                p = winreg.QueryValueEx(k, "Desktop")[0]
                if p and os.path.isdir(p):
                    return p
        except Exception:
            pass
    fb = os.path.join(os.path.expanduser("~"), "Desktop")
    return fb if os.path.isdir(fb) else os.path.expanduser("~")


def check_module_installed(name):
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def format_number(num):
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "N/A"
    if num != num or num in (float("inf"), float("-inf")):
        return "N/A"
    a = abs(num)
    if a >= 1e12: return f"{num/1e12:.2f} 万亿"
    if a >= 1e8:  return f"{num/1e8:.2f} 亿"
    if a >= 1e4:  return f"{num/1e4:.2f} 万"
    if a >= 1e3:  return f"{num/1e3:.2f} 千"
    return f"{int(num):,}"


def get_char_set(t):
    return {
        1: string.digits,
        2: string.ascii_lowercase,
        3: string.ascii_uppercase,
        4: string.digits + string.ascii_lowercase,
        5: string.digits + string.ascii_uppercase,
        6: string.digits + string.ascii_letters,
        7: string.digits + string.ascii_letters + string.punctuation,
    }.get(t, "")


PASSWORD_TYPE_LABELS = [
    (1, "仅数字"),
    (2, "仅小写字母"),
    (3, "仅大写字母"),
    (4, "数字 + 小写字母"),
    (5, "数字 + 大写字母"),
    (6, "数字 + 大小写字母"),
    (7, "所有字符（含特殊符号）"),
]

# 内置常见密码库（中英文 + 数字，按常见度排序）
COMMON_PASSWORDS = """123456
12345678
123456789
1234567890
1234567
000000
111111
888888
666666
123123
112233
5201314
5211314
1314520
1314521
7758521
520520
521521
iloveyou
woaini
woshinidie
qwerty
abc123
a123456
a12345678
password
password1
passw0rd
p@ssw0rd
admin
root
administrator
guest
123456a
1q2w3e4r
1qaz2wsx
qwerty123
qwertyuiop
asdfghjkl
zxcvbnm
123456789a
11111111
00000000
1234
12345
123abc
aabbcc
abcd1234
letmein
monkey
dragon
football
123qwe
qwe123
1q2w3e
88888888
520131
5201314a
1314520a
qq123456
qq12345678
taobao
alibaba
wang123
zhang123
li123456
123456aa
520520520
iloveyou1314
woaini1314
1314520520
19881123
19900101
20001101
520
521
1314
888
666
168
1234qwer
1a2b3c4d
q1w2e3r4
!@#$%^&*
qazwsx
tarena
admin123
root123
test
test123
demo
user
user123
admin888
administrator123
changeme
default
123456.com
www123
hello123
love123
"""


# ------------------------------------------------------------------
# 候选生成器
# ------------------------------------------------------------------
def generate_bruteforce_candidates(ptype, total_length, exact_length=False, prefix="", suffix=""):
    cs = get_char_set(ptype)
    if not cs:
        return
    fl = len(prefix) + len(suffix)
    max_mid = total_length - fl
    if max_mid < 0:
        return
    lengths = [max_mid] if exact_length else range(1 if fl == 0 else 0, max_mid + 1)
    for L in lengths:
        for combo in itertools.product(cs, repeat=L):
            yield prefix + "".join(combo) + suffix


def generate_dictionary_candidates(dict_path):
    with open(dict_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            pwd = line.rstrip("\r\n")
            if pwd:
                yield pwd


def count_lines(path):
    c = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.strip():
                c += 1
    return c


def parse_mask(mask):
    tokens, i = [], 0
    mp = {'l': string.ascii_lowercase, 'u': string.ascii_uppercase,
          'd': string.digits, 's': string.punctuation,
          'a': string.ascii_letters + string.digits + string.punctuation}
    while i < len(mask):
        c = mask[i]
        if c == '?' and i + 1 < len(mask) and mask[i + 1] in mp:
            tokens.append(('class', mp[mask[i + 1]]))
            i += 2
        else:
            tokens.append(('lit', c))
            i += 1
    return tokens


def mask_count(mask):
    n = 1
    for t in parse_mask(mask):
        if t[0] == 'class':
            n *= len(t[1])
    return n


def generate_mask_candidates(mask):
    tokens = parse_mask(mask)
    classes = [t[1] for t in tokens if t[0] == 'class']
    if not classes:
        s = "".join(t[1] for t in tokens)
        if s:
            yield s
        return
    for combo in itertools.product(*classes):
        out, ci = [], 0
        for t in tokens:
            if t[0] == 'lit':
                out.append(t[1])
            else:
                out.append(combo[ci]); ci += 1
        yield "".join(out)


def generate_hybrid_candidates(dict_path, mask, position="suffix", cap=8_000_000):
    words = [l.rstrip("\r\n") for l in open(dict_path, encoding="utf-8", errors="ignore") if l.strip()]
    cnt = 0
    for w in words:
        for m in generate_mask_candidates(mask):
            yield (w + m) if position == "suffix" else (m + w)
            cnt += 1
            if cnt >= cap:
                return


def generate_numeric_candidates(opts):
    seen = set()
    def add(x):
        if x and x not in seen:
            seen.add(x); return True
        return False
    if opts.get("years"):
        for y in range(1900, 2100):
            if add(str(y)): yield str(y)
    if opts.get("dates"):
        for y in range(1950, 2026):
            for m in range(1, 13):
                for d in range(1, 32):
                    a = f"{y}{m:02d}{d:02d}"
                    if add(a): yield a
                    b = f"{m:02d}{d:02d}{y}"
                    if add(b): yield b
        for m in range(1, 13):
            for d in range(1, 32):
                a = f"{m:02d}{d:02d}"
                if add(a): yield a
                b = f"{d:02d}{m:02d}"
                if add(b): yield b
    if opts.get("repeats"):
        for d in "123456789":
            for n in (4, 6, 8):
                if add(d * n): yield d * n
        for z in ("0" * 6, "0" * 8):
            if add(z): yield z
    if opts.get("seq"):
        for pat in ("123456", "654321", "12345678", "87654321", "123123",
                    "121212", "112233", "111111", "000000", "123456789",
                    "987654321", "1234", "4321", "234567", "345678"):
            if add(pat): yield pat
    if opts.get("short"):
        for n in range(0, 10000):
            s = str(n).zfill(4)          # 0000-9999，含前导零（手机尾号/PIN）
            if add(s): yield s
        for n in range(0, 1000):
            s = str(n)                   # 1-3 位纯数字
            if add(s): yield s
    bases = ["123", "1234", "12345", "123456", "888", "520", "521",
             "1314", "666", "168", "5201314", "1314520"]
    if opts.get("years"):
        bases += [str(y) for y in range(1980, 2015)]
    for b in bases:
        for s in ("", "!", "@", "#", "a", "A"):
            v = b + s
            if add(v): yield v


def generate_combinator_candidates(f1, f2, sep="", cap=3_000_000):
    a = list(itertools.islice((l.rstrip("\r\n") for l in open(f1, encoding="utf-8", errors="ignore") if l.strip()), 4000))
    b = list(itertools.islice((l.rstrip("\r\n") for l in open(f2, encoding="utf-8", errors="ignore") if l.strip()), 4000))
    cnt = 0
    for x in a:
        for y in b:
            yield x + sep + y
            cnt += 1
            if cnt >= cap:
                return


def generate_rules_candidates(dict_path, opts):
    base = [l.rstrip("\r\n") for l in open(dict_path, encoding="utf-8", errors="ignore") if l.strip()]
    suffixes = [""]
    if opts.get("suffix"):
        suffixes += ["!", "@", "#", "$", "%", "123", "1234", "12345", "888",
                     "520", "521", "1314", "666", "000", "111", "168", "5201314"]
    if opts.get("digits"):
        suffixes += [str(d).zfill(2) for d in range(0, 100)]
    for w in base:
        seen = set()
        def emit(x):
            if x and x not in seen:
                seen.add(x); return True
            return False
        if emit(w): yield w
        if opts.get("case"):
            for v in (w.lower(), w.upper(), w.capitalize()):
                if emit(v): yield v
        for s in suffixes:
            v = w + s
            if emit(v): yield v
            if opts.get("prefix"):
                v2 = s + w
                if emit(v2): yield v2


def estimate_count(kind, **kw):
    if kind == "brute":
        return _est_brute(**kw)
    if kind == "mask":
        return mask_count(kw["mask"])
    if kind == "dict":
        return count_lines(kw["path"])
    if kind == "hybrid":
        try:
            return count_lines(kw["path"]) * mask_count(kw["mask"])
        except Exception:
            return None
    if kind == "numeric":
        return None
    if kind == "common":
        return len([p for p in COMMON_PASSWORDS.splitlines() if p.strip()])
    if kind == "combinator":
        return None
    if kind == "rules":
        n = count_lines(kw["path"])
        return n * 6 if n else 0
    if kind == "gen":
        try:
            cs = get_char_set(kw["ptype"])
            lo, hi, q = kw["lo"], kw["hi"], kw["qty"]
            if q > 0:
                return q
            total = 0
            for L in range(lo, hi + 1):
                total += len(cs) ** L
            return total
        except Exception:
            return None
    return None


def _est_brute(ptype, total_length, exact_length, prefix, suffix):
    cs = get_char_set(ptype)
    if not cs:
        return 0
    n = len(cs)
    fl = len(prefix) + len(suffix)
    total = 0
    if exact_length:
        mid = total_length - fl
        if mid >= 0:
            total = n ** mid
    else:
        max_mid = total_length - fl
        if max_mid >= 0:
            start = 1 if fl == 0 else 0
            for L in range(start, max_mid + 1):
                total += n ** L
    return total


# ------------------------------------------------------------------
# 破解引擎（多进程并行）
# ------------------------------------------------------------------
# worker 进程级状态：zip 只打开一次并复用，避免每个任务块重复解析
_WT = None        # CrackTarget
_WERR = None


def _worker_init(zip_path):
    """每个子进程启动时打开一次压缩包（兼容 zip/7z/rar）。"""
    global _WT, _WERR
    _WT = None
    _WERR = None
    try:
        _WT = CrackTarget(zip_path)
        if _WT.err:
            _WERR = _WT.err
    except Exception as e:
        _WERR = str(e)


def _crack_worker(passwords):
    tried = len(passwords)
    if _WERR or _WT is None:
        return ("error", _WERR or "压缩包未初始化", tried)
    try:
        for pwd in passwords:
            if _WT.test_password(pwd):
                return ("found", pwd, tried)
        return ("done", None, tried)
    except Exception as e:
        return ("error", str(e), tried)


def _decode_zip_name(info):
    """把 ZIP 条目的文件名还原成正确字符串。

    中文 Windows 制作的 ZIP 文件名通常是 GBK/GB18030 编码，而标准库
    zipfile/pyzipper 默认按 CP437 解码，会导致中文名乱码。这里优先用
    条目标志位判断是否 UTF-8（flag 0x800），否则把 CP437 视图还原成原始
    字节再按 GB18030 解码。
    """
    raw = getattr(info, "orig_filename", None)
    if isinstance(raw, bytes):
        if info.flag_bits & 0x800:
            return raw.decode("utf-8", "replace")
        for enc in ("gb18030", "utf-8"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("gb18030")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _extract_zip_fix_encoding(zf, dst, pwd=None):
    """解压 ZIP 并修正文件名编码，避免中文名乱码。

    extractall() 按 CP437 写出乱码名，这里再把每个文件重命名为正确编码的
    名字，最后清理残留的空目录。
    """
    if pwd:
        zf.setpassword(pwd.encode("utf-8"))
    zf.extractall(path=dst)
    for info in zf.infolist():
        if info.is_dir():
            continue
        wrong = info.filename
        correct = _decode_zip_name(info)
        if not correct or correct == wrong:
            continue
        wparts = [p for p in wrong.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        cparts = [p for p in correct.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        wpath = os.path.join(dst, *wparts)
        cpath = os.path.join(dst, *cparts)
        if os.path.isfile(wpath) and not os.path.exists(cpath):
            parent = os.path.dirname(cpath)
            if parent:
                os.makedirs(parent, exist_ok=True)
            os.rename(wpath, cpath)
    for root, dirs, _ in os.walk(dst, topdown=False):
        for d in dirs:
            dp = os.path.join(root, d)
            try:
                if not os.listdir(dp):
                    os.rmdir(dp)
            except OSError:
                pass


def unzip_file(zip_path, base_dir, password=None):
    try:
        t = CrackTarget(zip_path)
        if t.err:
            return False, t.err
        return t.extract(password, base_dir)
    except Exception as e:
        return False, f"解压失败：{e}"


def is_zip_encrypted(zip_path):
    try:
        import pyzipper
        with pyzipper.AESZipFile(zip_path, "r") as zf:
            infos = [i for i in zf.infolist() if not getattr(i, "is_dir", lambda: False)()]
            if not infos:
                return False, "empty"
            target = min(infos, key=lambda i: i.file_size)
            enc_type = "unknown"
            try:
                flag = getattr(target, "flag_bits", 0)
                if not (flag & 0x1):
                    return False, "none"
                # AES 使用扩展字段，传统 ZipCrypto 没有
                if getattr(target, "compress_type", None) == 99:
                    enc_type = "aes"
                else:
                    enc_type = "zipcrypto"
            except Exception:
                pass
            return True, enc_type
    except Exception:
        return False, "error"


def detect_format(path):
    """根据扩展名与文件头嗅探压缩包格式：zip / 7z / rar / None。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".zip":
        return "zip"
    if ext == ".7z":
        return "7z"
    if ext == ".rar":
        return "rar"
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
            return "zip"
        if head[:6] == b"7z\xbc\xaf\x27\x1c":
            return "7z"
        if head[:8] in (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01"):
            return "rar"
    except Exception:
        pass
    return None


_UNRAR_PATH = None


def ensure_unrar(log=None):
    """定位或自动下载 UnRAR.exe（rarfile 破解 RAR 所必需）。返回路径或 None。"""
    global _UNRAR_PATH
    if _UNRAR_PATH and os.path.isfile(_UNRAR_PATH):
        return _UNRAR_PATH
    cand = os.path.join(_cfg_dir(), "UnRAR.exe")
    if os.path.isfile(cand):
        _UNRAR_PATH = cand
        try:
            import rarfile
            rarfile.UNRAR_TOOL = cand
        except Exception:
            pass
        return _UNRAR_PATH
    try:
        from shutil import which
        for name in ("UnRAR.exe", "unrar.exe", "unrar", "rar"):
            p = which(name)
            if p:
                _UNRAR_PATH = p
                try:
                    import rarfile
                    rarfile.UNRAR_TOOL = p
                except Exception:
                    pass
                return _UNRAR_PATH
    except Exception:
        pass
    url = "https://www.rarlab.com/rar/unrarw64.exe"
    try:
        if log:
            log("正在下载 UnRAR.exe（RAR 破解所需）…", "info")
        import urllib.request
        os.makedirs(_cfg_dir(), exist_ok=True)
        urllib.request.urlretrieve(url, cand)
        if os.path.isfile(cand):
            _UNRAR_PATH = cand
            try:
                import rarfile
                rarfile.UNRAR_TOOL = cand
            except Exception:
                pass
            if log:
                log("UnRAR.exe 已就绪。", "ok")
            return _UNRAR_PATH
    except Exception as e:
        if log:
            log("UnRAR.exe 下载失败：" + str(e), "warn")
    return None


class CrackTarget:
    """统一的压缩包抽象，支持 zip / 7z / rar 的密码测试与解压。
    穷举/字典/掩码/混合/数字规律/常见/组合/规则等方法都通过它工作。"""

    def __init__(self, path):
        self.path = path
        self.fmt = detect_format(path)
        self.archive = None        # zip 时复用句柄（提速）；7z/rar 时仅记录路径
        self.target = None         # zip: ZipInfo；其它: 文件名字符串
        self.target_name = None
        self.err = None
        self._open()

    def _open(self):
        try:
            if self.fmt == "zip":
                import pyzipper
                zf = pyzipper.AESZipFile(self.path)
                infos = [i for i in zf.infolist() if not getattr(i, "is_dir", lambda: False)()]
                if not infos:
                    self.err = "压缩包内没有可解密的文件"
                    return
                self.archive = zf
                t = min(infos, key=lambda i: i.file_size)
                self.target = t
                self.target_name = t.filename
            elif self.fmt == "7z":
                import py7zr
                zf = py7zr.SevenZipFile(self.path, "r")
                items = [it for it in zf.list() if it.filename and not it.filename.endswith("/")]
                if not items:
                    self.err = "压缩包内没有可解密的文件"
                    return
                t = min(items, key=lambda it: getattr(it, "uncompressed", 0) or 0)
                self.target_name = t.filename
            elif self.fmt == "rar":
                ensure_unrar()
                import rarfile
                rf = rarfile.RarFile(self.path)
                infos = [i for i in rf.infolist() if not i.is_dir()]
                if not infos:
                    self.err = "压缩包内没有可解密的文件"
                    return
                t = min(infos, key=lambda i: i.file_size)
                self.target_name = t.filename
            else:
                self.err = "不支持的压缩包格式（仅支持 ZIP / 7Z / RAR）"
        except Exception as e:
            self.err = "打开压缩包失败：" + str(e)

    def test_password(self, pwd):
        """测试密码是否正确，返回 bool。"""
        if self.err:
            return False
        if self.fmt == "zip":
            try:
                self.archive.setpassword(pwd.encode("utf-8"))
                with self.archive.open(self.target) as s:
                    s.read(1)
                return True
            except Exception:
                return False
        if self.fmt == "7z":
            try:
                import py7zr, tempfile
                with py7zr.SevenZipFile(self.path, "r", password=pwd) as zf:
                    td = tempfile.mkdtemp()
                    zf.extract(path=td, targets=[self.target_name])
                return True
            except Exception:
                return False
        if self.fmt == "rar":
            try:
                import rarfile
                with rarfile.RarFile(self.path, pwd=pwd) as rf:
                    with rf.open(self.target_name) as s:
                        s.read(1)
                return True
            except Exception:
                return False
        return False

    def extract(self, pwd, out):
        """解压到 out/压缩包名/，返回 (ok, 路径或错误信息)。"""
        if self.fmt is None:
            return False, "不支持的格式"
        folder = os.path.splitext(os.path.basename(self.path))[0]
        dst = os.path.join(out, folder)
        try:
            os.makedirs(dst, exist_ok=True)
            if self.fmt == "zip":
                import pyzipper
                with pyzipper.AESZipFile(self.path, "r") as zf:
                    if pwd:
                        zf.setpassword(pwd.encode("utf-8"))
                    _extract_zip_fix_encoding(zf, dst, pwd)
            elif self.fmt == "7z":
                import py7zr
                with py7zr.SevenZipFile(self.path, "r", password=pwd) as zf:
                    zf.extractall(path=dst)
            elif self.fmt == "rar":
                ensure_unrar()
                import rarfile
                with rarfile.RarFile(self.path, pwd=pwd) as rf:
                    rf.extractall(path=dst)
            return True, dst
        except Exception as e:
            s = str(e).lower()
            if any(k in s for k in ("password", "bad", "wrong", "crc", "fail")):
                return False, "密码错误"
            return False, f"解压失败：{e}"


def is_archive_encrypted(path):
    """返回 (是否加密, 类型标签)。类型标签：aes/zipcrypto/7z/rar/none/empty/error/unknown。"""
    fmt = detect_format(path)
    if fmt is None:
        return False, "unknown"
    try:
        if fmt == "zip":
            import pyzipper
            with pyzipper.AESZipFile(path, "r") as zf:
                infos = [i for i in zf.infolist() if not getattr(i, "is_dir", lambda: False)()]
                if not infos:
                    return False, "empty"
                target = min(infos, key=lambda i: i.file_size)
                if not (getattr(target, "flag_bits", 0) & 0x1):
                    return False, "none"
                enc = "aes" if getattr(target, "compress_type", None) == 99 else "zipcrypto"
                return True, enc
        if fmt == "7z":
            import py7zr
            try:
                with py7zr.SevenZipFile(path, "r") as zf:
                    if zf.needs_password():
                        return True, "7z"
                    return False, "none"
            except Exception:
                return True, "7z"
        if fmt == "rar":
            import rarfile
            with rarfile.RarFile(path) as rf:
                if rf.needs_password():
                    return True, "rar"
                return False, "none"
    except Exception:
        return False, "error"
    return False, "none"


def is_zip_encrypted(zip_path):
    """向后兼容别名。"""
    return is_archive_encrypted(zip_path)


def chunked(iterable, size):
    it = iter(iterable)
    while True:
        chunk = list(itertools.islice(it, size))
        if not chunk:
            return
        yield chunk


class Cracker:
    def __init__(self, on_event):
        self.on_event = on_event
        self.stop_event = threading.Event()
        self.thread = None
        self.pool = None

    def run(self, zip_path, save_path, cores, cand_factory, total):
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._manager, args=(zip_path, save_path, cores, cand_factory, total),
            daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.pool is not None:
            try:
                self.pool.terminate()
            except Exception:
                pass

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def _manager(self, zip_path, save_path, cores, cand_factory, total):
        start = time.time()
        tried = 0
        found = None
        last = 0
        try:
            self.pool = multiprocessing.Pool(processes=cores, initializer=_worker_init, initargs=(zip_path,))
            task_iter = chunked(cand_factory(), 2000)
            window = max(2, cores * 3)
            pending = deque()
            exhausted = False

            def fill():
                nonlocal exhausted
                while not exhausted and len(pending) < window:
                    try:
                        chunk = next(task_iter)
                    except StopIteration:
                        exhausted = True
                        break
                    pending.append(self.pool.apply_async(_crack_worker, (chunk,)))

            fill()
            while pending:
                if self.stop_event.is_set():
                    try: self.pool.terminate()
                    except Exception: pass
                    self.on_event("stopped", "任务已手动停止。")
                    return
                r = pending.popleft()
                try:
                    status, val, cnt = r.get(timeout=0.2)
                except multiprocessing.TimeoutError:
                    pending.appendleft(r)
                    now = time.time()
                    if now - last >= 0.25:
                        last = now
                        el = now - start
                        speed = tried / el if el > 0 else 0
                        eta = (total - tried) / speed if (total and speed > 0) else float("inf")
                        self.on_event("progress", (tried, total, speed, eta))
                    continue
                tried += cnt
                if status == "found":
                    found = val
                    try: self.pool.terminate()
                    except Exception: pass
                    break
                if status == "error":
                    try: self.pool.terminate()
                    except Exception: pass
                    self.on_event("fail", f"读取压缩包出错：{val}")
                    return
                fill()
                now = time.time()
                if now - last >= 0.25:
                    last = now
                    el = now - start
                    speed = tried / el if el > 0 else 0
                    eta = (total - tried) / speed if (total and speed > 0) else float("inf")
                    self.on_event("progress", (tried, total, speed, eta))
            if not found:
                try:
                    self.pool.close(); self.pool.join()
                except Exception:
                    pass
        except Exception as e:
            self.on_event("fail", f"破解过程出错：{e}")
            return
        finally:
            self.pool = None

        if self.stop_event.is_set():
            return
        if found:
            el = max(time.time() - start, 0.001)
            self.on_event("progress", (tried, total or tried, tried / el, 0))
            ok, info = unzip_file(zip_path, save_path, found)
            if ok:
                self.on_event("found", (found, info, tried, round(el, 1)))
            else:
                self.on_event("found_no_extract", (found, info))
        else:
            self.on_event("fail", f"破解完成，共尝试 {format_number(tried)} 个密码，未找到正确密码。")


def find_bkcrack():
    p = shutil.which("bkcrack")
    if p:
        return p
    roots = []
    try:
        roots.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    except Exception:
        pass
    roots += [os.getcwd(), get_desktop_dir()]
    for r in roots:
        for name in ("bkcrack.exe", "bkcrack"):
            c = os.path.join(r, name)
            if os.path.isfile(c):
                return c
            c2 = os.path.join(r, "bkcrack", name)
            if os.path.isfile(c2):
                return c2
    return None


# ------------------------------------------------------------------
# 前端 HTML / Fluent Design（原生 Windows 11 观感）
# ------------------------------------------------------------------
HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --accent:#0067c0; --accent-hover:#1a75c8; --accent-press:#005499;
    --txt:#1a1a1a; --sub:#616161;
    --card-solid:#ffffff; --card2-solid:#f5f5f5;
    --panel-alpha:100;
    --card:#ffffff; --card2:#f5f5f5;
    --line:#e5e5e5; --btn:#fbfbfb; --btn-hover:#f0f0f0; --btn-press:#e6e6e6;
    --panel:#f3f3f3; --ok:#0f7b0f; --warn:#9d5d00; --err:#c42b1c;
    --field:#ffffff; --radius:7px; --shadow:0 2px 6px rgba(0,0,0,.06);
  }
  body.dark{
    --accent:#4cc2ff; --accent-hover:#63caff; --accent-press:#3aa0d8;
    --txt:#f0f0f0; --sub:#c4c8d0;
    --card-solid:#2b2b2b; --card2-solid:#333333;
    --panel-alpha:100;
    --card:#2b2b2b; --card2:#333333;
    --line:#3d3d3d; --btn:#333333; --btn-hover:#3d3d3d; --btn-press:#2a2a2a;
    --panel:#23262d; --field:#2d2d2d; --ok:#6ccb5f; --warn:#fce100; --err:#ff99a4;
    --shadow:0 2px 8px rgba(0,0,0,.35);
  }
  *{box-sizing:border-box;}
  html,body{margin:0;height:100%;}
  body{
    font-family:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Microsoft YaHei UI",sans-serif;
    background:transparent; color:var(--txt); font-size:13px;
    -webkit-font-smoothing:antialiased; overflow:auto;
  }
.wrap{width:100%;max-width:1120px;margin:0 auto;padding:14px clamp(8px,3vw,26px) 16px;
      min-height:100vh;}
  header{display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap;}
  header .brand{display:flex;align-items:center;gap:9px;}
  header .brand svg{color:var(--accent);}
  header .title{font-size:19px;font-weight:600;letter-spacing:.2px;}
  header .right{margin-left:auto;display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
  .seg{display:inline-flex;background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:2px;gap:2px;}
  .seg button{border:none;background:transparent;color:var(--sub);padding:5px 9px;border-radius:6px;
    display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer;}
  .seg button.on{background:var(--card);color:var(--txt);box-shadow:var(--shadow);}
  .seg button svg{width:15px;height:15px;}
  .corebox{display:flex;align-items:center;gap:7px;color:var(--sub);font-size:12px;
    background:var(--card2);border:1px solid var(--line);border-radius:9px;padding:4px 9px;}
  .corebox svg{color:var(--accent);}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
        padding:13px 15px;margin-bottom:11px;box-shadow:var(--shadow);position:relative;overflow:visible;}
  .grid{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:8px;align-items:center;margin-bottom:8px;}
  .grid:last-child{margin-bottom:0;}
  .grid .lbl{color:var(--sub);white-space:nowrap;}
  .grid > input,.grid > select,.grid > .csel{min-width:0;width:100%;}
  input[type=text]{background:var(--field);color:var(--txt);border:1px solid var(--line);
    border-radius:var(--radius);padding:8px 10px;font-size:13px;outline:none;width:100%;font-family:inherit;}
  input[type=text]:focus{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 30%,transparent);}
  select{background:var(--field);color:var(--txt);border:1px solid var(--line);
    border-radius:var(--radius);padding:8px 30px 8px 11px;font-size:13px;outline:none;font-family:inherit;
    cursor:pointer;appearance:none;-webkit-appearance:none;-moz-appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%237a7a7a' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 10px center;background-size:12px;transition:.12s;}
  select:hover{border-color:var(--accent);}
  select:focus{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 28%,transparent);}
  body.dark select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23bcbcbc' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");}
  /* 自定义下拉（图标型） */
  .csel{position:relative;width:100%;min-width:0;}
  .csel-trigger{display:flex;align-items:center;gap:8px;width:100%;
    background:var(--field);color:var(--txt);border:1px solid var(--line);
    border-radius:var(--radius);padding:7px 10px;font-size:13px;cursor:pointer;
    font-family:inherit;transition:.12s;text-align:left;position:relative;}
  .csel-trigger:hover{border-color:var(--accent);}
  .csel-trigger.open{border-color:var(--accent);
    box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 28%,transparent);}
  .csel-icon{display:inline-flex;align-items:center;justify-content:center;
    color:var(--accent);flex:none;}
  .csel-icon svg{display:block;}
  .csel-label{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .csel-arrow{color:var(--sub);transition:transform .15s;flex:none;}
  .csel-trigger.open .csel-arrow{transform:rotate(180deg);}
  /* 下拉菜单：强不透明背景 + 阴影提升层级 + 关闭滚动穿透到父容器 */
  .csel-menu{position:absolute;left:0;right:0;top:calc(100% + 5px);z-index:300;
    background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    box-shadow:0 10px 26px rgba(0,0,0,.22),0 2px 6px rgba(0,0,0,.10);
    padding:5px;display:none;max-height:320px;overflow:auto;
    backdrop-filter:saturate(140%) blur(6px);
    margin:0;list-style:none;
    min-width:max-content;}
  .csel-menu > li{list-style:none;margin:0;padding:0;}
  body.dark .csel-menu{background:#2b2b2b;border-color:#3d3d3d;
    box-shadow:0 14px 32px rgba(0,0,0,.55),0 2px 6px rgba(0,0,0,.35);}
  .csel-menu.show{display:block;animation:cddIn .16s ease-out;}
  @keyframes cddIn{from{opacity:0;transform:translateY(-4px);}
                    to{opacity:1;transform:translateY(0);}}
  .csel-info{padding:8px 12px 7px;color:var(--sub);font-size:11px;line-height:1.5;
    background:color-mix(in srgb,var(--accent) 8%,transparent);
    border-bottom:1px solid var(--line);border-radius:var(--radius) var(--radius) 0 0;
    margin:-5px -5px 5px;}
  .csel-group{padding:6px 12px 3px;color:var(--sub);font-size:10.5px;
    text-transform:uppercase;letter-spacing:.5px;}
  .csel-sep{height:1px;background:var(--line);margin:5px 4px;}
  .csel-option{display:flex;align-items:center;gap:9px;
    padding:8px 30px 8px 12px;border-radius:7px;
    cursor:pointer;color:var(--txt);font-size:12.5px;transition:.1s;user-select:none;
    background:transparent;position:relative;white-space:nowrap;}
  .csel-option:hover{background:color-mix(in srgb,var(--accent) 12%,transparent);}
  .csel-option.active{background:color-mix(in srgb,var(--accent) 18%,transparent);
    color:var(--accent);font-weight:600;}
  .csel-option .opt-text{flex:1;min-width:0;}
  .csel-option .opt-check{position:absolute;right:10px;top:50%;
    transform:translateY(-50%);color:var(--accent);opacity:0;font-size:13px;}
  .csel-option.active .opt-check{opacity:1;}
  /* 头部核心数下拉：更紧凑的胶囊风格 */
  .corebox select{padding:5px 28px 5px 10px;font-size:12px;border-radius:8px;background-color:var(--card);}
  .hint{color:var(--sub);font-size:11px;margin-top:6px;line-height:1.6;opacity:.82;
    padding:6px 9px;background:color-mix(in srgb,var(--sub) 6%,transparent);
    border-left:2px solid color-mix(in srgb,var(--accent) 30%,transparent);
    border-radius:0 6px 6px 0;font-style:italic;}
  /* 输出预览卡（让按钮下方有明确 UI，避免卡片戛然而止） */
  .out-preview{margin-top:12px;padding:10px 12px;
    background:color-mix(in srgb,var(--accent) 5%,transparent);
    border:1px solid color-mix(in srgb,var(--accent) 18%,transparent);
    border-radius:var(--radius);}
  .op-head{display:flex;align-items:center;gap:8px;margin-bottom:6px;}
  .op-title{font-size:12px;font-weight:600;color:var(--accent);}
  .op-status{margin-left:auto;font-size:11px;padding:2px 8px;border-radius:10px;
    background:color-mix(in srgb,var(--sub) 12%,transparent);color:var(--sub);}
  .op-status.ok{background:color-mix(in srgb,#22c55e 18%,transparent);color:#22c55e;}
  .op-status.err{background:color-mix(in srgb,#ef4444 18%,transparent);color:#ef4444;}
  .op-path{font-family:ui-monospace,Consolas,monospace;font-size:12px;
    color:var(--txt);background:var(--field);padding:5px 8px;border-radius:5px;
    border:1px solid var(--line);word-break:break-all;line-height:1.45;}
  .op-meta{font-size:11px;color:var(--sub);margin-top:6px;line-height:1.5;opacity:.88;}
  button{font-family:inherit;font-size:13px;border-radius:var(--radius);padding:8px 15px;
    border:1px solid var(--line);background:var(--btn);color:var(--txt);cursor:pointer;transition:.12s;
    display:inline-flex;align-items:center;gap:6px;justify-content:center;}
  button:hover{background:var(--btn-hover);}
  button:active{background:var(--btn-press);}
  button svg{width:15px;height:15px;flex:none;}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600;}
  body.dark button.primary{color:#08233a;}
  button.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);}
  button.primary:active{background:var(--accent-press);}
  button.danger{background:var(--err);border-color:var(--err);color:#fff;font-weight:600;}
  body.dark button.danger{color:#3a0b0b;}
  button.danger:hover{filter:brightness(1.08);}
  button:disabled{opacity:.45;cursor:not-allowed;}
  button.ghost{padding:6px 10px;}
  /* 选择类按钮：强调色描边，与输入框形成统一的视觉组 */
  button.pick{border:1px solid var(--accent);color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,transparent);font-weight:600;}
  button.pick:hover{background:color-mix(in srgb,var(--accent) 17%,transparent);border-color:var(--accent-hover);}
  button.pick:active{background:color-mix(in srgb,var(--accent) 26%,transparent);}
  body.dark button.pick{color:var(--accent);}
  .tabs{display:flex;gap:3px;border-bottom:1px solid var(--line);margin:2px 0 12px;flex-wrap:wrap;}
  .tab{padding:9px 13px;cursor:pointer;color:var(--sub);border-bottom:2px solid transparent;
    font-size:13px;user-select:none;border-radius:6px 6px 0 0;transition:.12s;white-space:nowrap;}
  .tab:hover{color:var(--txt);background:var(--card2);}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600;}
  body.dark .tab{color:#d2d6de;text-shadow:0 1px 2px rgba(0,0,0,.55);}
  body.dark .tab:hover{color:#fff;}
  body.dark .seg button{color:#d2d6de;text-shadow:0 1px 2px rgba(0,0,0,.45);}
  .warn-badge{margin-left:5px;color:var(--warn);cursor:help;vertical-align:middle;}
  .modal-btns{display:flex;gap:8px;margin-top:14px;justify-content:flex-end;}
  .panel{display:none;}
  .panel.active{display:block;}
  .panel-head{display:flex;align-items:center;gap:8px;margin-bottom:10px;}
  .panel-head .ph-title{font-size:15px;font-weight:600;}
  .help{margin-left:auto;width:26px;height:26px;padding:0;border-radius:50%;color:var(--sub);}
  .help:hover{color:var(--accent);border-color:var(--accent);}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:9px;min-width:0;}
  .row:last-child{margin-bottom:0;}
  .row .lbl{color:var(--sub);min-width:0;flex-shrink:0;}
  .row > input,.row > select,.row > .csel{flex:1 1 200px;min-width:0;}
  .checks{display:flex;gap:10px 18px;flex-wrap:wrap;margin:4px 0 9px;}
  .checks label{display:flex;align-items:center;gap:6px;color:var(--txt);cursor:pointer;font-size:12.5px;}
  .content{padding-right:2px;}
  .stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:10px;margin:2px 0 11px;}
  .stat .box{background:var(--card2);border-radius:var(--radius);padding:11px 12px;text-align:center;}
  .stat .k{color:var(--sub);font-size:11px;}
  .stat .v{color:var(--accent);font-size:18px;font-weight:600;margin-top:3px;word-break:break-all;}
  progress{width:100%;height:8px;border-radius:6px;overflow:hidden;appearance:none;-webkit-appearance:none;}
  progress::-webkit-progress-bar{background:var(--card2);border-radius:6px;}
  progress::-webkit-progress-value{background:var(--accent);border-radius:6px;transition:width .2s;}
  progress:indeterminate::-webkit-progress-value{background:linear-gradient(90deg,transparent,var(--accent),transparent);
    background-size:40% 100%;animation:ind 1.1s infinite linear;}
  @keyframes ind{from{background-position:-40% 0;}to{background-position:140% 0;}}
  #log{height:150px;background:var(--field);border:1px solid var(--line);border-radius:var(--radius);
    padding:9px 11px;overflow:auto;font-family:"Cascadia Code",Consolas,monospace;font-size:12px;
    line-height:1.55;color:var(--txt);white-space:pre-wrap;user-select:text;}
  .log-ok{color:var(--ok);} .log-err{color:var(--err);} .log-warn{color:var(--warn);}
  .log-info{color:var(--sub);} .log-status{color:var(--accent);}
  .deplog{background:var(--card2);border:1px solid var(--line);border-radius:var(--radius);
    padding:8px 10px;margin-top:6px;max-height:200px;overflow:auto;
    font-family:"Cascadia Code",Consolas,monospace;font-size:11.5px;line-height:1.55;color:var(--txt);user-select:text;}
  .deplog .log-line{white-space:pre-wrap;}
  .foot{display:flex;gap:8px;align-items:center;margin-top:9px;flex-wrap:wrap;}
  .spacer{flex:1;}
  .pill{font-size:11.5px;color:var(--sub);}
  /* modal */
  .mask{position:fixed;inset:0;background:rgba(0,0,0,.42);display:none;align-items:center;justify-content:center;z-index:50;
    backdrop-filter:blur(2px);}
  .mask.show{display:flex;}
  .modal{--card:var(--card-solid);--card2:var(--card2-solid);background:var(--card,#222);border:1px solid var(--line);border-radius:11px;max-width:520px;width:calc(100% - 48px);
    box-shadow:0 12px 40px rgba(0,0,0,.35);overflow:hidden;animation:pop .16s ease-out;}
  @keyframes pop{from{transform:scale(.94);opacity:0;}to{transform:scale(1);opacity:1;}}
  .modal .mh{display:flex;align-items:center;gap:10px;padding:16px 20px 4px;font-size:16px;font-weight:600;}
  .modal .mh svg{width:22px;height:22px;flex:none;}
  .modal .mb{padding:8px 20px 4px;color:var(--txt);font-size:13px;line-height:1.7;max-height:52vh;overflow:auto;overscroll-behavior:contain;}
  .modal .mb code{background:var(--card2);padding:1px 5px;border-radius:4px;font-family:Consolas,monospace;}
  .modal .mf{padding:14px 20px 18px;display:flex;justify-content:flex-end;gap:8px;}
  .mi-ok{color:var(--ok);} .mi-err{color:var(--err);} .mi-info{color:var(--accent);} .mi-warn{color:var(--warn);}
  /* 设置按钮（header 右上角齿轮） */
  .icon-btn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;
    border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--sub);
    cursor:pointer;transition:.12s;padding:0;}
  .icon-btn:hover{color:var(--accent);border-color:var(--accent);background:color-mix(in srgb,var(--accent) 10%,transparent);}
  /* 设置表单 */
  .set-row{display:flex;align-items:center;gap:12px;margin:12px 0;}
  .set-row > label{flex:0 0 92px;color:var(--sub);font-size:12.5px;}
  .set-row .set-ctrl{flex:1;display:flex;align-items:center;gap:10px;min-width:0;}
  .set-row input[type=color]{width:44px;height:30px;padding:2px;border:1px solid var(--line);
    border-radius:6px;background:var(--field);cursor:pointer;}
  .set-row input[type=text]{flex:1;width:auto;}
  .set-row input[type=range]{flex:1;}
  /* 美化滑块：填充轨道 + 圆润滑块 */
  input[type=range]{ -webkit-appearance:none; appearance:none; width:100%; height:6px; border-radius:6px;
    background:color-mix(in srgb,var(--accent) 22%,var(--line)); outline:none; cursor:pointer; }
  input[type=range]::-webkit-slider-thumb{ -webkit-appearance:none; appearance:none; width:16px; height:16px;
    border-radius:50%; background:var(--accent); border:2px solid #fff; box-shadow:0 1px 4px rgba(0,0,0,.3);
    transition:transform .12s ease; }
  input[type=range]::-webkit-slider-thumb:hover{ transform:scale(1.18); }
  input[type=range]::-moz-range-thumb{ width:16px; height:16px; border-radius:50%; background:var(--accent);
    border:2px solid #fff; box-shadow:0 1px 4px rgba(0,0,0,.3); cursor:pointer; }
  .set-swatches{display:flex;gap:6px;}
  .set-sw{width:20px;height:20px;border-radius:5px;border:1px solid var(--line);cursor:pointer;
    transition:.1s;}
  .set-sw:hover{transform:scale(1.12);}
  .set-val{font-size:12px;color:var(--sub);min-width:38px;text-align:right;}
  .set-preview{margin-top:10px;min-height:44px;border-radius:10px;border:1px dashed var(--line);
    background:var(--card2);padding:8px;display:flex;align-items:center;justify-content:center;overflow:hidden;}
  .set-preview img{max-width:100%;max-height:200px;width:auto;height:auto;border-radius:6px;display:block;
    transition:opacity .2s ease, filter .2s ease;box-shadow:0 2px 8px rgba(0,0,0,.18);}
  .set-preview-empty{font-size:11px;color:var(--sub);opacity:.8;}
  /* 设置分区 */
  .set-sec{background:color-mix(in srgb,var(--card2) 55%,transparent);border:1px solid var(--line);
    border-radius:12px;padding:13px 15px;margin-bottom:14px;}
  .set-sec-t{font-size:11.5px;font-weight:700;letter-spacing:.6px;color:var(--accent);
    text-transform:uppercase;margin-bottom:8px;}
  .set-row.col{display:block;}
  .set-row.col > label{display:block;margin-bottom:8px;flex:none;}
  .set-row.col .set-ctrl{width:100%;}
  .set-hint{font-size:11px;color:var(--sub);margin-top:6px;opacity:.85;line-height:1.5;}
  .set-file{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;
    border:1px dashed var(--line);border-radius:8px;background:var(--field);color:var(--sub);
    font-size:12px;transition:.12s;}
  .set-file:hover{border-color:var(--accent);color:var(--accent);background:color-mix(in srgb,var(--accent) 8%,transparent);}
  .set-file svg{width:15px;height:15px;}
  .set-file input[type=file]{display:none;}
  .set-clear{margin-top:10px;width:100%;padding:7px;border:1px solid var(--line);border-radius:8px;
    background:transparent;color:var(--sub);font-size:12px;cursor:pointer;transition:.12s;}
  .set-clear:hover{border-color:var(--err);color:var(--err);}
  /* 模块 / 依赖列表 */
  .mod-row{display:flex;align-items:center;gap:12px;padding:11px 12px;margin:9px 0;
    border:1px solid var(--line);border-radius:10px;background:color-mix(in srgb,var(--card) 60%,transparent);}
  .mod-info{flex:1;min-width:0;}
  .mod-name{font-size:13px;font-weight:600;color:var(--txt);}
  .mod-desc{font-size:11px;color:var(--sub);margin-top:3px;opacity:.85;}
  .mod-state{flex:0 0 auto;font-size:11.5px;padding:5px 10px;border-radius:20px;white-space:nowrap;}
  .mod-state.locked{color:#c4c8d0;background:rgba(255,255,255,.06);border:1px solid var(--line);}
  .mod-actions{flex:0 0 auto;display:flex;gap:8px;}
  .mod-actions button{padding:6px 14px;border-radius:8px;border:1px solid var(--line);
    background:var(--field);color:var(--txt);font-size:12px;cursor:pointer;transition:.12s;}
  .mod-actions button:hover{border-color:var(--accent);color:var(--accent);}
  .mod-actions button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
  .mod-actions button.primary:hover{background:var(--accent-hover);color:#fff;}
  .mod-actions button.danger:hover{border-color:var(--err);color:var(--err);}
  /* 设置分栏：左侧菜单 + 右侧大页面 + 右上角关闭 */
  .modal.set-big{max-width:860px;width:calc(100% - 48px);}
  .modal.set-big .mb{max-height:86vh;padding:0;overflow:hidden;}
  .set-shell{display:flex;min-height:540px;max-height:86vh;}
  .set-side{width:196px;flex:none;border-right:1px solid var(--line);padding:14px 10px;
    overflow:auto;background:color-mix(in srgb,var(--card2) 42%,transparent);overscroll-behavior:contain;}
  .set-side-t{font-size:11px;font-weight:700;letter-spacing:.6px;color:var(--sub);text-transform:uppercase;
    padding:2px 12px 8px;opacity:.8;}
  .set-nav{display:block;width:100%;text-align:left;padding:10px 12px;margin:3px 0;border:none;border-radius:8px;
    background:transparent;color:var(--sub);font-size:13px;cursor:pointer;transition:.12s;}
  .set-nav:hover{background:color-mix(in srgb,var(--accent) 10%,transparent);color:var(--txt);}
  .set-nav.on{background:var(--accent);color:#fff;font-weight:600;}
  .set-content{flex:1;display:flex;flex-direction:column;min-width:0;}
  .set-top{display:flex;align-items:center;justify-content:space-between;padding:13px 18px;
    border-bottom:1px solid var(--line);}
  .set-title{font-size:15px;font-weight:600;color:var(--txt);}
  .set-x{width:30px;height:30px;border-radius:8px;border:1px solid var(--line);background:transparent;
    color:var(--sub);font-size:15px;line-height:1;cursor:pointer;transition:.12s;}
  .set-x:hover{border-color:var(--err);color:var(--err);background:color-mix(in srgb,var(--err) 10%,transparent);}
  .set-scroll{flex:1;overflow:auto;padding:16px 18px;overscroll-behavior:contain;}
  .set-pane .set-sec{margin-bottom:0;}
  .set-foot{padding:12px 18px;border-top:1px solid var(--line);display:flex;justify-content:flex-end;gap:8px;}
  .set-foot button{padding:7px 16px;border-radius:8px;border:1px solid var(--line);background:var(--field);
    color:var(--txt);font-size:12.5px;cursor:pointer;transition:.12s;}
  .set-foot button:hover{border-color:var(--accent);color:var(--accent);}
  .set-foot button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
  .set-foot button.primary:hover{background:var(--accent-hover);color:#fff;}
  /* 背景图层（独立图层：模糊/图片透明度互不干扰 UI，窗口透明度另由 Python 分层窗口实现） */
  #bgLayer{position:fixed;inset:-40px;z-index:-1;pointer-events:none;
    background-color:var(--panel);background-size:cover;background-position:center;background-repeat:no-repeat;
    opacity:1;filter:none;transition:opacity .25s ease, filter .25s ease;}
  /* toast */
  #toasts{position:fixed;top:16px;left:50%;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:60;align-items:center;}
  .toast{--card:var(--card-solid);--card2:var(--card2-solid);background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);
    border-radius:8px;padding:10px 14px;box-shadow:0 6px 20px rgba(0,0,0,.25);font-size:12.5px;
    display:flex;align-items:center;gap:8px;animation:slide .2s ease-out;max-width:320px;}
  .toast.ok{border-left-color:var(--ok);} .toast.err{border-left-color:var(--err);}
  .toast.warn{border-left-color:var(--warn);}
  .toast svg{width:16px;height:16px;flex:none;}
  @keyframes slide{from{transform:translateY(-20px);opacity:0;}to{transform:translateY(0);opacity:1;}}
  /* 自定义下拉组件（替换原生 <select>，展开菜单完全可控美化） */
  .cdd{position:relative;display:inline-block;}
  .cdd-trigger{display:flex;align-items:center;gap:8px;padding:5px 10px 5px 12px;
    background:var(--card);border:1px solid var(--line);border-radius:8px;
    color:var(--txt);font-size:12px;cursor:pointer;font-family:inherit;
    transition:.15s;min-width:150px;}
  .cdd-trigger:hover{border-color:var(--accent);background:var(--card);}
  .cdd.open .cdd-trigger,.cdd-trigger:focus{
    border-color:var(--accent);
    box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 22%,transparent);}
  .cdd-label{flex:1;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .cdd-arrow{color:var(--sub);transition:transform .2s;flex:none;}
  .cdd.open .cdd-arrow{transform:rotate(180deg);color:var(--accent);}
  .cdd-menu{position:absolute;top:calc(100% + 5px);left:0;min-width:100%;
    background:var(--card);border:1px solid var(--line);border-radius:10px;
    box-shadow:0 14px 36px rgba(0,0,0,.22);max-height:300px;overflow-y:auto;
    display:none;z-index:200;padding:5px;
    scrollbar-width:thin;scrollbar-color:var(--accent) transparent;}
  .cdd-menu::-webkit-scrollbar{width:7px;}
  .cdd-menu::-webkit-scrollbar-thumb{background:var(--accent);border-radius:4px;}
  .cdd-menu::-webkit-scrollbar-track{background:transparent;}
  .cdd.open .cdd-menu{display:block;animation:cddIn .16s ease-out;}
  @keyframes cddIn{from{opacity:0;transform:translateY(-6px);}to{opacity:1;transform:translateY(0);}}
  .cdd-item{padding:8px 12px 8px 30px;border-radius:7px;cursor:pointer;font-size:12.5px;
    transition:.1s;color:var(--txt);position:relative;white-space:nowrap;}
  .cdd-item:hover{background:color-mix(in srgb,var(--accent) 12%,transparent);}
  .cdd-item.sel{background:color-mix(in srgb,var(--accent) 18%,transparent);
    color:var(--accent);font-weight:600;}
  .cdd-item.sel::before{content:"";position:absolute;left:10px;top:50%;
    width:13px;height:13px;transform:translateY(-50%);
    background:var(--accent);
    -webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='20 6 9 17 4 12'/%3E%3C/svg%3E") center/contain no-repeat;
            mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23000' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='20 6 9 17 4 12'/%3E%3C/svg%3E") center/contain no-repeat;}
  .cdd-sep{height:1px;background:var(--line);margin:5px 4px;}
  .cdd-group{padding:5px 12px 3px;color:var(--sub);font-size:10.5px;
    text-transform:uppercase;letter-spacing:.5px;}
  .cdd-info{padding:7px 12px;color:var(--sub);font-size:11px;line-height:1.5;
    background:color-mix(in srgb,var(--accent) 8%,transparent);
    border-bottom:1px solid var(--line);}
  .pick-card{display:flex;align-items:center;gap:10px;padding:10px 12px;
    background:var(--card);border:1px solid var(--line);border-radius:9px;
    cursor:pointer;font-size:12.5px;color:var(--txt);
    transition:.15s;user-select:none;}
  .pick-card:hover{border-color:var(--accent);background:var(--card-hover,var(--card));}
  .pick-card:focus,.pick-card:active{
    border-color:var(--accent);
    box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent);}
  .pick-icon{font-size:16px;line-height:1;flex:none;opacity:.85;}
  .pick-text{flex:1;text-align:left;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis;color:var(--sub);}
  .pick-card .pick-text:not(:empty){color:var(--txt);}
  .pick-arrow{width:9px;height:9px;border-right:2px solid var(--sub);
    border-bottom:2px solid var(--sub);transform:rotate(45deg);
    margin-right:4px;flex:none;transition:.15s;}
  .pick-card:hover .pick-arrow{border-color:var(--accent);}
  /* 复选框卡片行：把每个 checkbox 选项做成可点击的胶囊卡片，勾选时整体高亮 */
  .checks-card{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 9px;}
  .checks-card label{display:inline-flex;align-items:center;gap:8px;
    padding:7px 13px;background:var(--card);border:1px solid var(--line);
    border-radius:8px;cursor:pointer;font-size:12.5px;color:var(--txt);
    transition:.12s;user-select:none;font-weight:500;}
  .checks-card label:hover{border-color:var(--accent);}
  .checks-card label:has(input:checked){
    border-color:var(--accent);
    background:color-mix(in srgb,var(--accent) 13%,transparent);
    color:var(--accent);font-weight:600;
    box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 35%,transparent);}
  .checks-card input[type=checkbox]{
    accent-color:var(--accent);width:14px;height:14px;cursor:pointer;
    margin:0;flex:none;}
  /* 全局 checkbox 主题色：避免任何位置出现浏览器默认的刺眼蓝色方块 */
  input[type=checkbox]{
    accent-color:var(--accent);
    width:14px;height:14px;cursor:pointer;margin:0;flex:none;
    vertical-align:middle;}
  /* .row 里的单独复选框（bruteExact 等）也用胶囊胶囊样式，保证一致性 */
  .check-pill{display:inline-flex;align-items:center;gap:8px;
    padding:6px 12px;background:var(--card);border:1px solid var(--line);
    border-radius:8px;cursor:pointer;font-size:12.5px;color:var(--txt);
    transition:.12s;user-select:none;font-weight:500;}
  .check-pill:hover{border-color:var(--accent);}
  .check-pill:has(input:checked){
    border-color:var(--accent);
    background:color-mix(in srgb,var(--accent) 13%,transparent);
    color:var(--accent);font-weight:600;
    box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 35%,transparent);}
</style>
</head>
<body>
<div id="bgLayer"></div>
<div class="wrap">
  <header>
    <div class="brand">
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4.5" y="10.5" width="15" height="10" rx="2.2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/><circle cx="12" cy="15.2" r="1.4" fill="currentColor" stroke="none"/></svg>
      <span class="title">压缩包密码破解</span>
    </div>
    <div class="right">
      <span class="corebox">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/></svg>
        并行核心
        <div class="cdd" id="coresDD" title="自动：由程序选择兼顾速度与流畅的核心数（推荐）。&#10;全部：使用除 1 核外的所有核心，强制保留 1 核给系统，保证电脑不卡顿。">
          <button class="cdd-trigger" type="button" aria-haspopup="listbox">
            <span class="cdd-label">自动（推荐 8 核）</span>
            <svg class="cdd-arrow" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="cdd-menu" role="listbox"></div>
        </div>
      </span>
      <div class="seg" id="themeSeg">
        <button data-th="system" onclick="setTheme('system')" title="跟随系统">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M8 20h8M12 16v4"/></svg>系统</button>
        <button data-th="light" onclick="setTheme('light')" title="浅色">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>浅色</button>
        <button data-th="dark" onclick="setTheme('dark')" title="深色">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></svg>深色</button>
      </div>
      <button class="icon-btn" id="btnSettings" onclick="openSettings()" title="设置">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-2.82 1.17V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 8 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15H4.5a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 6 8a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6V4.5a2 2 0 1 1 4 0v.09A1.65 1.65 0 0 0 16 6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9h.1a2 2 0 1 1 0 4h-.1z"/></svg>
      </button>
    </div>
  </header>

  <div class="content">
  <div class="card">
    <div class="grid">
      <span class="lbl">压缩包</span>
      <input type="text" id="zipPath" placeholder="点击右侧按钮选择压缩包（.zip / .7z / .rar）">
      <button class="pick" onclick="pick('zip')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 6h5l2 2h9v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6z"/></svg>选择文件</button>
    </div>
    <div class="grid">
      <span class="lbl">保存路径</span>
      <input type="text" id="savePath" placeholder="解压输出目录">
      <button class="pick" onclick="pick('save')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 6h5l2 2h9v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6z"/></svg>选择目录</button>
    </div>
    <div class="hint" id="fileStatus">尚未选择压缩包</div>
  </div>

  <div class="tabs" id="tabs"></div>

  <!-- 直接解压 -->
  <div class="panel" data-tab="direct">
    <div class="card">
      <div class="panel-head"><span class="ph-title">直接解压</span><button class="help" onclick="showHelp('direct')">?</button></div>
      <div class="grid">
        <span class="lbl">已知密码</span>
        <input type="text" id="directPwd" placeholder="留空表示尝试无密码解压">
        <button class="primary" onclick="start('direct')">开始解压</button>
      </div>
      <div class="hint">将解压到 [保存路径]/[压缩包名]/。</div>
    </div>
  </div>

  <!-- 穷举破解 -->
  <div class="panel" data-tab="brute">
    <div class="card">
      <div class="panel-head"><span class="ph-title">穷举破解</span><button class="help" onclick="showHelp('brute')">?</button></div>
      <div class="row"><span class="lbl">密码类型</span><div class="csel" id="csel-bruteType">
        <input type="hidden" id="bruteType" value="1">
        <button type="button" class="csel-trigger" id="csel-bruteType-trigger" aria-haspopup="listbox" aria-expanded="false">
          <span class="csel-icon" id="csel-bruteType-icon"></span>
          <span class="csel-label" id="csel-bruteType-label">仅数字</span>
          <svg class="csel-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <ul class="csel-menu" id="csel-bruteType-menu" role="listbox"></ul>
      </div></div>
      <div class="row"><span class="lbl">密码长度</span>
        <input type="text" id="bruteLen" value="4" style="width:80px">
        <label class="check-pill">
          <input type="checkbox" id="bruteExact"> 精确长度（不尝试更短）</label></div>
      <div class="row"><span class="lbl">前缀 / 后缀</span>
        <input type="text" id="brutePrefix" placeholder="前缀" style="width:130px">
        <input type="text" id="bruteSuffix" placeholder="后缀" style="width:130px"></div>
      <div class="hint">说明：<b>密码类型</b>决定使用的字符集（纯数字最快，混合越全越慢）；<b>密码长度</b>默认从 1 试到该长度，勾选「精确长度」则只试这一长度；已知开头/结尾时填<b>前缀/后缀</b>可成倍缩小范围。字符集与长度越大，耗时指数级增长。</div>
      <div class="row"><button class="primary" onclick="start('brute')">开始穷举破解</button></div>
    </div>
  </div>

  <!-- 字典破解 -->
  <div class="panel" data-tab="dict">
    <div class="card">
      <div class="panel-head"><span class="ph-title">字典破解</span><button class="help" onclick="showHelp('dict')">?</button></div>
      <div class="grid">
        <span class="lbl">字典文件</span>
        <input type="text" id="dictPath" placeholder="每行一个密码的 txt">
        <button class="pick" onclick="pick('dict')">选择字典</button>
      </div>
      <div class="hint">说明：字典是 <b>.txt 文件，每行一个候选密码</b>。程序会并行逐行尝试，命中即用。字典越贴近目标（如社工库、生日、姓名）命中越快；与「穷举」相比更适合有规律的弱口令。</div>
      <div class="row" style="margin-top:9px"><button class="primary" onclick="start('dict')">开始字典破解</button></div>
    </div>
  </div>

  <!-- 掩码攻击 -->
  <div class="panel" data-tab="mask">
    <div class="card">
      <div class="panel-head"><span class="ph-title">掩码攻击</span><button class="help" onclick="showHelp('mask')">?</button></div>
      <div class="grid">
        <span class="lbl">掩码</span>
        <input type="text" id="maskPattern" placeholder="例如 a?d?d?d?d 或 admin?d?d">
        <button class="primary" onclick="start('mask')">开始掩码破解</button>
      </div>
      <div class="hint">占位符：<b>?l</b> 小写 <b>?u</b> 大写 <b>?d</b> 数字 <b>?s</b> 符号 <b>?a</b> 全部；其余按字面。</div>
    </div>
  </div>

  <!-- 混合攻击 -->
  <div class="panel" data-tab="hybrid">
    <div class="card">
      <div class="panel-head"><span class="ph-title">混合攻击</span><button class="help" onclick="showHelp('hybrid')">?</button></div>
      <div class="grid">
        <span class="lbl">字典文件</span>
        <input type="text" id="hybridDict" placeholder="基础词表 txt">
        <button class="pick" onclick="pick('hybrid')">选择字典</button>
      </div>
      <div class="row"><span class="lbl">掩码</span>
        <input type="text" id="hybridMask" placeholder="例如 ?d?d?d" style="width:160px">
        <span class="lbl">拼接位置</span>
        <div class="cdd" id="hybridPos" data-value="suffix">
          <button class="cdd-trigger" type="button" aria-haspopup="listbox">
            <span class="cdd-label">词 + 掩码</span>
            <svg class="cdd-arrow" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
          </button>
          <div class="cdd-menu" role="listbox">
            <div class="cdd-item sel" data-v="suffix">词 + 掩码</div>
            <div class="cdd-item" data-v="prefix">掩码 + 词</div>
          </div>
        </div></div>
      <div class="hint">说明：对字典里<b>每个词</b>拼接掩码生成的片段。例：词 <code>abc</code> + 掩码 <code>?d?d?d</code> → <code>abc000</code>…<code>abc999</code>。<b>拼接位置</b>选择掩码在词前还是词后。</div>
      <div class="row"><button class="primary" onclick="start('hybrid')">开始混合攻击</button></div>
    </div>
  </div>

  <!-- 数字规律 -->
  <div class="panel" data-tab="numeric">
    <div class="card">
      <div class="panel-head"><span class="ph-title">数字规律</span><button class="help" onclick="showHelp('numeric')">?</button></div>
      <div class="checks-card">
        <label><input type="checkbox" id="numYears" checked> 年份 (1900-2099)</label>
        <label><input type="checkbox" id="numDates" checked> 完整日期</label>
        <label><input type="checkbox" id="numRepeats" checked> 重复数字</label>
        <label><input type="checkbox" id="numSeq" checked> 顺子/数列</label>
        <label><input type="checkbox" id="numShort" checked> 4位短数字</label>
      </div>
      <div class="hint">说明：勾选要尝试的<b>数字规律</b>。年份=出生/注册年；完整日期=月日年等组合；重复数字=8888/666666；顺子/数列=123456/123123；4位短数字=0000-9999。只勾需要的可大幅提速。</div>
      <div class="row"><button class="primary" onclick="start('numeric')">开始数字规律破解</button></div>
    </div>
  </div>

  <!-- 常见密码库 -->
  <div class="panel" data-tab="common">
    <div class="card">
      <div class="panel-head"><span class="ph-title">常见密码库</span><button class="help" onclick="showHelp('common')">?</button></div>
      <div class="hint">内置数百条最常见弱口令（中英文 + 数字 + 键盘规律），无需字典。</div>
      <div class="row" style="margin-top:9px"><button class="primary" onclick="start('common')">开始常见密码破解</button></div>
    </div>
  </div>

  <!-- 组合攻击 -->
  <div class="panel" data-tab="combinator">
    <div class="card">
      <div class="panel-head"><span class="ph-title">组合攻击</span><button class="help" onclick="showHelp('combinator')">?</button></div>
      <div class="grid"><span class="lbl">字典 A</span>
        <input type="text" id="comb1" placeholder="第一段词表"><button class="pick" onclick="pick('comb1')">选择</button></div>
      <div class="grid"><span class="lbl">字典 B</span>
        <input type="text" id="comb2" placeholder="第二段词表"><button class="pick" onclick="pick('comb2')">选择</button></div>
      <div class="row"><span class="lbl">连接符</span><input type="text" id="combSep" style="width:100px" placeholder="可留空"></div>
      <div class="hint">说明：把<b>字典 A 的每一行</b>与<b>字典 B 的每一行</b>拼接（A+B），适合「姓名+年份」「单词+数字」。<b>连接符</b>可填如 <code>-</code> 或 <code>@</code>，留空则直接相连。每表最多取 4000 行以保证速度。</div>
      <div class="row"><button class="primary" onclick="start('combinator')">开始组合攻击</button></div>
    </div>
  </div>

  <!-- 规则变形 -->
  <div class="panel" data-tab="rules">
    <div class="card">
      <div class="panel-head"><span class="ph-title">规则变形</span><button class="help" onclick="showHelp('rules')">?</button></div>
      <div class="grid">
        <span class="lbl">基础字典</span>
        <input type="text" id="rulesPath" placeholder="先选一个字典"><button class="pick" onclick="pick('rules')">选择字典</button>
      </div>
      <div class="checks-card">
        <label><input type="checkbox" id="rulesCase" checked> 大小写变形</label>
        <label><input type="checkbox" id="rulesSuffix" checked> 追加常见后缀</label>
        <label><input type="checkbox" id="rulesDigits" checked> 追加 00-99</label>
        <label><input type="checkbox" id="rulesPrefix"> 前缀形式</label>
      </div>
      <div class="hint">说明：对基础字典做常见<b>人工变形</b>以扩大命中。大小写=<code>Password/PASSWORD</code>；常见后缀=<code>123/!/520/</code> 等；追加 00-99=<code>abc00</code>…<code>abc99</code>；前缀形式=把数字等放到词前面。可多选叠加。</div>
      <div class="row"><button class="primary" onclick="start('rules')">开始规则变形破解</button></div>
    </div>
  </div>

  <!-- 明文攻击 -->
  <div class="panel" data-tab="plaintext">
    <div class="card">
      <div class="panel-head"><span class="ph-title">明文攻击（已知明文）</span><button class="help" onclick="showHelp('plaintext')">?</button></div>
      <div class="grid"><span class="lbl">已知明文文件</span>
        <input type="text" id="plainFile" placeholder="包内某文件的未加密副本 / 已知内容片段"><button class="pick" onclick="pick('plain')">选择</button></div>
      <div class="grid"><span class="lbl">包内文件名</span>
        <input type="text" id="plainEntry" placeholder="上述明文对应的压缩包内条目名，如 readme.txt"><span></span></div>
      <div class="grid"><span class="lbl">bkcrack 路径</span>
        <input type="text" id="bkPath" placeholder="留空则自动查找（PATH/程序目录/桌面）"><button class="pick" onclick="pick('bkcrack')">选择</button></div>
      <div class="hint">仅适用于传统 <b>ZipCrypto</b> 加密（非 AES）。成功后可无视密码直接还原全部文件。</div>
      <div class="row" style="margin-top:9px"><button class="primary" onclick="start('plaintext')">开始明文攻击</button></div>
    </div>
  </div>

  <!-- 密钥直解 -->
  <div class="panel" data-tab="keydecrypt">
    <div class="card">
      <div class="panel-head"><span class="ph-title">密钥直解（已知内部密钥）</span><button class="help" onclick="showHelp('keydecrypt')">?</button></div>
      <div class="row"><span class="lbl">内部密钥</span>
        <input type="text" id="key1" placeholder="X (8位十六进制)" style="width:150px">
        <input type="text" id="key2" placeholder="Y (8位十六进制)" style="width:150px">
        <input type="text" id="key3" placeholder="Z (8位十六进制)" style="width:150px"></div>
      <div class="grid"><span class="lbl">bkcrack 路径</span>
        <input type="text" id="bkPath2" placeholder="留空则自动查找（PATH/程序目录/桌面）"><button class="pick" onclick="pick('bkcrack2')">选择</button></div>
      <div class="hint">仅适用于传统 <b>ZipCrypto</b> 加密。用密钥（<code>-U</code>）还原，保留原始压缩流，解出内容与原始逐字节一致。</div>
      <div class="row" style="margin-top:9px"><button class="primary" onclick="start('keydecrypt')">开始密钥直解</button></div>
    </div>
  </div>

  <!-- 密码生成 -->
  <div class="panel" data-tab="gen">
    <div class="card">
      <div class="panel-head"><span class="ph-title">密码生成</span><button class="help" onclick="showHelp('gen')">?</button></div>
      <div class="row"><span class="lbl">密码类型</span><div class="csel" id="csel-genType">
        <input type="hidden" id="genType" value="1">
        <button type="button" class="csel-trigger" id="csel-genType-trigger" aria-haspopup="listbox" aria-expanded="false">
          <span class="csel-icon" id="csel-genType-icon"></span>
          <span class="csel-label" id="csel-genType-label">仅数字</span>
          <svg class="csel-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <ul class="csel-menu" id="csel-genType-menu" role="listbox"></ul>
      </div></div>
      <div class="row"><span class="lbl">长度范围</span>
        <input type="text" id="genMin" value="1" style="width:60px">
        <span style="color:var(--sub)">~</span>
        <input type="text" id="genMax" value="4" style="width:60px">
        <span style="color:var(--sub)">数量(0=全部)</span>
        <input type="text" id="genQty" value="0" style="width:80px"></div>
      <div class="hint">生成字典并保存为 [保存路径]/生成字典.txt，可再用于字典破解。</div>
      <div class="row" style="margin-top:9px"><button class="primary" onclick="start('gen')">开始生成</button></div>
      <div class="out-preview">
        <div class="op-head">
          <span class="op-title">输出预览</span>
          <span class="op-status" id="genOutStatus">尚未生成</span>
        </div>
        <div class="op-path" id="genOutPath">[保存路径]/生成字典.txt</div>
        <div class="op-meta" id="genOutMeta">点击「开始生成」后此处会显示实际文件位置与生成数量</div>
      </div>
    </div>
  </div>
  </div><!-- /content -->

  <div class="card" style="margin-bottom:0">
    <div class="stat">
      <div class="box"><div class="k">总任务量</div><div class="v" id="stTotal">—</div></div>
      <div class="box"><div class="k">已尝试</div><div class="v" id="stTried">—</div></div>
      <div class="box"><div class="k">速度/秒</div><div class="v" id="stSpeed">—</div></div>
      <div class="box"><div class="k">预计剩余</div><div class="v" id="stEta">—</div></div>
    </div>
    <progress id="prog" value="0" max="100"></progress>
    <div id="log" style="margin-top:10px"></div>
    <div class="foot">
      <button class="danger" id="stopBtn" onclick="apiStop()" disabled>
        <svg viewBox="0 0 16 16"><rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/></svg>停止当前任务</button>
      <span class="spacer"></span>
      <span class="pill" id="modePill"></span>
      <button class="ghost" onclick="copyLogAll()">复制日志</button>
      <button class="ghost" onclick="clearLog()">清空日志</button>
    </div>
  </div>
</div>

<div class="mask" id="mask" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="mh" id="mTitle"></div>
    <div class="mb" id="mBody"></div>
    <div class="mf" id="mFoot"><button class="primary" onclick="closeModal()">确定</button></div>
  </div>
</div>
<div id="toasts"></div>

<script>
let appSettings={accent:'',opacity:100,bgImage:'',bgBlur:0,bgOpacity:100};
const TABS = [
  ["direct","直接解压"],["brute","穷举破解"],["dict","字典破解"],
  ["mask","掩码攻击"],["hybrid","混合攻击"],["numeric","数字规律"],["common","常见密码"],
  ["combinator","组合攻击"],["rules","规则变形"],["plaintext","明文攻击"],
  ["keydecrypt","密钥直解"],["gen","密码生成"]
];
const TYPE_LABELS = """ + json.dumps(PASSWORD_TYPE_LABELS) + r""";
const HELP = {
  direct:["直接解压","已知密码时直接解压；压缩包无密码时留空即可。<br>支持 <b>ZIP / 7Z / RAR</b> 三种格式，结果保存到 <code>保存路径/压缩包名/</code>。"],
  brute:["穷举破解","按所选字符集与长度，从短到长尝试所有组合。<br>• <b>密码类型</b>：使用哪些字符（纯数字最快）。<br>• <b>精确长度</b>：只试该长度，不试更短。<br>• <b>前/后缀</b>：已知开头或结尾时填写，可成倍缩小范围。<br>注意：字符集越大、长度越长，耗时呈指数级增长。"],
  dict:["字典破解","逐行尝试字典文件（.txt，每行一个密码）。适合弱口令、社工库导出。字典越贴近目标命中越快。"],
  mask:["掩码攻击","已知密码结构时精准爆破，用占位符表示未知位：<br><code>?l</code> 小写 <code>?u</code> 大写 <code>?d</code> 数字 <code>?s</code> 符号 <code>?a</code> 全部；其它字符按字面匹配。<br>例：<code>admin?d?d</code> = admin 后接两位数字；<code>?u?l?l?l?d?d?d?d</code>。"],
  hybrid:["混合攻击","字典词 + 掩码 组合，对每个字典词拼接掩码生成的片段。<br>例：字典含 <code>abc</code>，掩码 <code>?d?d?d</code> → <code>abc000</code>…<code>abc999</code>。可选择拼在词前或词后。"],
  numeric:["数字规律","针对纯数字密码：生日、日期、手机尾号、重复数字(8888)、顺子(123456)、年份等。按需勾选规律。"],
  common:["常见密码库","内置数百条最常见弱口令（中英文/键盘规律/数字），无需字典，一键尝试。"],
  combinator:["组合攻击","两个词表逐行拼接（A+B），适合「姓名+年份」「单词+数字」。每表最多取 4000 行，可设连接符。"],
  rules:["规则变形","对基础字典做变形：大小写(<code>Password</code>)、追加后缀(<code>123/!/520</code>)、追加 00-99、前缀等，扩展命中率。"],
  plaintext:["明文攻击（已知明文）","针对传统 <b>ZipCrypto</b> 加密（非 AES）的已知明文攻击，依赖开源工具 <b>bkcrack</b>。<br>需要：<br>① 加密压缩包（顶部选择）；<br>② 包内某个文件的一段已知明文，或其未加密副本文件；<br>③ 该文件在压缩包内的条目名称。<br><b>成功后返回 3 个内部密钥</b>，并用密钥（<code>-U</code>）还原——保留原始压缩流，解出的内容与原始<b>逐字节一致</b>，不会破坏结构。若未安装 bkcrack 会提示下载地址。"],
  keydecrypt:["密钥直解","如果你<b>已经拥有 3 个内部密钥</b>（例如之前用明文攻击求出、或他人提供），无需再破解密码，直接填入即可秒还原。<br>原理：ZipCrypto 的密码只是用来推导这 3 个 32 位密钥（X Y Z），密钥才是真正解密的凭据。<br>本模式用 <code>bkcrack -k</code> 直接解密并保留原始压缩结构，解出的文件与原始完全一致，避免了「暴力破出密码→重新压缩后结构不同」的问题。"],
  gen:["密码生成","按字符集与长度范围批量生成字典并保存为 txt，可再用于字典破解。数量填 0 表示生成全部组合。"]
};
const IC = {
  ok:'<svg class="mi-ok" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5L16 9"/></svg>',
  err:'<svg class="mi-err" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
  warn:'<svg class="mi-warn" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l9 16H3z"/><path d="M12 9v5M12 17v.5"/></svg>',
  info:'<svg class="mi-info" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.5"/></svg>'
};
let running=false, systemTheme="dark", themePref="system", bkcrackMissing=false, bkcrackDismissed=false;

function $(id){return document.getElementById(id);}
function val(id){const el=$(id);return el?(el.value||(el.dataset&&el.dataset.value)||""):"";}

function buildUI(){
  const t=$('tabs');
  TABS.forEach(([k,name],i)=>{
    const d=document.createElement('div');
    d.className='tab'+(i===0?' active':'');
    d.textContent=name; d.dataset.tab=k; d.onclick=()=>selectTab(k);
    t.appendChild(d);
  });
  const cselInfo = '字符集越全、长度越大，耗时指数级增长。建议先用「数字」缩小范围，再逐步加字母。';
  const cselGroups = [
    { title:'基础',
      items:[
        {v:1, l:'仅数字',           icon:iconChip('123')},
        {v:2, l:'仅小写字母',       icon:iconChip('abc')},
        {v:3, l:'仅大写字母',       icon:iconChip('ABC')},
      ]},
    { title:'组合',
      items:[
        {v:4, l:'数字 + 小写字母',  icon:iconChip('a1')},
        {v:5, l:'数字 + 大写字母',  icon:iconChip('A1')},
        {v:6, l:'数字 + 大小写字母',icon:iconChip('Aa1')},
        {v:7, l:'所有字符（含特殊符号）', icon:iconChip('#@!')},
      ]},
  ];
  initCustomSelect('bruteType', null, {info:cselInfo, groups:cselGroups});
  initCustomSelect('genType',   null, {info:cselInfo, groups:cselGroups});
  selectTab('direct');
}

/* 自定义下拉：图标 + 标签，保留同名隐藏 input 供旧逻辑读取 */
function iconChip(text){
  return '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">'
    + '<rect x="1.5" y="1.5" width="21" height="21" rx="5.5" '
    + 'fill="currentColor" fill-opacity="0.12" stroke="currentColor" stroke-width="1.4"/>'
    + '<text x="12" y="15.2" text-anchor="middle" font-size="9" font-weight="700" '
    + 'font-family="ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif" '
    + 'fill="currentColor">'+text+'</text></svg>';
}
function initCustomSelect(id, options, extras){
  const wrap  = $('csel-'+id);
  const input = $(id);
  const trig  = $('csel-'+id+'-trigger');
  const lbl   = $('csel-'+id+'-label');
  const icn   = $('csel-'+id+'-icon');
  const menu  = $('csel-'+id+'-menu');
  if(!wrap||!input||!trig||!menu) return;
  extras = extras || {};

  function render(){
    let html = '';
    if(extras.info) html += '<li class="csel-info">'+extras.info+'</li>';
    if(extras.groups && extras.groups.length){
      extras.groups.forEach((g, gi)=>{
        if(gi>0) html += '<li class="csel-sep"></li>';
        if(g.title) html += '<li class="csel-group">'+g.title+'</li>';
        g.items.forEach(o=>{ html += renderOpt(o); });
      });
    } else {
      options.forEach(o=>{ html += renderOpt(o); });
    }
    menu.innerHTML = html;
    sync();
  }
  function renderOpt(o){
    return '<li class="csel-option" role="option" data-value="'+o.v+'" aria-selected="false">'
      + '<span class="opt-text">'+o.l+'</span>'
      + '<svg class="opt-check" viewBox="0 0 24 24" width="14" height="14" fill="none" '
      + 'stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">'
      + '<polyline points="20 6 9 17 4 12"/></svg>'
      + '</li>';
  }
  function sync(){
    const allOptions = (extras.groups && extras.groups.length)
      ? [].concat(...extras.groups.map(g=>g.items))
      : options;
    const cur = allOptions.find(o => String(o.v)===String(input.value)) || allOptions[0];
    if(!cur) return;
    lbl.textContent = cur.l;
    if(icn) icn.innerHTML = cur.icon || '';
    menu.querySelectorAll('.csel-option').forEach(li=>{
      const on = String(li.dataset.value)===String(cur.v);
      li.classList.toggle('active', on);
      li.setAttribute('aria-selected', on?'true':'false');
    });
  }
  function open(o){
    document.querySelectorAll('.csel-menu.show').forEach(m=>{
      if(m!==menu) close(m.previousElementSibling.previousElementSibling.id);
    });
    menu.classList.toggle('show', o);
    trig.classList.toggle('open', o);
    trig.setAttribute('aria-expanded', o?'true':'false');
  }
  trig.addEventListener('click', e=>{
    e.stopPropagation();
    const isOpen = menu.classList.contains('show');
    open(!isOpen);
    trig.focus();
  });
  menu.addEventListener('click', e=>{
    const li = e.target.closest('.csel-option');
    if(!li) return;
    input.value = li.dataset.value;
    sync();
    open(false);
    trig.focus();
    input.dispatchEvent(new Event('change',{bubbles:true}));
  });
  trig.addEventListener('keydown', e=>{
    const open_ = menu.classList.contains('show');
    if(e.key==='ArrowDown'||e.key==='ArrowUp'||(!open_&&(e.key==='Enter'||e.key===' '))){
      e.preventDefault();
      if(!open_){ open(true); return; }
      const items = [...menu.querySelectorAll('.csel-option')];
      const idx = items.findIndex(li=>li.classList.contains('active'));
      const next = e.key==='ArrowDown'
        ? items[(idx+1)%items.length]
        : items[(idx-1+items.length)%items.length];
      input.value = next.dataset.value; sync();
    } else if(e.key==='Enter'||e.key===' '){
      if(open_){ e.preventDefault(); open(false); }
    } else if(e.key==='Escape'){
      open(false);
    }
  });
  document.addEventListener('click', e=>{
    if(!wrap.contains(e.target)) open(false);
  });

  render();
  const _first = (extras.groups && extras.groups.length && extras.groups[0].items[0])
    || (options && options[0]);
  input.value = String(input.value || (_first && _first.v));
  sync();
}
function selectTab(k){
  document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('active',e.dataset.tab===k));
  document.querySelectorAll('.panel').forEach(e=>e.classList.toggle('active',e.dataset.tab===k));
  // 只要 bkcrack 未安装，每次进入相关标签页都弹提示（便于随时安装）
  if(bkcrackMissing && (k==='plaintext'||k==='keydecrypt')) showBkcrackModal();
}
function setBkcrackDismissed(v){
  bkcrackDismissed = !!v;
  // 不移除 ⚠ 角标：未安装 bkcrack 时标签上始终保留，点击 ⚠ 可随时安装
}
function setBkcrackMissing(v){
  bkcrackMissing=v;
  document.querySelectorAll('.tab').forEach(e=>{
    const k=e.dataset.tab, need=(k==='plaintext'||k==='keydecrypt');
    let b=e.querySelector('.warn-badge');
    if(v&&need){ if(!b){ b=document.createElement('span'); b.className='warn-badge';
      b.textContent='⚠'; b.title='依赖 bkcrack 未安装，点击安装'; b.style.cursor='pointer'; b.onclick=()=>showBkcrackModal(); e.appendChild(b);} }
    else if(b){ b.remove(); }
  });
}
function showBkcrackModal(){
  showModal('bkcrack 未安装','warn',
    '<div>明文攻击 / 密钥直解 依赖开源工具 <b>bkcrack</b>，当前未安装。</div>'+
    '<div style="margin-top:6px;color:var(--sub)">点击下方「安装」可联网自动下载并安装；或点「确定」仅关闭本次提示（每次进入该功能都会再次提示，也可在设置-模块中安装）。</div>'+
    '<div class="modal-btns">'+
      '<button class="primary" onclick="installBkcrack()">安装 bkcrack</button>'+
      '<button onclick="skipBkcrack()">确定</button>'+
    '</div>',
    false);
}
function skipBkcrack(){
  // 仅关闭本次提示；bkcrack 未安装时下次进入相关标签仍会弹出
  closeModal();
}
function installBkcrack(){
  // 不另开窗口：直接在当前 modal 里展示进度，避免双 webview 事件循环死锁
  closeModal();
  try { window.pywebview.api.installBkcrackInplace(); }
  catch(e){ alert('调用安装失败：'+(e&&e.message?e.message:e)); }
}
function openBkcrackInstallLater(){
  // 「稍后安装」按钮：从设置/再次进入 tab 时仍可触发，不修改持久化状态
  try { window.pywebview.api.installBkcrackInplace(); }
  catch(e){ alert('调用安装失败：'+(e&&e.message?e.message:e)); }
}
function applyTheme(){
  const eff=(themePref==='system')?systemTheme:themePref;
  document.body.classList.toggle('dark', eff==='dark');
  document.querySelectorAll('#themeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.th===themePref));
  const map={system:'跟随系统',light:'浅色',dark:'深色'};
  $('modePill').textContent='主题：'+map[themePref]+'（当前 '+(eff==='dark'?'深色':'浅色')+'）';
  applyPanelAlpha(appSettings.opacity||100);
  applyBgLayer(); // 主题切换后重算兜底背景渐变（深色不再死黑）
}
function setTheme(p){ themePref=p; applyTheme(); try{window.pywebview.api.setTheme(p);}catch(e){} }
function setCores(list,def,autoN,allN,physical,logical){
  const dd=$('coresDD');
  const menu=dd.querySelector('.cdd-menu');
  const label=dd.querySelector('.cdd-label');
  menu.innerHTML='';
  const cur=(def==null)?'auto':String(def);
  const ht = physical < logical;  // 存在超线程：逻辑核多于物理核
  // 本机检测结果信息行
  let info = '本机检测：物理 '+physical+' 核 / 逻辑 '+logical+' 核';
  if(ht) info += '（超线程对 AES 解密无效，建议选物理核）';
  else info += '（无超线程，所有核心均有效）';
  menu.insertAdjacentHTML('beforeend','<div class="cdd-info">'+info+'</div>');
  // 推荐分组
  menu.insertAdjacentHTML('beforeend','<div class="cdd-group">推荐</div>');
  const auto=document.createElement('div');
  auto.className='cdd-item'+(cur==='auto'?' sel':'');
  auto.dataset.v='auto';
  auto.textContent='自动（推荐 '+autoN+' 核'+(ht?' · 物理核 AES 最优':'')+'）';
  if(cur==='auto') label.textContent=auto.textContent;
  menu.appendChild(auto);
  menu.insertAdjacentHTML('beforeend','<div class="cdd-sep"></div>');
  // 手动分组
  menu.insertAdjacentHTML('beforeend','<div class="cdd-group">手动指定</div>');
  for(let i=1;i<=list;i++){
    const d=document.createElement('div');
    d.className='cdd-item'+(cur===String(i)?' sel':'');
    d.dataset.v=String(i);
    let tag='';
    if(ht && i>physical) tag=' · 超线程（AES 无效）';
    else if(ht && i===physical) tag=' · 物理核';
    d.textContent=i+' 核'+tag;
    if(cur===String(i)) label.textContent=d.textContent;
    menu.appendChild(d);
  }
  // 全部
  menu.insertAdjacentHTML('beforeend','<div class="cdd-sep"></div>');
  const all=document.createElement('div');
  all.className='cdd-item'+(cur==='all'?' sel':'');
  all.dataset.v='all';
  all.textContent='全部（'+allN+' 核'+(ht?' · 含超线程 AES 无效':'')+' · 留 1 核保流畅）';
  if(cur==='all') label.textContent=all.textContent;
  menu.appendChild(all);
  dd.dataset.value=cur;
}
function getCoresVal(){ return $('coresDD').dataset.value||'auto'; }
function initCDD(){
  document.querySelectorAll('.cdd').forEach(dd=>{
    const trigger=dd.querySelector('.cdd-trigger');
    const menu=dd.querySelector('.cdd-menu');
    if(!trigger||!menu) return;
    trigger.addEventListener('click',e=>{
      e.stopPropagation();
      document.querySelectorAll('.cdd.open').forEach(o=>{if(o!==dd)o.classList.remove('open');});
      dd.classList.toggle('open');
    });
    menu.addEventListener('click',e=>{
      const item=e.target.closest('.cdd-item');
      if(!item) return;
      dd.dataset.value=item.dataset.v;
      dd.querySelector('.cdd-label').textContent=item.textContent;
      menu.querySelectorAll('.cdd-item.sel').forEach(o=>o.classList.remove('sel'));
      item.classList.add('sel');
      dd.classList.remove('open');
    });
  });
  document.addEventListener('click',()=>{
    document.querySelectorAll('.cdd.open').forEach(o=>o.classList.remove('open'));
  });
}
function collect(){
  return {
    zipPath:val('zipPath'), savePath:val('savePath'), cores:getCoresVal(),
    directPwd:val('directPwd'),
    bruteType:parseInt(val('bruteType')), bruteLen:val('bruteLen'),
    bruteExact:$('bruteExact').checked, brutePrefix:val('brutePrefix'), bruteSuffix:val('bruteSuffix'),
    dictPath:val('dictPath'),
    maskPattern:val('maskPattern'),
    hybridDict:val('hybridDict'), hybridMask:val('hybridMask'), hybridPos:val('hybridPos'),
    num:{years:$('numYears').checked,dates:$('numDates').checked,repeats:$('numRepeats').checked,
         seq:$('numSeq').checked,short:$('numShort').checked},
    comb1:val('comb1'), comb2:val('comb2'), combSep:val('combSep'),
    rulesPath:val('rulesPath'), rulesCase:$('rulesCase').checked, rulesSuffix:$('rulesSuffix').checked,
    rulesDigits:$('rulesDigits').checked, rulesPrefix:$('rulesPrefix').checked,
    plainFile:val('plainFile'), plainEntry:val('plainEntry'), bkPath:val('bkPath'),
    key1:val('key1'), key2:val('key2'), key3:val('key3'), bkPath2:val('bkPath2'),
    genType:parseInt(val('genType')), genMin:val('genMin'), genMax:val('genMax'), genQty:val('genQty')
  };
}
function log(msg,lvl){
  const d=document.createElement('div');
  d.className='log-'+(lvl||'info');
  const ts=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  d.textContent='['+ts+'] '+msg;
  const box=$('log'); box.appendChild(d); box.scrollTop=box.scrollHeight;
}
function start(method){
  const p=collect(); p.method=method;
  resetStats();
  /* 密码生成时把输出预览切到「生成中」状态 */
  if(method==='gen'){
    const st=$('genOutStatus'), me=$('genOutMeta');
    if(st){ st.textContent='生成中…'; st.className='op-status'; }
    if(me) me.textContent='正在按所选字符集与长度范围生成密码字典…';
  }
  window.pywebview.api.start(JSON.stringify(p));
}
function apiStop(){ window.pywebview.api.stop(); log('正在停止…','warn'); }
function clearLog(){ $('log').innerHTML=''; }
function resetStats(){ ['stTotal','stTried','stSpeed','stEta'].forEach(i=>$(i).textContent='—');
  const pr=$('prog'); pr.value=0; pr.max=100; }
function setRunning(on){
  running=on;
  $('stopBtn').disabled=!on;
  document.querySelectorAll('.card button.primary').forEach(b=>b.disabled=on);
}
function onProgress(tried,total,speed,eta){
  $('stTried').textContent=fmt(tried);
  $('stSpeed').textContent=fmt(speed);
  $('stEta').textContent=(total&&eta<1e9)?fmtTime(eta):'计算中…';
  $('stTotal').textContent=total?fmt(total):'未知';
  const pr=$('prog');
  if(total){ pr.max=total; pr.value=Math.min(tried,total); }
  else { pr.removeAttribute('value'); }
}
function onStatus(txt){ $('fileStatus').textContent=txt; }
function onLog(msg,lvl){ log(msg,lvl); }
function onStart(msg){ toast(msg,'info'); log(msg,'status'); }
function onFound(pwd,path,tried,secs){
  log('★ 破解成功！密码：'+pwd,'ok'); log('文件已解压到：'+path,'ok');
  setRunning(false);
  showModal('破解成功','ok',
    '<b>密码：</b><code>'+esc(pwd)+'</code><br><b>尝试次数：</b>'+fmt(tried)+' 次<br><b>耗时：</b>'+secs+' 秒<br><b>解压目录：</b><br>'+esc(path));
  toast('破解成功！密码：'+pwd,'ok');
}
function onResult(msg,lvl){
  log(msg,lvl||'err'); setRunning(false);
  const k=(lvl==='ok')?'ok':(lvl==='warn'?'warn':'err');
  showModal(k==='ok'?'完成':(k==='warn'?'提示':'未成功'),k,esc(msg));
  toast(msg,k);
}
function onGen(count,path){
  log('生成完成！共 '+fmt(count)+' 个密码 → '+path,'ok'); setRunning(false);
  showModal('生成完成','ok','已生成 <b>'+fmt(count)+'</b> 个密码<br>保存到：<br>'+esc(path));
  toast('字典生成完成','ok');
  /* 写入输出预览卡（让面板下方有明确 UI） */
  const st=$('genOutStatus'), pa=$('genOutPath'), me=$('genOutMeta');
  if(pa) pa.textContent = path;
  if(st){ st.textContent='已完成 · '+fmt(count)+' 个'; st.className='op-status ok'; }
  if(me) me.textContent = '生成时间：'+new Date().toLocaleTimeString()+'  ·  文件可立即用于字典破解';
}
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function fmt(n){ if(n==null) return '—'; n=Number(n); if(!isFinite(n)) return '—';
  if(n>=1e12)return (n/1e12).toFixed(2)+' 万亿'; if(n>=1e8)return (n/1e8).toFixed(2)+' 亿';
  if(n>=1e4)return (n/1e4).toFixed(2)+' 万'; if(n>=1e3)return (n/1e3).toFixed(2)+' 千';
  return Math.round(n).toLocaleString('zh-CN'); }
function fmtTime(s){ s=Number(s); if(!isFinite(s)||s<0)return '计算中…';
  if(s<60)return s.toFixed(1)+' 秒'; let m=Math.floor(s/60),r=s%60;
  if(m<60)return m+' 分 '+Math.floor(r)+' 秒'; let h=Math.floor(m/60),mm=m%60;
  if(h<24)return h+' 时 '+mm+' 分'; let d=Math.floor(h/24),hh=h%24; return d+' 天 '+hh+' 时'; }

function showModal(title,kind,bodyHtml,showDefaultButton=true,modalClass=''){
  $('mTitle').innerHTML=(IC[kind]||IC.info)+'<span>'+esc(title)+'</span>';
  $('mBody').innerHTML=bodyHtml;
  $('mFoot').style.display=showDefaultButton?'':'none';
  var mh=$('mTitle'); if(mh) mh.style.display='';
  document.querySelector('#mask .modal').className='modal '+(modalClass||'');
  $('mask').classList.add('show');
}
function closeModal(){
  $('mask').classList.remove('show');
  $('mFoot').style.display='';
}

// —— bkcrack 安装进度弹窗（避免额外开 webview 窗口造成事件循环死锁） ——
function addDepLog(msg, cls){
  var box = document.getElementById('depLog');
  if(!box) return;
  var line = document.createElement('div');
  line.className = 'log-line ' + (cls||'log-info');
  line.textContent = msg;
  box.appendChild(line);
  // 自动滚动到最新
  box.scrollTop = box.scrollHeight;
}
function showBkcrackProgress(){
  showModal('正在安装 bkcrack','info',
    '<div style="margin-bottom:8px;">首次安装需要联网下载开源工具 <b>bkcrack</b>，请稍候…（若网络受限下载失败，可复制下方日志中的下载链接手动安装）</div>'+
    '<div id="depLog" class="deplog"></div>', false);
}
function bkInstallFinished(ok, url){
  // 在弹窗里加一行结果，再提供关闭 / 手动下载按钮
  var okText = ok ? '<b style="color:var(--ok)">✓ 安装成功</b>，明文 / 密钥攻击已可用。'
                  : '<b style="color:var(--err)">× 安装失败</b>，可稍后再试或手动放置 bkcrack.exe 到程序目录。';
  addDepLog(ok ? '[OK] 安装成功' : '[X] 安装失败', ok?'log-ok':'log-err');
  $('mFoot').style.display='';
  if(ok){
    $('mFoot').innerHTML = '<button onclick="closeModal()" autofocus>关闭</button>';
  } else {
    var linkHtml = url
      ? '<div id="bkDlBox" style="margin:0 0 10px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:rgba(255,255,255,.04);text-align:left;">'+
        '<div style="font-size:12px;color:var(--sub);margin-bottom:6px;">下载失败？复制下方链接用浏览器手动下载，解压出 <code>bkcrack.exe</code> 放到本程序目录或桌面即可：</div>'+
        '<div id="bkDlUrl" style="font-size:12px;word-break:break-all;color:var(--accent);margin-bottom:8px;">'+esc(url)+'</div>'+
        '<button class="primary" onclick="copyTextToClipboard(document.getElementById(\'bkDlUrl\').textContent)">复制下载链接</button> '+
        '<button onclick="openUrl(document.getElementById(\'bkDlUrl\').textContent)">打开下载页</button>'+
        '</div>'
      : '';
    $('mFoot').innerHTML = linkHtml + '<button onclick="closeModal()">关闭</button>';
  }
  $('mBody').insertAdjacentHTML('afterbegin', '<div style="margin-bottom:8px;">'+okText+'</div>');
}
function openUrl(u){
  try{ window.pywebview.api.openUrl(u); }catch(e){ copyTextToClipboard(u); }
}
function showHelp(tab){ const h=HELP[tab]; if(h) showModal(h[0],'info',h[1]); }
function toast(msg,kind){
  const t=document.createElement('div'); t.className='toast '+(kind||'');
  t.innerHTML=(IC[kind]||IC.info)+'<span>'+esc(msg)+'</span>';
  $('toasts').appendChild(t);
  setTimeout(()=>{ t.style.opacity='0'; t.style.transform='translateY(-10px)'; t.style.transition='opacity .3s, transform .3s'; setTimeout(()=>t.remove(),300); }, 3800);
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeModal(); });
// 复制文本到系统剪贴板：优先走 Python 后端（ctypes 直接写 Windows 剪贴板，最可靠）。
// WebView2 本地上下文禁用了 JS 剪贴板 API，所以 JS 仅作兜底。
function copyTextToClipboard(text){
  if(text==null) text='';
  text=String(text);
  if(!text){ toast('没有可复制的内容','warn'); return; }
  try{
    var ret = window.pywebview.api.copyText(text);
    if(ret && typeof ret.then==='function'){
      ret.then(function(r){ if(r && r.ok) toast('已复制 '+text.length+' 个字符','ok'); else copyFallback(text); })
         .catch(function(){ copyFallback(text); });
      return;
    }
  }catch(e){}
  copyFallback(text);
}
function copyFallback(text){
  try{
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(function(){ toast('已复制 '+text.length+' 个字符','ok'); })
        .catch(function(){ execCopy(text); });
      return;
    }
  }catch(e){}
  execCopy(text);
}
function execCopy(text){
  try{
    var ta=document.createElement('textarea');
    ta.value=text; ta.style.position='fixed'; ta.style.left='-9999px'; ta.style.top='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    var ok=document.execCommand('copy'); document.body.removeChild(ta);
    toast(ok?('已复制 '+text.length+' 个字符'):'复制失败，请手动选择文本','err');
  }catch(e){ toast('复制失败，请手动选择文本','err'); }
}
function copyLogAll(){
  var box = document.getElementById('log') || document.getElementById('depLog');
  if(!box){ toast('没有可复制的内容','warn'); return; }
  copyTextToClipboard(box.innerText || box.textContent || '');
}
// 自定义右键菜单样式（WebView2 本地上下文无默认右键菜单，这里给日志区与输入框补上）
(function(){
  var st=document.createElement('style');
  st.textContent=
    '.ctx-menu{--card:var(--card-solid);--card2:var(--card2-solid);position:fixed;z-index:99999;min-width:128px;background:var(--card,#222);color:var(--txt,#eee);'+
    'border:1px solid var(--line,rgba(255,255,255,.12));border-radius:8px;padding:4px;'+
    'box-shadow:0 8px 30px rgba(0,0,0,.5);font-size:13px;}'+
    '.ctx-item{padding:7px 12px;border-radius:6px;cursor:pointer;white-space:nowrap;user-select:none;}'+
    '.ctx-item:hover{background:var(--accent,#4f8cff);color:#fff;}'+
    '.ctx-item.disabled{opacity:.4;cursor:not-allowed;}'+
    '.ctx-sep{height:1px;background:var(--line,rgba(255,255,255,.12));margin:4px 2px;}';
  document.head.appendChild(st);
})();
var ctxMenu=null;
function ensureCtxMenu(){
  if(ctxMenu) return ctxMenu;
  ctxMenu=document.createElement('div');
  ctxMenu.id='ctxMenu'; ctxMenu.className='ctx-menu'; ctxMenu.style.display='none';
  document.body.appendChild(ctxMenu);
  ctxMenu.addEventListener('click', function(e){ e.stopPropagation(); });
  return ctxMenu;
}
function showCtxMenu(x,y,items){
  var m=ensureCtxMenu(); m.innerHTML='';
  items.forEach(function(it){
    if(it.sep){ var s=document.createElement('div'); s.className='ctx-sep'; m.appendChild(s); return; }
    var b=document.createElement('div');
    b.className='ctx-item'+(it.disabled?' disabled':'');
    b.textContent=it.label;
    b.onclick=function(){ hideCtxMenu(); if(!it.disabled && it.onclick) it.onclick(); };
    m.appendChild(b);
  });
  m.style.display='block';
  var w=m.offsetWidth, h=m.offsetHeight;
  if(x+w>window.innerWidth) x=window.innerWidth-w-4;
  if(y+h>window.innerHeight) y=window.innerHeight-h-4;
  if(x<4) x=4; if(y<4) y=4;
  m.style.left=x+'px'; m.style.top=y+'px';
}
function hideCtxMenu(){ if(ctxMenu) ctxMenu.style.display='none'; }
document.addEventListener('click', hideCtxMenu);
document.addEventListener('scroll', hideCtxMenu, true);
function selectAllIn(node){
  try{ var s=window.getSelection(); s.removeAllRanges(); var r=document.createRange(); r.selectNodeContents(node); s.addRange(r); }catch(e){}
}
function insertAtCursor(field, text){
  var s=field.selectionStart||0, en=field.selectionEnd||0;
  field.focus();
  if(field.setRangeText){ field.setRangeText(text, s, en, 'end'); }
  else { field.value=field.value.substring(0,s)+text+field.value.substring(en); field.selectionStart=field.selectionEnd=s+text.length; }
  field.dispatchEvent(new Event('input', {bubbles:true}));
}
function pasteInto(field){
  try{
    var ret=window.pywebview.api.pasteText();
    function apply(r){ if(r && r.ok && r.text!=null){ insertAtCursor(field, r.text); } else { toast(r && r.error?('粘贴失败：'+r.error):'粘贴失败','err'); } }
    if(ret && typeof ret.then==='function'){ ret.then(apply).catch(function(){ toast('粘贴失败','err'); }); }
    else { apply(ret); }
  }catch(e){ toast('粘贴失败','err'); }
}
// 右键菜单：日志区可「复制」，输入框可「复制 / 粘贴 / 剪切」
document.addEventListener('contextmenu', function(e){
  var el=e.target.closest('#log, .deplog');
  if(el){
    e.preventDefault();
    var sel=(window.getSelection()||'').toString();
    showCtxMenu(e.clientX, e.clientY, [
      { label:'全选', onclick:function(){ selectAllIn(el); } },
      { sep:true },
      { label:'复制', onclick:function(){ copyTextToClipboard(sel || (el.innerText||el.textContent||'')); } }
    ]);
    return;
  }
  var field=e.target.closest('input, textarea');
  if(field){
    var t=field.type;
    if(t && (t==='checkbox'||t==='radio'||t==='button'||t==='file'||t==='range'||t==='color'||t==='submit'||t==='hidden')) return;
    e.preventDefault();
    var fs=(field.value||'').substring(field.selectionStart||0, field.selectionEnd||0);
    var hasSel=!!fs;
    showCtxMenu(e.clientX, e.clientY, [
      { label: hasSel?'复制选中':'复制', disabled:!hasSel, onclick:function(){ copyTextToClipboard(fs); } },
      { label:'全选', onclick:function(){ try{ field.select(); }catch(e){} } },
      { sep:true },
      { label:'粘贴', onclick:function(){ pasteInto(field); } },
      { label:'剪切', disabled:!hasSel, onclick:function(){ copyTextToClipboard(fs); field.setRangeText('', field.selectionStart, field.selectionEnd, 'end'); } }
    ]);
    return;
  }
  // 其它区域：保持默认（WebView2 本地上下文默认无菜单，故不拦截）
});

// —— 初始化提示：只有「确定」按钮，点确定写入 initialized=true ——
function showInitModal(){
  showModal('欢迎使用 压缩包密码破解','info',
    '<div style="margin-top:6px;color:var(--sub)">左侧选择压缩包与保存路径，按界面提示操作即可。'+
    '点击右上角 ⚙ 可设置<b>主题色</b>、<b>面板透明度</b>与<b>背景图片</b>。</div>', true);
  // 把默认「确定」按钮改为写入初始化标记
  $('mFoot').innerHTML='<button class="primary" onclick="markInitializedAndClose()">确定</button>';
}
function markInitializedAndClose(){
  try{ window.pywebview.api.markInitialized(); }catch(e){}
  closeModal();
}

// —— 设置面板：左侧菜单栏 + 大页面（分栏） ——
function openSettings(){
  const s=appSettings||{accent:'',opacity:100,bgImage:'',bgBlur:0,bgOpacity:100};
  const accent = s.accent || getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#0067c0';
  const bgUrlVal = (s.bgImage && /^https?:/i.test(s.bgImage)) ? s.bgImage : '';
  const html =
    '<div class="set-shell">'+
      '<div class="set-side">'+
        '<div class="set-side-t">设置</div>'+
        '<button class="set-nav on" data-sec="appearance" onclick="setNav(\'appearance\')">外观</button>'+
        '<button class="set-nav" data-sec="background" onclick="setNav(\'background\')">背景图片</button>'+
        '<button class="set-nav" data-sec="modules" onclick="setNav(\'modules\')">模块 / 依赖</button>'+
      '</div>'+
      '<div class="set-content">'+
        '<div class="set-top">'+
          '<div class="set-title" id="setTitle">外观</div>'+
          '<button class="set-x" onclick="closeModal()" title="关闭">✕</button>'+
        '</div>'+
        '<div class="set-scroll">'+
          // —— 外观 ——
          '<div class="set-pane" id="pane-appearance">'+
            '<div class="set-sec"><div class="set-sec-t">外观</div>'+
              '<div class="set-row"><label>主题色</label>'+
                '<div class="set-ctrl"><input type="color" id="setAccent" value="'+accent+'">'+
                  '<div class="set-swatches">'+
                    swatch('#0067c0')+swatch('#4cc2ff')+swatch('#16a34a')+swatch('#dc2626')+
                    swatch('#9333ea')+swatch('#ea580c')+swatch('#0ea5e9')+swatch('#e11d48')+
                  '</div></div></div>'+
              '<div class="set-row col"><label>面板透明度</label>'+
                '<div class="set-ctrl"><input type="range" id="setOpacity" min="30" max="100" value="'+s.opacity+'" oninput="setOpacityVal.textContent=this.value+\'%\';applyOpacityLive();paintRange(this)">'+
                  '<span class="set-val" id="setOpacityVal">'+s.opacity+'%</span></div>'+
                '<div class="set-hint">数值越低，各功能面板的背景板越透明，越能透出后面的背景图</div></div>'+
            '</div>'+
          '</div>'+
          // —— 背景图片 ——
          '<div class="set-pane" id="pane-background" style="display:none">'+
            '<div class="set-sec"><div class="set-sec-t">背景图片</div>'+
              '<div class="set-row col"><label>上传图片</label>'+
                '<div class="set-ctrl"><label class="set-file"><input type="file" id="setBgFile" accept="image/*" onchange="chooseBgFile(this)"><span>选择本地图片…</span></label></div></div>'+
              '<div class="set-row col"><label>或图片链接</label>'+
                '<div class="set-ctrl"><input type="text" id="setBg" placeholder="粘贴图片链接，如 https://.../bg.jpg" value="'+esc(bgUrlVal)+'">'+
                  '<button class="pick" onclick="applyBgUrl()">应用</button></div></div>'+
              '<div class="set-row"><label>模糊度</label>'+
                '<div class="set-ctrl"><input type="range" id="setBlur" min="0" max="20" value="'+s.bgBlur+'" oninput="setBlurVal.textContent=this.value+\'px\';applyBgLive();paintRange(this)">'+
                  '<span class="set-val" id="setBlurVal">'+s.bgBlur+'px</span></div></div>'+
              '<div class="set-row"><label>图片透明度</label>'+
                '<div class="set-ctrl"><input type="range" id="setBgOp" min="0" max="100" value="'+s.bgOpacity+'" oninput="setBgOpVal.textContent=this.value+\'%\';applyBgLive()">'+
                  '<span class="set-val" id="setBgOpVal">'+s.bgOpacity+'%</span></div></div>'+
              '<div class="set-preview" id="setPreview"></div>'+
              '<button class="set-clear" onclick="clearBg()">清除背景图片</button>'+
            '</div>'+
          '</div>'+
          // —— 模块 / 依赖 ——
          '<div class="set-pane" id="pane-modules" style="display:none">'+
            '<div class="set-sec"><div class="set-sec-t">模块 / 依赖</div>'+
              '<div id="moduleList"></div>'+
            '</div>'+
          '</div>'+
          '<div class="set-foot">'+
            '<button class="primary" onclick="saveSettings()">保存</button>'+
            '<button onclick="closeModal()">取消</button>'+
          '</div>'+
        '</div>'+
      '</div>'+
    '</div>';
  showModal('设置','info', html, false, 'set-big');
  $('mTitle').style.display='none'; // 由分栏内的标题栏接管
  renderBgPreview();
  renderModuleList();
  paintRange($('setOpacity')); paintRange($('setBlur')); paintRange($('setBgOp'));
}
function setNav(sec){
  document.querySelectorAll('.set-nav').forEach(b=>b.classList.toggle('on', b.dataset.sec===sec));
  document.querySelectorAll('.set-pane').forEach(p=>p.style.display=(p.id==='pane-'+sec)?'':'none');
  const titles={appearance:'外观',background:'背景图片',modules:'模块 / 依赖'};
  const t=$('setTitle'); if(t) t.textContent=titles[sec]||'设置';
}
function swatch(c){ return '<span class="set-sw" style="background:'+c+'" onclick="setAccent.value=\''+c+'\'"></span>'; }
function chooseBgFile(input){
  const file=input.files && input.files[0];
  if(!file) return;
  const reader=new FileReader();
  reader.onload=e=>{
    const img=new Image();
    img.onload=()=>{
      let w=img.width, h=img.height; const max=1600;
      if(w>max||h>max){ const r=Math.min(max/w,max/h); w=Math.round(w*r); h=Math.round(h*r); }
      const cv=document.createElement('canvas'); cv.width=w; cv.height=h;
      cv.getContext('2d').drawImage(img,0,0,w,h);
      try{ appSettings.bgImage=cv.toDataURL('image/jpeg',0.82); }
      catch(err){ appSettings.bgImage=e.target.result; }
      if($('setBg')) $('setBg').value='';
      applyBgLayer(); renderBgPreview();
      toast('背景图片已应用','ok');
    };
    img.src=e.target.result;
  };
  reader.readAsDataURL(file);
}
function applyBgUrl(){
  const url=$('setBg').value.trim();
  appSettings.bgImage=url;
  if($('setBgFile')) $('setBgFile').value='';
  applyBgLayer(); renderBgPreview();
}
function applyBgLive(){
  appSettings.bgBlur=parseInt($('setBlur').value,10)||0;
  const op=parseInt($('setBgOp').value,10);
  appSettings.bgOpacity=isNaN(op)?100:op;
  applyBgLayer(); renderBgPreview();
}
function applyOpacityLive(){
  const v=parseInt($('setOpacity').value,10)||100;
  appSettings.opacity=v;
  applyPanelAlpha(v);
  try{ window.pywebview.api.setOpacity(v); }catch(e){}
}
function clearBg(){
  appSettings.bgImage=''; appSettings.bgBlur=0; appSettings.bgOpacity=100;
  if($('setBg')) $('setBg').value='';
  if($('setBgFile')) $('setBgFile').value='';
  if($('setBlur')) $('setBlur').value=0;
  if($('setBgOp')) $('setBgOp').value=100;
  applyBgLayer(); renderBgPreview();
}
function renderBgPreview(){
  const pv=$('setPreview'); if(!pv) return;
  const img=appSettings.bgImage||'';
  if(img){
    pv.innerHTML='<img id="setPreviewImg" src="'+img+'" alt="背景预览">';
    const im=$('setPreviewImg');
    if(im){
      im.style.opacity=(appSettings.bgOpacity!=null?appSettings.bgOpacity:100)/100;
      im.style.filter='blur('+(appSettings.bgBlur||0)+'px)';
    }
  } else {
    pv.innerHTML='<div class="set-preview-empty">未设置背景图片</div>';
  }
}
function renderModuleList(){
  const box=$('moduleList'); if(!box) return;
  let h='';
  // 模块1：Edge WebView2 运行时（必装，不可卸载）
  h+='<div class="mod-row"><div class="mod-info"><div class="mod-name">Edge WebView2 运行时</div>'+
     '<div class="mod-desc">界面渲染依赖 · 程序运行所必需</div></div>'+
     '<div class="mod-state locked">已安装 · 必装 · 不可卸载</div></div>';
  // 模块2：bkcrack（明文攻击 / 密钥直解依赖）
  if(bkcrackMissing){
    h+='<div class="mod-row"><div class="mod-info"><div class="mod-name">bkcrack</div>'+
       '<div class="mod-desc">明文攻击 / 密钥直解 依赖的开源工具</div></div>'+
       '<div class="mod-actions"><button class="primary" onclick="installBkcrack()">安装</button></div></div>';
  } else {
    h+='<div class="mod-row"><div class="mod-info"><div class="mod-name">bkcrack</div>'+
       '<div class="mod-desc">明文攻击 / 密钥直解 依赖的开源工具</div></div>'+
       '<div class="mod-actions">'+
         '<button onclick="reinstallBkcrack()">重装</button>'+
         '<button class="danger" onclick="uninstallBkcrack()">卸载</button>'+
       '</div></div>';
  }
  box.innerHTML=h;
}
let _confirmCb=null;
function askConfirm(title,msg,onYes){
  _confirmCb=onYes;
  showModal(title,'warn',
    '<div>'+msg+'</div>'+
    '<div class="modal-btns">'+
      '<button class="danger" onclick="_runConfirm()">确定</button>'+
      '<button onclick="_cancelConfirm()">取消</button>'+
    '</div>', false);
}
function _runConfirm(){ const f=_confirmCb; _confirmCb=null; closeModal(); if(f) f(); }
function _cancelConfirm(){ _confirmCb=null; closeModal(); }
function uninstallBkcrack(){
  askConfirm('卸载 bkcrack','确定卸载 bkcrack？卸载后明文攻击 / 密钥直解将不可用，直到重新安装。', doUninstallBkcrack);
}
function doUninstallBkcrack(){
  try {
    const r=window.pywebview.api.uninstallBkcrack();
    setBkcrackMissing(true); renderModuleList();
    if(r && r.ok){ toast('bkcrack 已卸载','ok'); }
    else if(r && r.error){ toast('卸载失败：'+r.error,'err'); }
    else { toast('bkcrack 已卸载','ok'); }
  } catch(e){ alert('卸载失败：'+(e&&e.message?e.message:e)); }
}
function reinstallBkcrack(){
  try {
    window.pywebview.api.reinstallBkcrack();
    toast('正在重新安装 bkcrack…','info');
  } catch(e){ alert('重装失败：'+(e&&e.message?e.message:e)); }
}
function paintRange(el){
  if(!el) return;
  const min=parseFloat(el.min)||0, max=parseFloat(el.max)||100, v=parseFloat(el.value)||0;
  const pct=Math.max(0,Math.min(100,((v-min)/(max-min))*100));
  const track='color-mix(in srgb,var(--accent) 22%,var(--line))';
  el.style.background='linear-gradient(90deg,var(--accent) 0%,var(--accent) '+pct+'%,'+track+' '+pct+'%)';
}
function getVar(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
function solidToRgba(hex,a){
  hex=String(hex).replace('#','');
  if(hex.length===3) hex=hex.split('').map(x=>x+x).join('');
  if(hex.length!==6) return '';
  const r=parseInt(hex.slice(0,2),16),g=parseInt(hex.slice(2,4),16),b=parseInt(hex.slice(4,6),16);
  return 'rgba('+r+','+g+','+b+','+(a/100).toFixed(3)+')';
}
function hexToRgb(hex){
  hex=String(hex).replace('#','');
  if(hex.length===3) hex=hex.split('').map(x=>x+x).join('');
  if(hex.length!==6) return null;
  return [parseInt(hex.slice(0,2),16),parseInt(hex.slice(2,4),16),parseInt(hex.slice(4,6),16)];
}
function applyPanelAlpha(v){
  v=Math.max(20,Math.min(100,parseInt(v,10)||100));
  document.documentElement.style.setProperty('--panel-alpha', v);
  // 直接依据 JS 维护的主题状态（与 applyTheme 同源），避免依赖 body class 或 CSS 变量解析的时机：
  // 旧写法从 getComputedStyle(document.body).getPropertyValue('--card-solid') 读取，但初始化时
  // body.dark 尚未加上，会取到白色基准，导致深色模式下右键菜单/弹窗变成白色半透明。
  const eff=(typeof themePref!=='undefined') ? ((themePref==='system')?systemTheme:themePref) : 'dark';
  const dark = eff==='dark';
  const base1 = dark ? '43,43,43' : '255,255,255';
  const base2 = dark ? '51,51,51' : '245,245,245';
  const c1='rgba('+base1+','+(v/100).toFixed(3)+')';
  const c2='rgba('+base2+','+(v/100).toFixed(3)+')';
  const set=(n,val)=>{ document.documentElement.style.setProperty(n,val); document.body.style.setProperty(n,val); };
  set('--card', c1); set('--card2', c2);
  set('--card-rgb', base1); set('--card2-rgb', base2);
}
function saveSettings(){
  const s={
    accent: $('setAccent').value,
    opacity: parseInt($('setOpacity').value,10)||100,
    bgImage: appSettings.bgImage||'',
    bgBlur: parseInt($('setBlur').value,10)||0,
    bgOpacity: parseInt($('setBgOp').value,10)
  };
  if(isNaN(s.bgOpacity)) s.bgOpacity=100;
  appSettings=s;
  applySettings(s);
  try{ window.pywebview.api.saveSettings(s); }catch(e){}
  closeModal();
  toast('设置已保存','ok');
}
function applySettings(s){
  if(!s) return;
  // 主题色：同时写到 documentElement 与 body，确保深色模式下也能覆盖 body.dark 的默认色
  if(s.accent){
    const h=shade(s.accent,12), p=shade(s.accent,-10);
    document.documentElement.style.setProperty('--accent', s.accent);
    document.documentElement.style.setProperty('--accent-hover', h);
    document.documentElement.style.setProperty('--accent-press', p);
    document.body.style.setProperty('--accent', s.accent);
    document.body.style.setProperty('--accent-hover', h);
    document.body.style.setProperty('--accent-press', p);
  }
  applyBgLayer();
  // 面板透明度（毛玻璃效果：让各功能面板背景板半透明、透出背景图）
  if(s.opacity!=null){
    applyPanelAlpha(s.opacity);
    try{ window.pywebview.api.setOpacity(s.opacity); }catch(e){}
  }
}
function applyBgLayer(){
  const el=$('bgLayer'); if(!el) return;
  const img=appSettings.bgImage||'';
  const eff=(themePref==='system')?systemTheme:themePref;
  if(img){
    // 有背景图：毛玻璃透出图片，深色下不再被底色盖成黑
    el.style.backgroundImage='url("'+img+'")';
    el.style.backgroundColor='transparent';
  } else {
    // 无背景图：用主题渐变兜底，深色不再是死黑，浅色不再是一片白
    if(eff==='dark'){
      el.style.backgroundColor='#23262d';
      el.style.backgroundImage='radial-gradient(135% 120% at 50% -10%, #2d323b 0%, #1b1e24 68%)';
    } else {
      el.style.backgroundColor='#f3f3f3';
      el.style.backgroundImage='radial-gradient(135% 120% at 50% -10%, #ffffff 0%, #ececec 72%)';
    }
  }
  el.style.opacity = (appSettings.bgOpacity!=null?appSettings.bgOpacity:100)/100;
  el.style.filter = 'blur('+(appSettings.bgBlur||0)+'px)';
}
function shade(hex, pct){
  // 简单地加亮/变暗：hex -> rgb，按 pct 调整
  hex=String(hex).replace('#','');
  if(hex.length===3) hex=hex.split('').map(c=>c+c).join('');
  if(hex.length!==6) return hex;
  let r=parseInt(hex.slice(0,2),16), g=parseInt(hex.slice(2,4),16), b=parseInt(hex.slice(4,6),16);
  const f=v=>Math.max(0,Math.min(255, Math.round(v + (pct/100)*255)));
  r=f(r); g=f(g); b=f(b);
  return '#'+[r,g,b].map(v=>v.toString(16).padStart(2,'0')).join('');
}

// 选择按钮统一入口：使用完整的 window.pywebview.api（裸 api 在该版本未定义），并兜底报错
function pick(which){
  try {
    window.pywebview.api.choose(which);
  } catch(e){
    console.error('选择调用失败', e);
    alert('选择功能调用失败：' + (e && e.message ? e.message : e));
  }
}

window.addEventListener('pywebviewready', ()=>{
  buildUI();
  initCDD();
  window.pywebview.api.init().then(r=>{
    systemTheme=r.systemTheme||'dark';
    themePref=r.themePref||'system';
    setCores(r.coresMax, r.coresDefault, r.coresAuto, r.coresAll, r.coresPhysical, r.coresLogical);
    if(r.savePath) $('savePath').value=r.savePath;
    if(r.settings) appSettings=Object.assign({accent:'',opacity:100,bgImage:'',bgBlur:0,bgOpacity:100}, r.settings);
    applyTheme();
    applySettings(appSettings);
    if(r.bkcrackMissing) setBkcrackMissing(true);
    setBkcrackDismissed(!!r.bkcrackDismissed);
    if(!r.initialized) showInitModal();
  });
});
</script>
</body>
</html>
"""


# ------------------------------------------------------------------
# Python 后端 API（暴露给 JS）
# ------------------------------------------------------------------
class Api:
    def __init__(self):
        import webview
        self.webview = webview
        self.system_theme = detect_system_theme()
        self.cpu = os.cpu_count() or 1
        self.cores_physical = self._detect_physical_cores()
        self.cores_max = self.cpu
        # 自动（推荐）：取物理核数，但至少给系统留 1 核余量，兼顾速度与流畅
        self.cores_recommended = max(1, min(self.cores_physical, self.cpu - 1))
        # 全部：用除 1 核外的所有逻辑核心，强制留出 1 核给系统；超线程对 AES 无效会在 UI 标注
        self.cores_all = max(1, self.cpu - 1)
        self.cores_default = "auto"
        self.cracker = None
        self.pref = load_pref()
        self.win = None            # 主窗口引用（init 时绑定，供安装回调刷新 UI）

    @staticmethod
    def _detect_physical_cores():
        # 优先用 psutil 取物理核数（超线程下比逻辑核更省且不易争抢）
        try:
            import psutil
            n = psutil.cpu_count(logical=False)
            if n:
                return n
        except Exception:
            pass
        logical = os.cpu_count() or 1
        # 退化策略：假定约一半逻辑核为物理核（超线程常见 2:1）
        return max(1, logical // 2)

    def _emit(self, js):
        try:
            self.webview.windows[0].evaluate_js(js)
        except Exception:
            pass

    def _log(self, msg, lvl="info"):
        self._emit(f"onLog({json.dumps(msg)},{json.dumps(lvl)});")

    def init(self):
        # 必须返回 dict（pywebview 会序列化为 JS 对象）；
        # 若返回 json.dumps 字符串，前端 r.xxx 会全部读到 undefined，导致主题误判为深色。
        try:
            if self.webview.windows:
                self.win = self.webview.windows[0]
        except Exception:
            pass
        # 启用分层窗口（供「主界面透明度」生效），并应用已保存的透明度
        try:
            self._enable_layered()
            self._apply_opacity((self.pref.get("settings") or {}).get("opacity", 100))
        except Exception:
            pass
        return {
            "coresMax": self.cores_max,
            "coresAuto": self.cores_recommended,
            "coresAll": self.cores_all,
            "coresPhysical": self.cores_physical,
            "coresLogical": self.cpu,
            "coresDefault": self.pref.get("cores", self.cores_default),
            "systemTheme": self.system_theme,
            "themePref": self.pref.get("theme", "system"),
            "savePath": get_desktop_dir(),
            "bkcrackMissing": find_bkcrack() is None,
            # 「只第一次检测」的持久化：用户曾在弹窗里选「不使用此功能」为 True
            "bkcrackDismissed": bool(self.pref.get("bkcrack_dismissed", False)),
            "initialized": bool(self.pref.get("initialized", False)),
            "settings": self.pref.get("settings", {}) or {},
        }

    def dismissBkcrackMissing(self, flag):
        """用户点击「不使用此功能」时调用：写入持久化，之后不再弹。"""
        try:
            self.pref["bkcrack_dismissed"] = bool(flag)
            save_pref(self.pref)
        except Exception:
            pass

    def installBkcrack(self):
        """兼容旧入口：直接转发到新的 in-place 安装。"""
        return self.installBkcrackInplace()

    def installBkcrackInplace(self):
        """在主窗口内以异步进度弹窗方式安装 bkcrack，绝不在 webview 事件循环里同步阻塞。
        通过 evaluate_js 推送日志，结果回调 setBkcrackMissing。"""
        if getattr(self, "_installing_bkcrack", False):
            try:
                self.win.evaluate_js("toast('bkcrack 正在安装中，请稍候…','info');")
            except Exception:
                pass
            return
        self._installing_bkcrack = True

        def _js_log(msg, color="info"):
            cls = {"ok": "log-ok", "err": "log-err", "warn": "log-warn",
                   "accent": "log-status"}.get(color, "log-info")
            try:
                self.win.evaluate_js("addDepLog(%s, %s);" % (json.dumps(msg), json.dumps(cls)))
            except Exception:
                pass

        def _run():
            try:
                # 推一个进度弹窗
                try:
                    self.win.evaluate_js(
                        "showBkcrackProgress();"
                    )
                except Exception:
                    pass
                _js_log("开始下载并安装 bkcrack…", "accent")
                # 临时把 _ensure_bkcrack 的 log 接到我们的推送
                p = {"fg": "#616161", "ok": "#0f7b0f", "warn": "#9d5d00",
                     "err": "#c42b1c", "accent": "#0067c0"}

                def log(msg, color=p["fg"]):
                    _js_log(msg, color)

                # _ensure_bkcrack 是同步函数，下载 bkcrack 的 zip 通常几秒到几十秒
                # 用短一点的超时和友好提示即可
                import threading as _t
                done = []
                def _work():
                    try:
                        done.append(_ensure_bkcrack(log, p))
                    except Exception as e:
                        log("安装过程异常：%s" % e, p["err"])
                        done.append(False)
                th = _t.Thread(target=_work, daemon=True)
                th.start()
                th.join(timeout=180)
                if not done:
                    log("安装超时（>180s），请稍后重试。", p["err"])
                    ok = False
                else:
                    ok = bool(done[0])
                try:
                    if ok:
                        self.win.evaluate_js(
                            "bkInstallFinished(true);"
                            "setBkcrackMissing(false);"
                            "renderModuleList();"
                            "toast('bkcrack 安装成功，明文/密钥攻击已可用','ok');"
                        )
                    else:
                        self.win.evaluate_js(
                            "bkInstallFinished(false, %s);" % json.dumps(BKCRACK_URL)
                            + "toast('bkcrack 安装失败，可稍后重试或手动安装','err');"
                        )
                except Exception:
                    pass
            finally:
                self._installing_bkcrack = False

        # 推到后台线程，避免阻塞 webview 事件循环
        threading.Thread(target=_run, daemon=True).start()

    def uninstallBkcrack(self):
        """移除本程序目录 / 桌面 / 工作目录下的 bkcrack.exe（及 bkcrack 子目录中的同名文件）。"""
        try:
            import glob
            removed = False
            for loc in (_app_dir(), os.getcwd(), get_desktop_dir()):
                if not loc:
                    continue
                for name in ("bkcrack.exe", "bkcrack"):
                    for cand in (os.path.join(loc, name),
                                 os.path.join(loc, "bkcrack", name)):
                        if os.path.isfile(cand):
                            try:
                                os.remove(cand)
                                removed = True
                            except Exception:
                                pass
            try:
                self.win.evaluate_js("setBkcrackMissing(true);renderModuleList();")
            except Exception:
                pass
            return {"ok": True, "removed": removed}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reinstallBkcrack(self):
        """先卸载再重新联网安装 bkcrack。"""
        try:
            try:
                self.uninstallBkcrack()
            except Exception:
                pass
            self.installBkcrackInplace()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def copyText(self, text):
        """将文本写入系统剪贴板（Windows）。WebView2 在本地上下文下禁用了 JS 剪贴板，
        因此由 Python 端用 ctypes 直接调用 Win32 API，最可靠。"""
        try:
            text = "" if text is None else str(text)
            if not text:
                return {"ok": True, "empty": True}
            import ctypes
            from ctypes import wintypes
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            kernel32 = ctypes.windll.kernel32
            user32 = ctypes.windll.user32
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = wintypes.HANDLE
            kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalLock.restype = wintypes.LPVOID
            kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
            kernel32.GlobalUnlock.restype = wintypes.BOOL
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.EmptyClipboard.restype = wintypes.BOOL
            user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            user32.SetClipboardData.restype = wintypes.HANDLE
            user32.CloseClipboard.restype = wintypes.BOOL
            data = text.encode("utf-16-le") + b"\x00\x00"
            for _ in range(3):
                hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not hmem:
                    continue
                ptr = kernel32.GlobalLock(hmem)
                ctypes.memmove(ptr, data, len(data))
                kernel32.GlobalUnlock(hmem)
                if user32.OpenClipboard(None):
                    try:
                        user32.EmptyClipboard()
                        if user32.SetClipboardData(CF_UNICODETEXT, hmem):
                            return {"ok": True}
                    finally:
                        user32.CloseClipboard()
                # 若被其它程序占用，稍后重试
                import time as _t
                _t.sleep(0.05)
            return {"ok": False, "error": "clipboard-busy"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def openUrl(self, url):
        """用系统默认浏览器打开一个网址（用于「打开下载页」等）。"""
        try:
            import webbrowser
            webbrowser.open(str(url))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pasteText(self):
        """读取系统剪贴板文本（Windows）。供前端输入框右键「粘贴」使用。
        WebView2 本地上下文禁用了 JS 剪贴板读取，因此由 Python 用 ctypes 读。"""
        try:
            import ctypes
            from ctypes import wintypes
            CF_UNICODETEXT = 13
            user32 = ctypes.windll.user32
            user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
            user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.GetClipboardData.argtypes = [wintypes.UINT]
            user32.GetClipboardData.restype = wintypes.HANDLE
            user32.CloseClipboard.restype = wintypes.BOOL
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return {"ok": True, "text": ""}
            if not user32.OpenClipboard(None):
                return {"ok": False, "error": "clipboard-busy"}
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return {"ok": True, "text": ""}
                text = ctypes.c_wchar_p(h).value or ""
                return {"ok": True, "text": text}
            finally:
                user32.CloseClipboard()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def setTheme(self, pref):
        self.pref["theme"] = pref
        save_pref(self.pref)

    # —— 初始化标记 / 设置（主题色、透明度、背景图） ——
    def markInitialized(self):
        try:
            self.pref["initialized"] = True
            save_pref(self.pref)
        except Exception:
            pass

    def saveSettings(self, settings):
        try:
            s = settings or {}
            cur = self.pref.get("settings", {}) or {}
            cur["accent"] = s.get("accent", cur.get("accent", ""))
            cur["opacity"] = int(s.get("opacity", cur.get("opacity", 100)))
            cur["bgImage"] = s.get("bgImage", cur.get("bgImage", ""))
            try:
                cur["bgBlur"] = int(s.get("bgBlur", cur.get("bgBlur", 0)))
            except Exception:
                cur["bgBlur"] = 0
            try:
                cur["bgOpacity"] = int(s.get("bgOpacity", cur.get("bgOpacity", 100)))
            except Exception:
                cur["bgOpacity"] = 100
            self.pref["settings"] = cur
            save_pref(self.pref)
            self._apply_opacity(cur["opacity"])
        except Exception:
            pass

    def setOpacity(self, v):
        try:
            v = max(20, min(100, int(v)))
        except Exception:
            v = 100
        self._apply_opacity(v)
        try:
            cur = self.pref.get("settings", {}) or {}
            cur["opacity"] = v
            self.pref["settings"] = cur
            save_pref(self.pref)
        except Exception:
            pass

    def _enable_layered(self):
        """给主窗口加 WS_EX_LAYERED 样式，使整体透明度（LWA_ALPHA）生效。"""
        try:
            import ctypes
            hwnd = self.webview.windows[0].hwnd
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x80000
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not (ex & WS_EX_LAYERED):
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        except Exception:
            pass

    def _apply_opacity(self, v):
        """面板透明度改由前端 CSS 变量 --panel-alpha 控制（毛玻璃卡片），
        窗口本身保持不透明，不再做整体 LWA_ALPHA 透明处理。"""
        pass

    def _resolve_cores(self, sel):
        """把前端选择（auto / all / 数字）解析为实际工作进程数。"""
        if sel in ("auto", "自动", None, ""):
            return self.cores_recommended
        if sel in ("all", "全部"):
            # 强制留出 1 核给系统；AES 解密用物理核心更有效
            return self.cores_all
        try:
            n = int(sel)
        except Exception:
            n = self.cores_recommended
        return max(1, min(n, self.cpu))

    # 目标输入框映射
    _CHOOSE_MAP = {
        "zip":     ("zipPath",   "选择压缩包",   [("压缩包 (*.zip;*.7z;*.rar)", "*.zip;*.7z;*.rar"), ("所有文件", "*.*")]),
        "dict":    ("dictPath",  "选择字典文件", [("文本文件 (*.txt)", "*.txt"), ("所有文件", "*.*")]),
        "rules":   ("rulesPath", "选择基础字典", [("文本文件 (*.txt)", "*.txt"), ("所有文件", "*.*")]),
        "hybrid":  ("hybridDict","选择字典文件", [("文本文件 (*.txt)", "*.txt"), ("所有文件", "*.*")]),
        "comb1":   ("comb1",     "选择字典 A",   [("文本文件 (*.txt)", "*.txt"), ("所有文件", "*.*")]),
        "comb2":   ("comb2",     "选择字典 B",   [("文本文件 (*.txt)", "*.txt"), ("所有文件", "*.*")]),
        "plain":   ("plainFile", "选择已知明文文件", [("所有文件", "*.*")]),
        "bkcrack": ("bkPath",    "选择 bkcrack.exe", [("可执行文件 (*.exe)", "*.exe"), ("所有文件", "*.*")]),
        "bkcrack2":("bkPath2",   "选择 bkcrack.exe", [("可执行文件 (*.exe)", "*.exe"), ("所有文件", "*.*")]),
    }

    def choose(self, which):
        """在独立线程弹出原生对话框，避免阻塞 WebView UI 线程。"""
        self._log(f"▶ 选择请求：{which}", "info")
        threading.Thread(target=self._choose_impl, args=(which,), daemon=True).start()

    def _choose_impl(self, which):
        try:
            if which == "save":
                path = self._pick_folder("选择解压保存目录")
                if path:
                    self._emit(f"$('savePath').value={json.dumps(path)};")
                return
            if which not in self._CHOOSE_MAP:
                return
            elem, title, filters = self._CHOOSE_MAP[which]
            path = self._pick_file(title, filters)
            if path:
                self._emit(f"$('{elem}').value={json.dumps(path)};")
                if which == "zip":
                    threading.Thread(target=self._detect, args=(path,), daemon=True).start()
        except Exception as e:
            self._log(f"选择文件出错：{e}", "err")

    def _pick_file(self, title, filters):
        # 只弹一次系统原生对话框：取消直接返回，绝不级联；仅原生真正报错时回退一次 PowerShell
        desktop = get_desktop_dir()
        owner = _owner_hwnd()
        r = None
        try:
            r = _native_open_file(title, filters, desktop, owner)
        except Exception as e:
            self._log(f"系统文件对话框异常：{e}", "warn")
            try:
                r = _ps_open_file(title, filters, desktop, owner)
            except Exception as e2:
                self._log(f"PowerShell 文件对话框异常：{e2}", "warn")
        if r:
            self._log(f"✓ 已选择文件：{r}", "info")
        else:
            self._log("未能选择文件，可直接在输入框手动粘贴路径。", "err")
        return r or None

    def _pick_folder(self, title):
        # 只弹一次系统原生对话框：取消直接返回，绝不级联；仅原生真正报错时回退一次 PowerShell
        desktop = get_desktop_dir()
        owner = _owner_hwnd()
        r = None
        try:
            r = _native_folder(title, desktop, owner)
        except Exception as e:
            self._log(f"系统目录对话框异常：{e}", "warn")
            try:
                r = _ps_folder(title, desktop, owner)
            except Exception as e2:
                self._log(f"PowerShell 目录对话框异常：{e2}", "warn")
        if r:
            self._log(f"✓ 已选择目录：{r}", "info")
        else:
            self._log("未能选择目录，可直接在输入框手动粘贴路径。", "err")
        return r or None

    def _detect(self, path):
        fmt = detect_format(path)
        fmt_name = FORMAT_NAMES.get(fmt, "未知格式") if fmt else "不支持的格式"
        if fmt == "rar":
            if not ensure_unrar(self._log):
                self._emit(f"onStatus({json.dumps('格式：RAR ｜ 需要 UnRAR.exe（正在使用的系统缺少，已尝试自动下载）')});")
                return
        enc, kind = is_archive_encrypted(path)
        label = {"aes": "已加密（AES）", "zipcrypto": "已加密（ZipCrypto，可用明文攻击）",
                 "7z": "已加密（7z AES）", "rar": "已加密（RAR）",
                 "none": "未加密（可直接解压）", "empty": "空压缩包",
                 "error": "无法读取", "unknown": "已加密"}.get(kind, "已加密")
        if fmt is None:
            label = "不支持的格式（仅支持 ZIP / 7Z / RAR）"
        self._emit(f"onStatus({json.dumps('格式：'+fmt_name+' ｜ '+label)});")

    def _event(self, kind, data):
        if kind == "progress":
            tried, total, speed, eta = data
            t = total if total else 0
            e = eta if eta != float("inf") else 1e18
            self._emit(f"onProgress({tried},{t},{speed:.2f},{e});")
        elif kind == "found":
            pwd, path, tried, secs = data
            self._emit(f"onFound({json.dumps(pwd)},{json.dumps(path)},{tried},{secs});")
        elif kind == "found_no_extract":
            pwd, info = data
            self._emit(f"onResult({json.dumps('★ 找到密码：'+pwd+'，但自动解压失败：'+info)},'warn');")
        elif kind == "fail":
            self._emit(f"onResult({json.dumps(data)},'err');")
        elif kind == "stopped":
            self._emit(f"onResult({json.dumps(data)},'warn');")

    def start(self, payload_json):
        try:
            p = json.loads(payload_json)
        except Exception:
            return
        method = p.get("method")
        zp = (p.get("zipPath") or "").strip()
        sp = (p.get("savePath") or "").strip() or get_desktop_dir()

        # 记住核心数（保存原始选择：auto / all / 数字）
        try:
            self.pref["cores"] = p.get("cores") or "auto"
            save_pref(self.pref)
        except Exception:
            pass

        if method != "gen":
            if not zp or not os.path.isfile(zp):
                self._emit(f"onResult({json.dumps('请先选择一个有效的压缩包文件。')},'err');")
                return
            fmt = detect_format(zp)
            if fmt is None:
                self._emit(f"onResult({json.dumps('不支持的压缩包格式，仅支持 ZIP / 7Z / RAR。')},'err');")
                return
            if fmt == "rar" and not ensure_unrar(self._log):
                self._emit(f"onResult({json.dumps('破解 RAR 需要 UnRAR.exe，且当前无法自动下载，请检查网络后重试。')},'err');")
                return
            try:
                os.makedirs(sp, exist_ok=True)
            except Exception as e:
                self._emit(f"onResult({json.dumps('保存目录无法创建：'+str(e))},'err');")
                return

        if self.cracker and self.cracker.is_running():
            self._emit(f"onResult({json.dumps('已有任务在运行，请先停止。')},'warn');")
            return

        self._emit("setRunning(true);")
        cores = self._resolve_cores(p.get("cores"))
        log = self._log

        if method == "direct":
            self._emit(f"onStart({json.dumps('开始尝试解压…')});")
            self.cracker = Cracker(self._event)
            threading.Thread(target=self._run_direct, args=(zp, sp, p.get("directPwd", "")), daemon=True).start()
            return

        if method == "brute":
            try:
                tl = int(p["bruteLen"])
                if tl <= 0:
                    raise ValueError
            except ValueError:
                return self._fail("请输入有效的密码长度。")
            ptype = int(p["bruteType"]); exact = p.get("bruteExact", False)
            pre = p.get("brutePrefix", ""); suf = p.get("bruteSuffix", "")
            total = estimate_count("brute", ptype=ptype, total_length=tl, exact_length=exact, prefix=pre, suffix=suf)
            name = dict(PASSWORD_TYPE_LABELS).get(ptype, "")
            self._emit(f"onStart({json.dumps('开始穷举破解：'+name+'，长度 '+str(tl)+'，'+str(cores)+' 核')});")
            if total:
                log(f"预计总组合数：{format_number(total)}", "info")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores, lambda: generate_bruteforce_candidates(ptype, tl, exact, pre, suf), total)
            return

        if method == "dict":
            dp = (p.get("dictPath") or "").strip()
            if not dp or not os.path.isfile(dp):
                return self._fail("请先选择有效的字典文件。")
            total = count_lines(dp)
            self._emit(f"onStart({json.dumps('开始字典破解，共 '+format_number(total)+' 条，'+str(cores)+' 核')});")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores, lambda: generate_dictionary_candidates(dp), total)
            return

        if method == "mask":
            mask = (p.get("maskPattern") or "").strip()
            if not mask:
                return self._fail("请输入掩码。")
            total = estimate_count("mask", mask=mask)
            self._emit(f"onStart({json.dumps('开始掩码攻击：'+mask+'，约 '+format_number(total)+' 种，'+str(cores)+' 核')});")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores, lambda: generate_mask_candidates(mask), total)
            return

        if method == "hybrid":
            dp = (p.get("hybridDict") or "").strip()
            mask = (p.get("hybridMask") or "").strip()
            if not dp or not os.path.isfile(dp):
                return self._fail("请先选择混合攻击的字典文件。")
            if not mask:
                return self._fail("请输入混合攻击的掩码。")
            pos = p.get("hybridPos", "suffix")
            total = estimate_count("hybrid", path=dp, mask=mask)
            self._emit(f"onStart({json.dumps('开始混合攻击（字典+掩码），'+str(cores)+' 核')});")
            if total:
                log(f"预计约 {format_number(total)} 种组合", "info")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores, lambda: generate_hybrid_candidates(dp, mask, pos), total)
            return

        if method == "numeric":
            opts = p.get("num", {})
            self._emit(f"onStart({json.dumps('开始数字规律破解…')});")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores, lambda: generate_numeric_candidates(opts), None)
            return

        if method == "common":
            total = estimate_count("common")
            self._emit(f"onStart({json.dumps('开始常见密码库破解（约 '+str(total)+' 条）')});")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores,
                             lambda: (x for x in COMMON_PASSWORDS.splitlines() if x.strip()), total)
            return

        if method == "combinator":
            a = (p.get("comb1") or "").strip(); b = (p.get("comb2") or "").strip()
            if not a or not os.path.isfile(a) or not b or not os.path.isfile(b):
                return self._fail("请先选择两个有效的字典文件。")
            sep = p.get("combSep", "")
            self._emit(f"onStart({json.dumps('开始组合攻击（A+B 拼接）…')});")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores, lambda: generate_combinator_candidates(a, b, sep), None)
            return

        if method == "rules":
            dp = (p.get("rulesPath") or "").strip()
            if not dp or not os.path.isfile(dp):
                return self._fail("请先选择基础字典文件。")
            opts = {"case": p.get("rulesCase", False), "suffix": p.get("rulesSuffix", False),
                    "digits": p.get("rulesDigits", False), "prefix": p.get("rulesPrefix", False)}
            total = estimate_count("rules", path=dp)
            self._emit(f"onStart({json.dumps('开始规则变形破解，约 '+format_number(total)+' 种…')});")
            self.cracker = Cracker(self._event)
            self.cracker.run(zp, sp, cores, lambda: generate_rules_candidates(dp, opts), total)
            return

        if method == "plaintext":
            pf = (p.get("plainFile") or "").strip()
            entry = (p.get("plainEntry") or "").strip()
            bk = (p.get("bkPath") or "").strip()
            if not pf or not os.path.isfile(pf):
                return self._fail("请先选择已知明文文件。")
            if not entry:
                return self._fail("请填写该明文对应的压缩包内文件名。")
            self._emit(f"onStart({json.dumps('开始明文攻击（bkcrack）…')});")
            self.cracker = Cracker(self._event)
            threading.Thread(target=self._run_plaintext, args=(zp, sp, pf, entry, bk), daemon=True).start()
            return

        if method == "keydecrypt":
            k1 = (p.get("key1") or "").strip()
            k2 = (p.get("key2") or "").strip()
            k3 = (p.get("key3") or "").strip()
            bk = (p.get("bkPath2") or "").strip()
            keys = [k1, k2, k3]
            if not all(re.fullmatch(r"[0-9a-fA-F]{1,8}", k or "") for k in keys):
                return self._fail("请填写 3 个有效的十六进制内部密钥（各 1-8 位）。")
            keys = [k.lower().zfill(8) for k in keys]
            self._emit(f"onStart({json.dumps('开始密钥直解（bkcrack -k）…')});")
            self.cracker = Cracker(self._event)
            threading.Thread(target=self._run_keydecrypt, args=(zp, sp, keys, bk), daemon=True).start()
            return

        if method == "gen":
            try:
                ptype = int(p["genType"]); lo = int(p["genMin"]); hi = int(p["genMax"]); qty = int(p["genQty"])
                if lo <= 0 or hi <= 0 or lo > hi or qty < 0:
                    raise ValueError
            except ValueError:
                return self._fail("长度/数量设置无效。")
            out = os.path.join(sp, "生成字典.txt")
            self._emit(f"onStart({json.dumps('开始生成密码字典 → '+out)});")
            self.cracker = Cracker(self._event)
            threading.Thread(target=self._run_gen, args=(ptype, lo, hi, qty, out), daemon=True).start()
            return

        self._fail("未知破解方式。")

    def _fail(self, msg):
        self._emit(f"onResult({json.dumps(msg)},'err');")

    def _run_direct(self, zp, sp, pwd):
        ok, info = unzip_file(zp, sp, pwd if pwd else None)
        if ok:
            self._emit(f"onResult({json.dumps('解压成功！文件已保存到：'+info)},'ok');")
        else:
            self._emit(f"onResult({json.dumps(info)},'err');")

    def _run_gen(self, ptype, lo, hi, qty, out):
        cs = get_char_set(ptype)
        count = 0
        start = time.time()
        last = 0
        try:
            with open(out, "w", encoding="utf-8") as f:
                for L in range(lo, hi + 1):
                    for combo in itertools.product(cs, repeat=L):
                        if self.cracker and self.cracker.stop_event.is_set():
                            self._emit(f"onResult({json.dumps('已停止，已生成 '+format_number(count)+' 个 → '+out)},'warn');")
                            return
                        f.write("".join(combo) + "\n")
                        count += 1
                        if qty > 0 and count >= qty:
                            self._emit(f"onGen({count},{json.dumps(out)});")
                            return
                        now = time.time()
                        if now - last >= 0.3:
                            last = now
                            spd = count / (now - start) if now > start else 0
                            self._emit(f"onProgress({count},{qty if qty>0 else 0},{spd:.2f},0);")
            self._emit(f"onGen({count},{json.dumps(out)});")
        except Exception as e:
            self._emit(f"onResult({json.dumps('密码生成失败：'+str(e))},'err');")

    def _run_plaintext(self, zp, sp, plainfile, entry, bkpath):
        bk = bkpath if (bkpath and os.path.isfile(bkpath)) else find_bkcrack()
        if not bk:
            self._emit(
                "showModal('未找到 bkcrack','warn',"
                "'明文攻击依赖开源工具 <b>bkcrack</b>，未在系统中找到。<br><br>"
                "请从 <code>github.com/kimci86/bkcrack/releases</code> 下载，"
                "解压后把 <code>bkcrack.exe</code> 放到本程序目录或桌面，"
                "或在“bkcrack 路径”中手动指定。');")
            self._emit("setRunning(false);")
            return
        log = self._log
        log(f"使用 bkcrack：{bk}", "info")
        # 1) 求解内部密钥
        cmd = [bk, "-C", zp, "-c", entry, "-p", plainfile]
        code, lines = self._stream_proc(cmd, log)
        if self.cracker and self.cracker.stop_event.is_set():
            self._emit(f"onResult({json.dumps('明文攻击已停止。')},'warn');")
            return
        keys = None
        for ln in lines:
            m = re.search(r"([0-9a-fA-F]{6,8})\s+([0-9a-fA-F]{6,8})\s+([0-9a-fA-F]{6,8})", ln)
            if "key" in ln.lower() and m:
                keys = [m.group(1), m.group(2), m.group(3)]
                break
        if not keys:
            for ln in lines:
                m = re.fullmatch(r"\s*([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})\s+([0-9a-fA-F]{8})\s*", ln)
                if m:
                    keys = [m.group(1), m.group(2), m.group(3)]
                    break
        if not keys:
            self._emit(f"onResult({json.dumps('未能求出内部密钥，请确认明文/文件名是否正确，且压缩包为 ZipCrypto 加密。')},'err');")
            return
        log(f"★ 已求出 3 个内部密钥：{' '.join(keys)}", "ok")
        # 2) 用密钥还原（保持原始压缩结构，不重新打包）
        self._unlock_with_keys(bk, zp, sp, keys, log)

    def _run_keydecrypt(self, zp, sp, keys, bkpath):
        """密钥直解：已有 3 个内部密钥，直接用 bkcrack -U 还原，保持原始结构。"""
        bk = bkpath if (bkpath and os.path.isfile(bkpath)) else find_bkcrack()
        if not bk:
            self._emit(
                "showModal('未找到 bkcrack','warn',"
                "'密钥直解依赖开源工具 <b>bkcrack</b>，未在系统中找到。<br><br>"
                "请从 <code>github.com/kimci86/bkcrack/releases</code> 下载，"
                "把 <code>bkcrack.exe</code> 放到本程序目录或桌面，或手动指定路径。');")
            self._emit("setRunning(false);")
            return
        log = self._log
        log(f"使用 bkcrack：{bk}", "info")
        log(f"使用内部密钥：{' '.join(keys)}", "info")
        self._unlock_with_keys(bk, zp, sp, keys, log)

    def _unlock_with_keys(self, bk, zp, sp, keys, log):
        """用 3 个内部密钥导出解密副本并解压。-U 保留原始压缩流，解出的内容与原始逐字节一致。"""
        keystr = " ".join(keys)
        new_pwd = "123456"
        new_zip = os.path.join(sp, "已解密_" + os.path.splitext(os.path.basename(zp))[0] + ".zip")
        try:
            if os.path.isfile(new_zip):
                os.remove(new_zip)
        except Exception:
            pass
        cmd2 = [bk, "-C", zp, "-k", keys[0], keys[1], keys[2], "-U", new_zip, new_pwd]
        self._stream_proc(cmd2, log)
        if self.cracker and self.cracker.stop_event.is_set():
            self._emit(f"onResult({json.dumps('已停止。')},'warn');")
            return
        if os.path.isfile(new_zip):
            ok, info = unzip_file(new_zip, sp, new_pwd)
            body = ("<b>3 个内部密钥：</b><br><code>" + html_escape(keystr) + "</code><br>"
                    "<b>解密副本：</b><br>" + html_escape(new_zip) + "（新密码 " + new_pwd + "）<br>")
            if ok:
                log(f"★ 已用密钥还原，文件解压到：{info}", "ok")
                self._emit(
                    "showModal('密钥还原成功','ok'," +
                    json.dumps(body + "<b>解压目录：</b><br>" + info +
                               "<br><br><span style='color:var(--sub)'>说明：采用 bkcrack -U 保留原始压缩流，"
                               "解出的文件与原始逐字节一致，结构不会改变。</span>") + ");")
                self._emit("setRunning(false);")
                self._emit("toast('密钥还原成功','ok');")
            else:
                self._emit(f"onResult({json.dumps('已生成解密包：'+new_zip+'（新密码 '+new_pwd+'），但自动解压失败：'+info)},'warn');")
        else:
            self._emit(f"onResult({json.dumps('已求出/使用密钥，但导出解密包失败，可手动执行：bkcrack -C 原包 -k '+keystr+' -U 输出.zip 新密码')},'warn');")

    def _stream_proc(self, cmd, log):
        out_lines = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                                    creationflags=(0x08000000 if os.name == "nt" else 0))
            for line in proc.stdout:
                s = line.rstrip()
                out_lines.append(s)
                if s:
                    log("  " + s, "info")
                if self.cracker and self.cracker.stop_event.is_set():
                    try: proc.terminate()
                    except Exception: pass
                    break
            proc.wait()
            return proc.returncode, out_lines
        except Exception as e:
            log(f"执行 bkcrack 出错：{e}", "err")
            return -1, out_lines

    def stop(self):
        if self.cracker:
            self.cracker.stop()


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# 运行环境自检 + 自动修复（打包后给无 Python 环境的朋友使用）
#   缺失则自动下载：1) Edge WebView2 运行时  2) bkcrack.exe（明文/密钥攻击）
# ------------------------------------------------------------------
WEBVIEW2_FWLINK = "https://go.microsoft.com/fwlink/p/?LinkId=2093437"
BKCRACK_VER = "1.8.1"
BKCRACK_URL = ("https://github.com/kimci86/bkcrack/releases/download/"
"v%s/bkcrack-%s-win64.zip" % (BKCRACK_VER, BKCRACK_VER))


def _webview2_installed():
    """检测系统是否安装 Edge WebView2 运行时（用户级 / 系统级均可识别）。"""
    try:
        import winreg
        KEY = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A08C11}"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for pre in ("SOFTWARE\\", "SOFTWARE\\WOW6432Node\\"):
                try:
                    with winreg.OpenKey(root, pre + KEY):
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
        if base and os.path.isdir(os.path.join(base, "Microsoft", "EdgeWebView", "Application")):
            return True
    return False


def _app_dir():
    """真实 exe 所在目录（程序启动位置）。
    PyInstaller onefile 下 sys.executable / sys.argv[0] 指向临时解包目录，
    故用 GetModuleFileName 取用户实际启动的 exe 路径。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(2048)
        n = ctypes.windll.kernel32.GetModuleFileNameW(0, buf, 2048)
        if n:
            return os.path.dirname(buf.value)
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _download_file(url, dest, log, p, label="文件"):
    """下载文件到 dest，返回 (成功, 错误信息)。log 用于进度提示。"""
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            total = resp.headers.get("Content-Length")
            total = int(total) if (total and total.isdigit()) else 0
            done = 0
            next_tip = 0
            with open(dest, "wb") as f:
                while True:
                    buf = resp.read(65536)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if total and done >= next_tip:
                        log("  %s 下载中… %d%%" % (label, done * 100 // total), p["fg"])
                        next_tip += max(total // 50, 1)
        return True, ""
    except Exception as e:
        return False, str(e)


def _ensure_webview2(log, p):
    import tempfile
    log("WebView2 未安装，准备下载安装器（约 100MB，请保持联网）…", p["warn"])
    setup = os.path.join(tempfile.gettempdir(), "MicrosoftEdgeWebview2Setup.exe")
    ok, err = _download_file(WEBVIEW2_FWLINK, setup, log, p, "WebView2 安装器")
    if not ok:
        log("[X] 安装器下载失败：%s" % err, p["err"])
        log("请手动从 https://go.microsoft.com/fwlink/p/?LinkId=2093437 下载安装 WebView2 Runtime。", p["err"])
        return False
    log("正在安装 WebView2 运行时（用户级，无需管理员授权）…", p["accent"])
    code = _stream([setup, "--user", "--silent"], log, p)
    if code == 0 and _webview2_installed():
        try:
            os.remove(setup)
        except Exception:
            pass
        log("[OK] WebView2 安装成功。", p["ok"])
        return True
    log("用户级安装失败/被拒，尝试系统级安装（会弹出管理员授权）…", p["warn"])
    code = _stream([setup, "--silent"], log, p)
    try:
        os.remove(setup)
    except Exception:
        pass
    if code == 0 and _webview2_installed():
        log("[OK] WebView2 安装成功。", p["ok"])
        return True
    log("[X] WebView2 安装失败，请手动安装后重试。", p["err"])
    return False


def _ensure_bkcrack(log, p):
    import tempfile, zipfile, json
    log("未找到 bkcrack，准备下载…", p["warn"])
    url = None
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.github.com/repos/kimci86/bkcrack/releases/latest",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        import sys as _sys, platform as _plat
        _is64 = (_sys.maxsize > 2**32) or ("64" in str(_plat.machine()))
        for a in data.get("assets", []):
            nm = (a.get("name") or "").lower()
            if nm.endswith(".zip") and ("win64" in nm if _is64 else "win32" in nm):
                url = a.get("browser_download_url")
                break
        if not url:
            for a in data.get("assets", []):
                nm = (a.get("name") or "").lower()
                if nm.endswith(".zip") and "win" in nm:
                    url = a.get("browser_download_url")
                    break
    except Exception as e:
        log("  [提示] 通过 API 获取下载地址失败：%s" % e, p["warn"])
    if not url:
        url = BKCRACK_URL
        log("  改用固定版本地址 v%s" % BKCRACK_VER, p["fg"])
    zpath = os.path.join(tempfile.gettempdir(), "bkcrack.zip")
    ok, err = _download_file(url, zpath, log, p, "bkcrack")
    if not ok:
        log("[X] 下载失败：%s" % err, p["err"])
        log("请手动从 github.com/kimci86/bkcrack/releases 下载 bkcrack.exe，放到本程序目录或桌面。", p["err"])
        return False
    dest = _app_dir()
    try:
        with zipfile.ZipFile(zpath) as z:
            target = None
            for n in z.namelist():
                if n.lower().endswith("bkcrack.exe"):
                    target = n
                    break
            if not target:
                log("[X] 压缩包内未找到 bkcrack.exe", p["err"])
                return False
            out = os.path.join(dest, "bkcrack.exe")
            with open(out, "wb") as f:
                f.write(z.read(target))
        log("[OK] bkcrack.exe 已就绪：%s" % out, p["ok"])
        return True
    except Exception as e:
        log("[X] 解压失败：%s" % e, p["err"])
        return False
    finally:
        try:
            os.remove(zpath)
        except Exception:
            pass


# ------------------------------------------------------------------
# 运行环境检测页（webview HTML，与主界面 Fluent 风格统一）
#   仅当 WebView2 未安装时，先用极简 tkinter 装好 WebView2，再开此页面
# ------------------------------------------------------------------
SETUP_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --accent:#0067c0; --accent-hover:#1a75c8; --accent-press:#005499;
    --txt:#1a1a1a; --sub:#616161; --card:#ffffff; --card2:#f5f5f5;
    --line:#e5e5e5; --panel:#f3f3f3; --ok:#0f7b0f; --warn:#9d5d00; --err:#c42b1c;
    --radius:7px; --shadow:0 2px 6px rgba(0,0,0,.06);
  }
  body.dark{
    --accent:#4cc2ff; --accent-hover:#63caff; --accent-press:#3aa0d8;
    --txt:#f0f0f0; --sub:#c4c8d0; --card:#2b2b2b; --card2:#333333;
    --line:#3d3d3d; --panel:#202020; --ok:#6ccb5f; --warn:#fce100; --err:#ff99a4;
    --shadow:0 2px 8px rgba(0,0,0,.35);
  }
  *{box-sizing:border-box;}
  html,body{margin:0;height:100%;}
  body{font-family:"Segoe UI Variable Text","Segoe UI",system-ui,"Microsoft YaHei UI",sans-serif;
    background:var(--panel); color:var(--txt); font-size:13px; -webkit-font-smoothing:antialiased; overflow:hidden;}
  .wrap{width:100%;max-width:760px;margin:0 auto;padding:18px 20px 20px;display:flex;flex-direction:column;height:100vh;}
  header{display:flex;align-items:center;gap:10px;margin-bottom:14px;}
  header .brand{display:flex;align-items:center;gap:9px;}
  header .brand svg{color:var(--accent);}
  header .title{font-size:19px;font-weight:600;}
  header .sub{color:var(--sub);font-size:12px;margin-left:auto;text-align:right;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:13px 15px;margin-bottom:11px;box-shadow:var(--shadow);}
  .comp{display:flex;align-items:center;gap:13px;padding:11px 13px;}
  .comp + .comp{border-top:1px solid var(--line);}
  .dot{width:14px;height:14px;border-radius:50%;background:#9aa0a6;flex:none;transition:.2s;
    box-shadow:0 0 0 4px color-mix(in srgb,var(--line) 70%,transparent);}
  .dot.ok{background:var(--ok);} .dot.warn{background:var(--warn);}
  .dot.err{background:var(--err);} .dot.check{background:var(--accent);animation:pulse 1.1s infinite;}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 35%,transparent);}
    50%{box-shadow:0 0 0 7px color-mix(in srgb,var(--accent) 6%,transparent);}}
  .ctxt{flex:1;min-width:0;}
  .cname{font-size:13.5px;font-weight:600;}
  .cdesc{color:var(--sub);font-size:11.5px;margin-top:2px;}
  .cstat{font-size:12px;font-weight:600;color:var(--sub);white-space:nowrap;}
  .cstat.ok{color:var(--ok);} .cstat.warn{color:var(--warn);}
  .cstat.err{color:var(--err);} .cstat.check{color:var(--accent);}
  #pg{width:100%;height:7px;border-radius:6px;overflow:hidden;appearance:none;-webkit-appearance:none;
    margin:2px 0 12px;display:none;}
  #pg.show{display:block;}
  #pg::-webkit-progress-bar{background:var(--card2);border-radius:6px;}
  #pg:indeterminate::-webkit-progress-value{background:linear-gradient(90deg,transparent,var(--accent),transparent);
    background-size:45% 100%;animation:ind 1.1s infinite linear;}
  @keyframes ind{from{background-position:-45% 0;}to{background-position:145% 0;}}
  .logcard{display:flex;flex-direction:column;flex:1;min-height:0;}
  .logcard .lh{color:var(--sub);font-size:11px;font-weight:600;letter-spacing:.5px;margin-bottom:6px;}
  #log{flex:1;min-height:0;background:var(--card2);border:1px solid var(--line);border-radius:var(--radius);
    padding:9px 11px;overflow:auto;font-family:"Cascadia Code",Consolas,monospace;font-size:12px;line-height:1.55;
    color:var(--txt);white-space:pre-wrap;}
  .log-ok{color:var(--ok);} .log-err{color:var(--err);} .log-warn{color:var(--warn);}
  .log-info{color:var(--sub);} .log-status{color:var(--accent);}
  .deplog{background:var(--card2);border:1px solid var(--line);border-radius:var(--radius);
    padding:8px 10px;margin-top:6px;max-height:200px;overflow:auto;
    font-family:"Cascadia Code",Consolas,monospace;font-size:11.5px;line-height:1.55;color:var(--txt);user-select:text;}
  .deplog .log-line{white-space:pre-wrap;}
  footer{display:flex;gap:8px;justify-content:flex-end;margin-top:12px;}
  button{font-family:inherit;font-size:13px;border-radius:var(--radius);padding:8px 16px;
    border:1px solid var(--line);background:var(--card);color:var(--txt);cursor:pointer;transition:.12s;
    display:inline-flex;align-items:center;gap:6px;font-weight:600;}
  button:hover{background:var(--card2);}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
  body.dark button.primary{color:#08233a;}
  button.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);}
  button.danger{background:var(--err);border-color:var(--err);color:#fff;}
  body.dark button.danger{color:#3a0b0b;}
  button:disabled{opacity:.4;cursor:not-allowed;}
</style>
</head>
<body __THEME__>
<div class="wrap">
  <header>
    <div class="brand">
      <svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 2l8 4v6c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/></svg>
      <span class="title">运行环境检测</span>
    </div>
    <span class="sub">自动检测 · 缺失组件自动安装</span>
  </header>

  <div class="card">
    <div class="comp" id="rowWV">
      <div class="dot" id="dotWV"></div>
      <div class="ctxt"><div class="cname">Edge WebView2 运行时</div><div class="cdesc">界面渲染依赖 · 必装</div></div>
      <div class="cstat" id="stWV">等待中…</div>
    </div>
    <div class="comp" id="rowBK">
      <div class="dot" id="dotBK"></div>
      <div class="ctxt"><div class="cname">bkcrack 破解工具</div><div class="cdesc">明文 / 密钥攻击用 · 可选</div></div>
      <div class="cstat" id="stBK">等待中…</div>
    </div>
  </div>

  <progress id="pg" indeterminate></progress>

  <div class="card logcard">
    <div class="lh">运 行 日 志</div>
    <div id="log"></div>
  </div>

  <footer>
    <button id="btnRetry" onclick="if(window.pywebview&&window.pywebview.api)window.pywebview.api.retry()" style="display:none">重试</button>
    <button id="btnExit" class="danger" onclick="if(window.pywebview&&window.pywebview.api)window.pywebview.api.exit()" style="display:none">退出</button>
    <button id="btnEnter" class="primary" onclick="if(window.pywebview&&window.pywebview.api)window.pywebview.api.enter()" style="display:none">进入程序</button>
  </footer>
</div>
<script>
  function setRow(dotId, stId, st, text){
    var dot = document.getElementById(dotId), s = document.getElementById(stId);
    var cls = (st==="ok"||st==="warn"||st==="err"||st==="check") ? " "+st : "";
    dot.className = "dot" + cls; s.className = "cstat" + cls; s.textContent = text;
  }
  function addLog(msg, cls){
    var el = document.getElementById("log");
    var d = document.createElement("div");
    d.className = cls || "log-info"; d.textContent = msg;
    el.appendChild(d); el.scrollTop = el.scrollHeight;
  }
  function progress(on){ document.getElementById("pg").classList.toggle("show", !!on); }
  function done(state){
    progress(false);
    if(state==="ready"){
      addLog("全部依赖已就绪，即将自动进入程序…", "log-ok");
      setTimeout(function(){
        if(window.pywebview&&window.pywebview.api) window.pywebview.api.enter();
      }, 700);
    } else {
      document.getElementById("btnExit").style.display="";
      document.getElementById("btnRetry").style.display="";
      if(state==="optional-failed") document.getElementById("btnEnter").style.display="";
    }
  }
  var _booted = false;
  function boot(){
    if(_booted) return;
    if(window.pywebview && window.pywebview.api){
      _booted = true;
      window.pywebview.api.start_check();
    } else {
      setTimeout(boot, 150);   // pywebviewready 未触发时持续轮询兜底
    }
  }
  if(document.readyState === "loading"){
    window.addEventListener("DOMContentLoaded", function(){ setTimeout(boot, 100); });
  } else {
    setTimeout(boot, 100);
  }
  window.addEventListener("pywebviewready", boot);
</script>
</body>
</html>
"""


# ------------------------------------------------------------------
# bkcrack 独立安装窗口（终端风格，复用检测页观感）
# ------------------------------------------------------------------
BK_INSTALL_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --accent:#0067c0; --accent-hover:#1a75c8; --accent-press:#005499;
    --txt:#1a1a1a; --sub:#616161; --card:#ffffff; --card2:#f5f5f5;
    --line:#e5e5e5; --panel:#f3f3f3; --ok:#0f7b0f; --warn:#9d5d00; --err:#c42b1c;
    --radius:7px; --shadow:0 2px 6px rgba(0,0,0,.06);
  }
  body.dark{
    --accent:#4cc2ff; --accent-hover:#63caff; --accent-press:#3aa0d8;
    --txt:#f0f0f0; --sub:#c4c8d0; --card:#2b2b2b; --card2:#333333;
    --line:#3d3d3d; --panel:#202020; --ok:#6ccb5f; --warn:#fce100; --err:#ff99a4;
    --shadow:0 2px 8px rgba(0,0,0,.35);
  }
  *{box-sizing:border-box;}
  html,body{margin:0;height:100%;}
  body{font-family:"Segoe UI Variable Text","Segoe UI",system-ui,"Microsoft YaHei UI",sans-serif;
    background:var(--panel); color:var(--txt); font-size:13px; -webkit-font-smoothing:antialiased; overflow:hidden;}
  .wrap{width:100%;max-width:680px;margin:0 auto;padding:18px 20px 20px;display:flex;flex-direction:column;height:100vh;}
  header{display:flex;align-items:center;gap:10px;margin-bottom:14px;}
  header .title{font-size:19px;font-weight:600;}
  header .sub{color:var(--sub);font-size:12px;margin-left:auto;text-align:right;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
    padding:13px 15px;margin-bottom:11px;box-shadow:var(--shadow);}
  .cdesc{color:var(--sub);font-size:12px;margin-top:3px;line-height:1.6;}
  #pg{width:100%;height:7px;border-radius:6px;overflow:hidden;appearance:none;-webkit-appearance:none;
    margin:2px 0 12px;display:none;}
  #pg.show{display:block;}
  #pg::-webkit-progress-bar{background:var(--card2);border-radius:6px;}
  #pg:indeterminate::-webkit-progress-value{background:linear-gradient(90deg,transparent,var(--accent),transparent);
    background-size:45% 100%;animation:ind 1.1s infinite linear;}
  @keyframes ind{from{background-position:-45% 0;}to{background-position:145% 0;}}
  .logcard{display:flex;flex-direction:column;flex:1;min-height:0;}
  .logcard .lh{color:var(--sub);font-size:11px;font-weight:600;letter-spacing:.5px;margin-bottom:6px;}
  #log{flex:1;min-height:0;background:var(--card2);border:1px solid var(--line);border-radius:var(--radius);
    padding:9px 11px;overflow:auto;font-family:"Cascadia Code",Consolas,monospace;font-size:12px;line-height:1.55;
    color:var(--txt);white-space:pre-wrap;}
  .log-ok{color:var(--ok);} .log-err{color:var(--err);} .log-warn{color:var(--warn);}
  .log-info{color:var(--sub);} .log-status{color:var(--accent);}
  .deplog{background:var(--card2);border:1px solid var(--line);border-radius:var(--radius);
    padding:8px 10px;margin-top:6px;max-height:200px;overflow:auto;
    font-family:"Cascadia Code",Consolas,monospace;font-size:11.5px;line-height:1.55;color:var(--txt);user-select:text;}
  .deplog .log-line{white-space:pre-wrap;}
  footer{display:flex;gap:8px;justify-content:flex-end;margin-top:12px;}
  button{font-family:inherit;font-size:13px;border-radius:var(--radius);padding:8px 16px;
    border:1px solid var(--line);background:var(--card);color:var(--txt);cursor:pointer;transition:.12s;
    display:inline-flex;align-items:center;gap:6px;font-weight:600;}
  button:hover{background:var(--card2);}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;}
  body.dark button.primary{color:#08233a;}
  button.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);}
  button:disabled{opacity:.4;cursor:not-allowed;}
</style>
</head>
<body>
<div id="bgLayer"></div>
<div class="wrap">
  <header>
    <div class="title">安装 bkcrack</div>
    <span class="sub">联网下载 · 明文 / 密钥攻击组件</span>
  </header>
  <div class="card">
    <div class="cdesc">正在下载 <b>bkcrack</b>（开源明文 / 密钥攻击工具）并解压到本程序目录。<br>
    下载进度与结果会实时显示在下方终端窗口中，完成后即可在主界面使用明文攻击 / 密钥直解。</div>
  </div>
  <progress id="pg" indeterminate></progress>
  <div class="card logcard">
    <div class="lh">安 装 日 志</div>
    <div id="log"></div>
  </div>
  <footer>
    <button id="btnClose" class="primary" onclick="if(window.pywebview&&window.pywebview.api)window.pywebview.api.close()" disabled>关闭</button>
  </footer>
</div>
<script>
  function addLog(msg, cls){
    var el = document.getElementById("log");
    var d = document.createElement("div");
    d.className = cls || "log-info"; d.textContent = msg;
    el.appendChild(d); el.scrollTop = el.scrollHeight;
  }
  function progress(on){ document.getElementById("pg").classList.toggle("show", !!on); }
  function finished(ok){
    progress(false);
    var b = document.getElementById("btnClose");
    if(ok){
      addLog("✓ bkcrack 安装完成，即将自动返回功能页面。", "log-ok");
      setTimeout(function(){
        if(window.pywebview&&window.pywebview.api) window.pywebview.api.close();
      }, 800);
    } else {
      b.disabled = false;
      addLog("✗ bkcrack 安装失败，请重试或手动安装。", "log-err");
    }
  }
  if(document.readyState === "loading"){
    window.addEventListener("DOMContentLoaded", function(){ setTimeout(function(){ if(window.pywebview&&window.pywebview.api)window.pywebview.api.start(); }, 120); });
  } else {
    setTimeout(function(){ if(window.pywebview&&window.pywebview.api)window.pywebview.api.start(); }, 120);
  }
</script>
</body>
</html>
"""


class BkcrackInstallApi:
    """bkcrack 独立安装窗口的 JS 桥接：后台下载并实时回传日志，成功后通知主窗口刷新。"""

    COLORS = {"ok": "log-ok", "err": "log-err", "warn": "log-warn",
              "accent": "log-status", "fg": "log-info"}

    def __init__(self, main_api):
        self.main_api = main_api
        self.win = None
        self._p = {"fg": "#616161", "ok": "#0f7b0f", "warn": "#9d5d00",
                   "err": "#c42b1c", "accent": "#0067c0"}

    def _js(self, code):
        try:
            if self.win is not None:
                self.win.evaluate_js(code)
        except Exception:
            pass

    def _log(self, msg, color=None):
        cls = self.COLORS.get(color, "log-info")
        self._js("addLog(%s,%s)" % (json.dumps(msg), json.dumps(cls)))

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self._js("progress(true)")
        ok = False
        try:
            ok = _ensure_bkcrack(self._log, self._p)
        except Exception as e:
            self._log("安装过程异常：%s" % e, "err")
            ok = False
        self._js("progress(false)")
        self._js("finished(%s)" % json.dumps(bool(ok)))
        # 通知主窗口刷新 bkcrack 状态
        try:
            main_win = self.main_api.win
            if main_win is None and self.main_api.webview.windows:
                main_win = self.main_api.webview.windows[0]
            if main_win is not None:
                if ok:
                    main_win.evaluate_js("setBkcrackMissing(false);")
                    main_win.evaluate_js("toast('bkcrack 安装成功，明文/密钥攻击已可用','ok');")
                else:
                    main_win.evaluate_js("toast('bkcrack 安装失败，可稍后在设置中重试','err');")
        except Exception:
            pass

    def close(self):
        try:
            if self.win is not None:
                self.win.destroy()
        except Exception:
            pass


def open_bkcrack_installer(main_api):
    """在主界面外打开一个独立的终端窗口，联网下载并安装 bkcrack。"""
    import webview
    api = BkcrackInstallApi(main_api)
    win = webview.create_window("安装 bkcrack", html=BK_INSTALL_HTML, js_api=api,
                                width=680, height=520, min_size=(560, 440))
    api.win = win
    api.start()


class SetupApi:
    """webview 检测页的 JS 桥接：检测并自动下载缺失组件，实时回传日志 / 状态。"""
    def __init__(self):
        self.result = {"ok": False}
        self._p = {"fg": "#1a1a1a", "warn": "#9d5d00", "accent": "#0067c0",
                   "ok": "#0f7b0f", "err": "#c42b1c"}
        self._cls = {self._p["accent"]: "log-status", self._p["ok"]: "log-ok",
                     self._p["warn"]: "log-warn", self._p["err"]: "log-err",
                     self._p["fg"]: "log-info"}
        self._started = False          # 检测防重入（events.loaded 与 JS 兜底都安全）

    def _js(self, code):
        try:
            if webview.windows:
                webview.windows[0].evaluate_js(code)
        except Exception:
            pass

    def _log(self, msg, color=None):
        color = color or self._p["fg"]
        self._js("addLog(%s, %s)" % (json.dumps(msg), json.dumps(self._cls.get(color, "log-info"))))

    def _check(self):
        if self._started:   # 防重入：events.loaded 与 JS 端轮询兜底可能都触发
            return
        self._started = True
        self._js("progress(true)")
        self._log("=" * 46, self._p["fg"])
        self._log("[环境] 操作系统: Windows", self._p["accent"])
        # WebView2（能开本页即已安装）
        self._js('setRow("dotWV","stWV","check","检测中…")')
        if _webview2_installed():
            self._js('setRow("dotWV","stWV","ok","已就绪")')
            self._log("[OK] WebView2 已安装，界面可正常显示。", self._p["ok"])
            wv = True
        else:
            self._js('setRow("dotWV","stWV","err","未安装")')
            self._log("[X] WebView2 未安装，界面无法加载。", self._p["err"])
            wv = False
        # bkcrack
        self._js('setRow("dotBK","stBK","check","检测中…")')
        self._log("\n[检测] bkcrack（明文 / 密钥攻击组件）…", self._p["accent"])
        bk_ready = bool(find_bkcrack())
        if bk_ready:
            self._js('setRow("dotBK","stBK","ok","已就绪")')
            self._log("[OK] 已找到 bkcrack，明文 / 密钥攻击可用。", self._p["ok"])
        else:
            self._js('setRow("dotBK","stBK","warn","未安装")')
            self._log("[!] 未找到 bkcrack（仅影响明文 / 密钥攻击）。", self._p["warn"])
            self._log("      正在自动下载 bkcrack.exe…", self._p["fg"])
            self._js('setRow("dotBK","stBK","check","下载中…")')
            bk_ready = _ensure_bkcrack(self._log, self._p)
            if bk_ready:
                self._js('setRow("dotBK","stBK","ok","已就绪")')
                self._log("[OK] bkcrack.exe 已就绪。", self._p["ok"])
            else:
                self._js('setRow("dotBK","stBK","warn","下载失败·可忽略")')
                self._log("[!] bkcrack 下载失败，可点击「进入程序」继续使用其它功能，或点击「重试」再次安装。", self._p["warn"])
        self._log("-" * 46, self._p["fg"])
        if wv and bk_ready:
            self._log("[OK] 所有可安装依赖均已就绪，正在自动进入程序。", self._p["ok"])
            self._js('done("ready")')
        elif wv:
            self._log("[!] 可选依赖未安装，可重试或忽略并进入程序。", self._p["warn"])
            self._js('done("optional-failed")')
        else:
            self._js('done("blocked")')

    def start_check(self):
        threading.Thread(target=self._check, daemon=True).start()

    def retry(self):
        self._started = False
        self._js('setRow("dotWV","stWV","","等待中…")')
        self._js('setRow("dotBK","stBK","","等待中…")')
        self._js('document.getElementById("log").innerHTML=""')
        self._js('document.getElementById("btnEnter").style.display="none"')
        self._js('document.getElementById("btnExit").style.display="none"')
        self._js('document.getElementById("btnRetry").style.display="none"')
        self.start_check()

    def enter(self):
        # 标记成功并销毁检测窗口；run_runtime_setup() 的 webview.start() 返回，
        # 由 main() 再用一次独立的 webview.start() 打开主界面（避免会话内新建窗口导致崩溃）
        self.result["ok"] = True
        try:
            for w in list(webview.windows):
                try: w.destroy()
                except Exception: pass
        except Exception:
            pass

    def exit(self):
        self.result["ok"] = False
        try:
            for w in list(webview.windows):
                try: w.destroy()
                except Exception: pass
        except Exception:
            pass


def _tk_install_webview2():
    """WebView2 缺失时用极简 tkinter 窗口下载安装（此时尚不能开 webview）。返回是否成功。"""
    import tkinter as tk
    from tkinter import scrolledtext
    dark = detect_system_theme() == "dark"
    BG = "#1b1d23" if dark else "#eef1f6"
    CARD = "#262a33" if dark else "#ffffff"
    FG = "#e8eaed" if dark else "#1f2330"
    SUB = "#9aa0aa" if dark else "#6b7280"
    ACCENT = "#3b82f6"; OK = "#22c55e"; WARN = "#f59e0b"; ERR = "#ef4444"
    p = {"bg": BG, "fg": FG, "accent": ACCENT, "ok": OK, "warn": WARN, "err": ERR}
    res = {"ok": False}
    win = tk.Tk()
    win.title("正在安装组件")
    win.geometry("720x460")
    win.configure(bg=BG)
    win.resizable(False, False)
    try:
        import pywinstyles
        pywinstyles.apply_style(win, detect_system_theme())
    except Exception:
        pass
    tk.Label(win, text="⚙  正在安装 Edge WebView2 运行时",
             bg=BG, fg=FG, font=("Microsoft YaHei UI", 16, "bold")).pack(pady=(22, 4))
    tk.Label(win, text="界面渲染依赖未安装，正在自动下载并安装（约 100MB，请保持联网）…",
             bg=BG, fg=SUB, font=("Microsoft YaHei UI", 10)).pack(padx=24)
    term = scrolledtext.ScrolledText(win, bg=CARD, fg=FG, font=("Consolas", 10),
                                     relief="flat", bd=0, padx=14, pady=10)
    term.pack(fill="both", expand=True, padx=22, pady=16)
    term.configure(state="disabled")
    bar = tk.Frame(win, bg=BG); bar.pack(fill="x", padx=22, pady=(0, 18))

    def log(msg, color=FG):
        term.configure(state="normal")
        tag = "c%d" % (abs(hash(color)) % 100000)
        term.tag_configure(tag, foreground=color)
        term.insert("end", msg + "\n", tag); term.see("end"); term.configure(state="disabled")

    def add_btn(text, cmd, color):
        b = tk.Button(bar, text=text, command=cmd, bg=color, fg="white",
                      font=("Microsoft YaHei UI", 10, "bold"), relief="flat",
                      activebackground=color, activeforeground="white",
                      padx=22, pady=8, cursor="hand2", bd=0)
        b.pack(side="right", padx=6); return b

    def do_install():
        if _ensure_webview2(log, p):
            res["ok"] = True
            win.after(0, win.destroy)
        else:
            win.after(0, lambda: (
                add_btn("退出", win.destroy, ERR),
                add_btn("重试", lambda: threading.Thread(target=do_install, daemon=True).start(), ACCENT),
            ))

    threading.Thread(target=do_install, daemon=True).start()
    win.mainloop()
    return res["ok"]


def run_runtime_setup():
    """打包后启动：先确保 WebView2 已装（否则极简 tkinter 安装），再用 webview 打开检测页。
    关键：检测启动不依赖任何 webview 事件（events.loaded / pywebviewready 在内联 HTML 下都不可靠），
    用 threading.Timer 在窗口稳定显示后由 Python 端主动启动检测线程。"""
    import webview
    globals()["webview"] = webview   # 暴露为模块全局，供 SetupApi._js/_check 使用（否则 NameError 被 except 吞掉）
    if not _webview2_installed():
        if not _tk_install_webview2():
            return False
    dark = detect_system_theme() == "dark"
    html = SETUP_HTML.replace("__THEME__", 'class="dark"' if dark else "")
    api = SetupApi()
    setup_pos = _apply_saved_pos("win_setup_pos", 720, 600)
    win = webview.create_window("运行环境检测", html=html, js_api=api,
                                width=720, height=600, min_size=(620, 520),
                                **setup_pos)
    try:
        # 显示后用 pywebview 自身的 win.move 精确居中（无记录时）；有记录则沿用记忆位置
        win.events.shown += lambda: _restore_pos(win, "运行环境检测", 720, 600, setup_pos)
        win.events.closing += lambda: _save_win_pos("win_setup_pos", win, "运行环境检测")
        win.events.closed += lambda: _save_win_pos("win_setup_pos", win, "运行环境检测")
    except Exception:
        pass
    # webview.start() 是阻塞的；启动 GUI 前先排个 2 秒定时器兜底启动检测
    # （完全不依赖 events.loaded / pywebviewready / window.pywebview 注入）
    threading.Timer(2.0, _on_setup_timer, args=[api]).start()
    webview.start()
    return api.result.get("ok", False)


def _on_setup_timer(api):
    """定时器回调：2 秒后由 Python 端主动启动检测线程（_check 自带防重入）。"""
    if api._started:
        return
    threading.Thread(target=api._check, daemon=True).start()


def launch():
    import webview
    api = Api()
    main_pos = _apply_saved_pos("win_main_pos", 1040, 780)
    win = webview.create_window(APP_TITLE, html=HTML, js_api=api,
                                width=1040, height=780, min_size=(720, 560),
                                **main_pos)
    try:
        # 显示后用 pywebview 自身的 win.move 精确居中（无记录时）；有记录则沿用记忆位置
        win.events.shown += lambda: _restore_pos(win, APP_TITLE, 1040, 780, main_pos)
        win.events.closing += lambda: _save_win_pos("win_main_pos", win, APP_TITLE)
        win.events.closed += lambda: _save_win_pos("win_main_pos", win, APP_TITLE)
    except Exception:
        pass
    webview.start()


def run_environment_setup(missing_modules=None):
    import tkinter as tk
    from tkinter import scrolledtext
    p = {"bg": "#202020" if detect_system_theme() == "dark" else "#f3f3f3",
         "fg": "#ffffff" if detect_system_theme() == "dark" else "#1b1b1b",
         "accent": "#0078d4", "ok": "#16c60c", "warn": "#ffb900", "err": "#e81123"}
    result = {"ok": False}
    win = tk.Tk()
    win.title("环境初始化")
    win.geometry("720x480")
    win.configure(bg=p["bg"])
    try:
        import pywinstyles
        pywinstyles.apply_style(win, detect_system_theme())
    except Exception:
        pass
    tk.Label(win, text="正在初始化运行环境", bg=p["bg"], fg=p["accent"],
             font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(18, 4))
    tk.Label(win, text="首次运行会自动安装所需模块，请稍候…",
             bg=p["bg"], fg=p["fg"], font=("Microsoft YaHei UI", 10)).pack()
    term = scrolledtext.ScrolledText(win, bg=p["bg"], fg=p["fg"],
                                     font=("Consolas", 10), relief="flat", bd=0)
    term.pack(fill="both", expand=True, padx=18, pady=14)
    term.configure(state="disabled")
    bar = tk.Frame(win, bg=p["bg"]); bar.pack(fill="x", padx=18, pady=(0, 14))

    def log(msg, color=p["fg"]):
        term.configure(state="normal")
        tag = f"c{abs(hash(color)) % 100000}"
        term.tag_configure(tag, foreground=color)
        term.insert("end", msg + "\n", tag); term.see("end"); term.configure(state="disabled")

    def add_btn(text, cmd, color):
        b = tk.Button(bar, text=text, command=cmd, bg=color, fg="white",
                      font=("Microsoft YaHei UI", 10, "bold"), relief="flat",
                      activebackground=color, activeforeground="white", padx=18, pady=6, cursor="hand2", bd=0)
        b.pack(side="right", padx=6); return b

    def setup():
        log("=" * 60, p["fg"])
        log(f"[环境] Python : {sys.version.split()[0]}", p["accent"])
        missing = [m for m in REQUIRED_MODULES if not check_module_installed(m)] if missing_modules is None else list(missing_modules)
        if not missing:
            log("[OK] 所有依赖模块均已安装。", p["ok"])
            win.after(700, enter)
            return
        log(f"[!] 缺少依赖：{', '.join(missing)}", p["warn"])
        for mod in missing:
            pkg = PIP_NAMES.get(mod, mod)
            log(f"\n[->] 正在安装：{pkg}", p["accent"])
            code = _stream([sys.executable, "-m", "pip", "install", pkg, "--user",
                            "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"], log, p)
            if code != 0 or not check_module_installed(mod):
                log("[!] 镜像失败，尝试官方源…", p["warn"])
                _stream([sys.executable, "-m", "pip", "install", pkg, "--user"], log, p)
            if check_module_installed(mod):
                log(f"[OK] {mod} 安装成功。", p["ok"])
            else:
                log(f"[X] {mod} 安装失败。", p["err"])
                win.after(0, lambda: (add_btn("退出", win.destroy, p["err"]),
                                      add_btn("重试", lambda: threading.Thread(target=setup, daemon=True).start(), p["accent"])))
                return
        log("-" * 60, p["fg"])
        log("[OK] 依赖安装完成！", p["ok"])
        win.after(0, lambda: add_btn("进入程序", enter, p["ok"]))

    def enter():
        result["ok"] = True
        win.destroy()

    threading.Thread(target=setup, daemon=True).start()
    win.mainloop()
    return result["ok"]


def _stream(cmd, log, p):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, creationflags=(0x08000000 if os.name == "nt" else 0))
        for line in proc.stdout:
            log("    " + line.rstrip(), p["fg"])
        proc.wait()
        return proc.returncode
    except Exception as e:
        log(f"    [执行出错] {e}", p["err"])
        return -1


def main():
    if getattr(sys, "frozen", False):
        # 迁移旧版配置到程序启动位置（仅首次，会自动清理 %APPDATA%/ZipCracker）
        _migrate_legacy_config()
        pref = load_pref()
        # 已初始化且 WebView2 就绪 → 直接进主界面，不再每次打开都重复检测
        if pref.get("initialized") and _webview2_installed():
            launch()
            return
        # 打包后的 exe：先检测运行环境（WebView2 / bkcrack 缺失则自动下载）。
        # 检测窗口关闭后 run_runtime_setup 返回；成功则再用一次独立的 webview.start() 打开主界面，
        # 避免在同一会话内新建窗口（edgechromium 后端下会导致整个 GUI 循环退出）。
        if run_runtime_setup():
            # 运行时检测已通过：写初始化标记，之后启动直接进主界面、不再弹检测窗
            try:
                pref["initialized"] = True
                save_pref(pref)
            except Exception:
                pass
            launch()
    else:
        missing = [m for m in REQUIRED_MODULES if not check_module_installed(m)]
        if missing:
            if not run_environment_setup(missing):
                return
        launch()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
