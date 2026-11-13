"""mpv 창(테두리 없는 창)의 크기 조절 영역을 넓혀 주는 눈에 안 보이는 띠.

mpv 는 테두리 없는 창의 크기 조절 영역을 윈도우 기본 테두리 두께(보통 8px)로 고정해 두고 바꿀 수 있는
옵션이 없다. 그래서 mpv 창 바깥 둘레에 거의 투명한 작은 창 8개(네 변 + 네 모서리)를 얹고, 그 위에서
마우스 모양이 바뀌게 한 뒤, 끌면 mpv 창 크기를 직접 바꾼다. 영상 비율은 유지하고 최소 크기 아래로는
줄어들지 않는다. 띠는 mpv 창이 보일 때만 나타나고, 창을 따라다닌다.
(바깥 4px + 안쪽 12px 가 잡힌다 — 숫자는 아래 OUTER / INNER 에서 바꾼다)

Tk 의 메시지 루프가 같은 스레드의 창 메시지도 처리하므로 별도 스레드는 필요 없다.
"""
import ctypes
import ctypes.wintypes as wt

OUTER = 4         # 창 바깥쪽으로는 이만큼(px)만 (살짝 밖에서도 잡히게)
INNER = 12        # 창 안쪽으로 이만큼. 컨트롤바 패널·제목줄이 창 가장자리에서 8px 안쪽부터 시작하고
                  # 닫기 버튼은 위에서 11px, 아래 버튼들은 아래에서 16px 떨어져 있어서 12px 까지는 안 가린다
CORNER_INNER = 16
MIN_W, MIN_H = 360, 200      # 최소 창 크기 (px, 화면 배율 100% 기준)

HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT, HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = range(10, 18)


def compute_rect(code, rect0, dx, dy, min_w=MIN_W, min_h=MIN_H):
    """끌기 시작 때의 창 (x, y, w, h) 와 마우스 이동량(dx, dy)으로 새 (x, y, w, h) 를 구한다.
    시작 때의 가로세로 비율을 유지하고, 끄는 변의 반대쪽 변/모서리는 제자리에 둔다."""
    x0, y0, w0, h0 = rect0
    # 비율 유지: 각 방향 이동이 만드는 배율을 구해서, 모서리는 더 크게 변한 쪽을 따른다
    sx = {HTLEFT: (w0 - dx) / w0, HTRIGHT: (w0 + dx) / w0,
          HTTOPLEFT: (w0 - dx) / w0, HTBOTTOMLEFT: (w0 - dx) / w0,
          HTTOPRIGHT: (w0 + dx) / w0, HTBOTTOMRIGHT: (w0 + dx) / w0}.get(code)
    sy = {HTTOP: (h0 - dy) / h0, HTBOTTOM: (h0 + dy) / h0,
          HTTOPLEFT: (h0 - dy) / h0, HTTOPRIGHT: (h0 - dy) / h0,
          HTBOTTOMLEFT: (h0 + dy) / h0, HTBOTTOMRIGHT: (h0 + dy) / h0}.get(code)
    candidates = [s for s in (sx, sy) if s is not None]
    s = max(candidates)
    s = max(s, min_w / w0, min_h / h0)          # 최소 크기
    w, h = round(w0 * s), round(h0 * s)
    # 반대쪽 변을 고정
    x = x0 + w0 - w if code in (HTLEFT, HTTOPLEFT, HTBOTTOMLEFT) else x0
    y = y0 + h0 - h if code in (HTTOP, HTTOPLEFT, HTTOPRIGHT) else y0
    return x, y, w, h


def strip_rects(rect, scale=1.0):
    """mpv 창 rect=(l, t, r, b) 둘레에 놓을 8개 띠의 (코드, x, y, w, h)."""
    l, t, r, b = rect
    o, i, c = round(OUTER * scale), round(INNER * scale), round(CORNER_INNER * scale)
    return [
        (HTLEFT, l - o, t + c, o + i, (b - t) - 2 * c),
        (HTRIGHT, r - i, t + c, o + i, (b - t) - 2 * c),
        (HTTOP, l + c, t - o, (r - l) - 2 * c, o + i),
        (HTBOTTOM, l + c, b - i, (r - l) - 2 * c, o + i),
        (HTTOPLEFT, l - o, t - o, o + c, o + c),
        (HTTOPRIGHT, r - c, t - o, o + c, o + c),
        (HTBOTTOMLEFT, l - o, b - c, o + c, o + c),
        (HTBOTTOMRIGHT, r - c, b - c, o + c, o + c),
    ]


