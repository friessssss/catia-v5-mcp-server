"""Window inspection that needs no COM call (safe while CATIA is blocked)."""

from __future__ import annotations

_CATIA_EXES = {"cnext.exe", "3dexperience.exe"}


def catia_dialog_titles() -> list[str]:
    """Titles of visible dialog windows (#32770 or owned popups) belonging to
    a CATIA process. Empty off Windows or when nothing is found."""
    try:
        import win32api
        import win32con
        import win32gui
        import win32process
    except ImportError:
        return []

    exe_by_pid: dict[int, str] = {}

    def exe_name(pid: int) -> str:
        if pid not in exe_by_pid:
            name = ""
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
                )
                try:
                    name = win32process.GetModuleFileNameEx(handle, 0).rsplit("\\", 1)[-1]
                finally:
                    win32api.CloseHandle(handle)
            except Exception:  # noqa: BLE001 -- protected/exited process: not CATIA's
                name = ""
            exe_by_pid[pid] = name.lower()
        return exe_by_pid[pid]

    titles: list[str] = []

    def visit(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if exe_name(pid) not in _CATIA_EXES:
            return True
        is_dialog = win32gui.GetClassName(hwnd) == "#32770" or bool(
            win32gui.GetWindow(hwnd, win32con.GW_OWNER)
        )
        title = win32gui.GetWindowText(hwnd)
        if is_dialog and title:
            titles.append(title)
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:  # noqa: BLE001
        return titles
    return titles[:5]