# ---------------------------------------------------------------- Win32 ----
user32 = ctypes.WinDLL("user32", use_last_error=True)     # 앱의 다른 코드와 함수 설정이 섞이지 않게 전용 핸들
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsZoomed.argtypes = [wt.HWND]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.SetCapture.argtypes = [wt.HWND]
user32.SetCursor.argtypes = [wt.HANDLE]
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetDpiForWindow.argtypes = [wt.HWND]
user32.GetDpiForWindow.restype = wt.UINT
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.GetStockObject.restype = wt.HANDLE

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x8, 0x80, 0x80000, 0x08000000
LWA_ALPHA = 0x2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x4, 0x10, 0x40
SW_HIDE = 0
HWND_TOPMOST = wt.HWND(-1)
WM_SETCURSOR, WM_MOUSEACTIVATE = 0x20, 0x21
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_CAPTURECHANGED = 0x200, 0x201, 0x202, 0x215
MK_LBUTTON = 0x1
MA_NOACTIVATE = 3
IDC = {HTLEFT: 32644, HTRIGHT: 32644, HTTOP: 32645, HTBOTTOM: 32645,
       HTTOPLEFT: 32642, HTBOTTOMRIGHT: 32642, HTTOPRIGHT: 32643, HTBOTTOMLEFT: 32643}   # 가로/세로/\ /
PER_MONITOR_V2 = ctypes.c_void_p(-4)

CLASS_NAME = "YtPipResizeGrip"
_registered = False
_wndproc_ref = None          # 콜백이 가비지 컬렉션 되지 않게 붙들어 둔다
_instance = None


class _aware:
    """이 블록 안에서는 화면 배율과 상관없이 실제(물리) 픽셀 좌표로 창 API 를 쓴다 (mpv 창 좌표와 맞추기 위해)"""
    def __enter__(self):
        try:
            self.old = user32.SetThreadDpiAwarenessContext(PER_MONITOR_V2)
        except Exception:
            self.old = None
        return self

    def __exit__(self, *exc):
        if self.old:
            try:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(self.old))
            except Exception:
                pass


def find_window(pid):
    """pid 프로세스의 눈에 보이는 최상위 창"""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _lp):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def _wndproc(hwnd, msg, wparam, lparam):
    try:
        inst = _instance
        if inst is not None:
            handled, result = inst.handle(hwnd, msg, wparam, lparam)
            if handled:
                return result
    except Exception:
        pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _register():
    global _registered, _wndproc_ref
    if _registered:
        return
    _wndproc_ref = WNDPROC(_wndproc)
    wc = WNDCLASSW()
    wc.lpfnWndProc = _wndproc_ref
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.hbrBackground = gdi32.GetStockObject(4)      # BLACK_BRUSH
    wc.lpszClassName = CLASS_NAME
    user32.RegisterClassW(ctypes.byref(wc))
    _registered = True


class Grips:
    """root: Tk 루트 (after 로 주기 호출), get_pid: mpv 프로세스 id 또는 None 을 돌려주는 함수,
    on_resized: 끌기가 끝나면 부르는 함수 (창 크기 기억용)"""

    def __init__(self, root, get_pid, on_resized=None, interval_ms=100):
        self.root, self.get_pid, self.on_resized, self.interval = root, get_pid, on_resized, interval_ms
        self.strips = {}            # hwnd -> 코드
        self.by_code = {}           # 코드 -> hwnd
        self.target = None          # 지금 붙어 있는 mpv 창
        self.drag = None
        self._placed = None
        self._after = None

    # --- 수명 ---
    def start(self):
        global _instance
        _instance = self
        self._tick()

    def close(self):
        global _instance
        if self._after:
            try:
                self.root.after_cancel(self._after)
            except Exception:
                pass
        for h in list(self.strips):
            user32.DestroyWindow(h)
        self.strips.clear(); self.by_code.clear()
        if _instance is self:
            _instance = None

    # --- 띠 만들기 / 따라다니기 ---
    def _ensure(self):
        if self.strips:
            return
        _register()
        with _aware():
            for code in (HTLEFT, HTRIGHT, HTTOP, HTBOTTOM, HTTOPLEFT, HTTOPRIGHT, HTBOTTOMLEFT, HTBOTTOMRIGHT):
                h = user32.CreateWindowExW(
                    WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE, CLASS_NAME, "",
                    WS_POPUP, 0, 0, 1, 1, None, None, kernel32.GetModuleHandleW(None), None)
                if h:
                    user32.SetLayeredWindowAttributes(h, 0, 1, LWA_ALPHA)    # 1/255: 안 보이지만 마우스는 받는다
                    self.strips[h] = code
                    self.by_code[code] = h

    def _hide(self):
        if self._placed is not None:
            for h in self.strips:
                user32.ShowWindow(h, SW_HIDE)
            self._placed = None

    def _raise(self):
        """mpv 창을 클릭하면 mpv 가 띠 위로 올라와서 창 안쪽에 걸친 띠가 가려진다. 띠를 매번 다시 맨 위로 올린다."""
        for h in self.strips:
            user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _is_fullscreen(self, hwnd, rect):
        mon = user32.MonitorFromWindow(hwnd, 2)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return False
        m = info.rcMonitor
        return rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom

    def _tick(self):
        try:
            if self.drag is None:
                self._sync()
        except Exception:
            pass
        finally:
            self._after = self.root.after(self.interval, self._tick)

    def _sync(self):
        pid = self.get_pid()
        with _aware():
            hwnd = find_window(pid) if pid else None
            rc = wt.RECT()
            if (not hwnd or user32.IsIconic(hwnd) or user32.IsZoomed(hwnd)
                    or not user32.GetWindowRect(hwnd, ctypes.byref(rc)) or self._is_fullscreen(hwnd, rc)):
                self.target = None
                self._hide()
                return
            self.target = hwnd
            rect = (rc.left, rc.top, rc.right, rc.bottom)
            scale = (user32.GetDpiForWindow(hwnd) or 96) / 96
            key = (rect, scale)
            if key == self._placed:
                self._raise()
                return
            self._ensure()
            for code, x, y, w, h in strip_rects(rect, scale):
                user32.SetWindowPos(self.by_code[code], HWND_TOPMOST, x, y, max(1, w), max(1, h),
                                    SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._placed = key

    # --- 마우스 ---
    def handle(self, hwnd, msg, wparam, lparam):
        code = self.strips.get(hwnd)
        if code is None:
            return False, 0
        if msg == WM_SETCURSOR:
            user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC[code])))
            return True, 1
        if msg == WM_MOUSEACTIVATE:
            return True, MA_NOACTIVATE
        if msg == WM_LBUTTONDOWN:
            self._begin(hwnd, code, lparam)
            return True, 0
        if msg == WM_MOUSEMOVE and self.drag is not None and (wparam & MK_LBUTTON):
            self._move(hwnd, lparam)
            return True, 0
        if msg in (WM_LBUTTONUP, WM_CAPTURECHANGED):
            self._end()
            return True, 0
        return False, 0

    @staticmethod
    def _screen_pt(hwnd, lparam):
        pt = wt.POINT(ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)
        with _aware():
            user32.ClientToScreen(hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def _begin(self, hwnd, code, lparam):
        if not self.target:
            return
        with _aware():
            rc = wt.RECT()
            if not user32.GetWindowRect(self.target, ctypes.byref(rc)):
                return
            dpi = (user32.GetDpiForWindow(self.target) or 96) / 96
        x0, y0 = self._screen_pt(hwnd, lparam)
        self.drag = {"code": code, "x": x0, "y": y0, "dpi": dpi,
                     "rect": (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)}
        user32.SetCapture(hwnd)

    def _move(self, hwnd, lparam):
        d = self.drag
        x, y = self._screen_pt(hwnd, lparam)
        nx, ny, nw, nh = compute_rect(d["code"], d["rect"], x - d["x"], y - d["y"],
                                      round(MIN_W * d["dpi"]), round(MIN_H * d["dpi"]))
        with _aware():
            user32.SetWindowPos(self.target, None, nx, ny, nw, nh, SWP_NOZORDER | SWP_NOACTIVATE)

    def _end(self):
        if self.drag is None:
            return
        self.drag = None
        self._placed = None            # 다음 주기에 띠를 새 창 위치로 옮긴다
        try:
            ctypes.windll.user32.ReleaseCapture()
        except Exception:
            pass
        if self.on_resized:
            try:
                self.on_resized()
            except Exception:
                pass
