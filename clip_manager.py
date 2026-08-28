import audioop
import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import wave
import winsound
from collections import Counter, deque
from contextlib import suppress
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from typing import TYPE_CHECKING

if __name__ != "__main__":
    import obspython as obs

if TYPE_CHECKING:
    # OBS injects script_path() into this module's namespace at load; there is nothing to import.
    def script_path() -> str: ...


# -------------------- toast popup (runs as a separate process) --------------------
# Per-pixel alpha via Win32 UpdateLayeredWindow, not tkinter chroma-key, for real anti-aliasing and a soft shadow without a fringe. Requires Pillow in OBS's configured Python: pythonw.exe -m pip install pillow
TOASTS = {
    "replay_saved": {"icon": "success", "self_contained": True, "text": "Replay saved"},
    "replay_failed": {
        "icon": "failure",
        "self_contained": True,
        "text": "Saving failed",
    },
    "buffer_on": {"icon": "record", "self_contained": True, "text": "Replay on"},
    "buffer_off": {
        "icon": "record-slash",
        "self_contained": True,
        "text": "Replay off",
    },
    "game_on": {
        "icon": "game-capture-on",
        "self_contained": False,
        "badge": "#3b82f6",
        "text": "Game clipping on",
    },
    "game_off": {
        "icon": "game-capture-off",
        "self_contained": False,
        "badge": "#6b7280",
        "text": "Game clipping off",
    },
    "game_failed": {
        "icon": "failure",
        "self_contained": True,
        "text": "No game to capture",
    },
    "warning": {"icon": "warning", "self_contained": True, "text": "Warning"},
    "info": {"icon": "info", "self_contained": True, "text": "Info"},
    "desktop_on": {
        "icon": "display-capture-on",
        "self_contained": False,
        "badge": "#3b82f6",
        "text": "Desktop capture on",
    },
    "desktop_off": {
        "icon": "display-capture-off",
        "self_contained": False,
        "badge": "#6b7280",
        "text": "Desktop capture off",
    },
    "mic_on": {
        "icon": "microphone-on",
        "self_contained": False,
        "badge": "#3b82f6",
        "text": "Microphone on",
    },
    "mic_off": {
        "icon": "microphone-off",
        "self_contained": False,
        "badge": "#6b7280",
        "text": "Microphone off",
    },
    "monitor_on": {
        "icon": "headset-monitor-on",
        "self_contained": False,
        "badge": "#3b82f6",
        "text": "Listening on",
    },
    "monitor_off": {
        "icon": "headset-monitor-off",
        "self_contained": False,
        "badge": "#6b7280",
        "text": "Listening off",
    },
}
SOUND_DEFAULT_FILES = {
    "replay_saved": "success.wav",
    "replay_failed": "failure.wav",
    "buffer_on": "on.wav",
    "buffer_off": "off.wav",
    "game_on": "on.wav",
    "game_off": "off.wav",
    "game_failed": "failure.wav",
    "warning": "failure.wav",
    "info": "on.wav",
    "desktop_on": "on.wav",
    "desktop_off": "off.wav",
    "mic_on": "on.wav",
    "mic_off": "off.wav",
    "monitor_on": "on.wav",
    "monitor_off": "off.wav",
}
ICON_CANVAS = 512
ICON_CONTENT_REF = 410  # measured content diameter shared across all the icon assets
# Self-contained icons (success/failure/record/record-slash) include their own circle; scale 2.0 matches that circle exactly to the drawn circle behind glyph-only icons.
SELF_CONTAINED_ICON_SCALE = 2.0
GLYPH_ICON_SCALE = 1.1
# Only the "on" (slash-free) variant's bbox is trustworthy for measuring an off-center glyph - the "off" variant's bbox is dominated by its symmetric diagonal slash regardless of where the glyph sits.
ICON_Y_OFFSET_SRC_PX = {"display-capture-on": 12.5, "display-capture-off": 12.5}
ICON_Y_NUDGE_PX = (
    1  # applies to every icon, not just the ones with a measured offset above
)
TOAST_HOLD_SECONDS = 2.0
TOAST_MAX_SLOTS = 8
STATE_POLL_SECONDS = 0.1
TOAST_MUTEX_NAME = "Local\\ClipManagerToastStateMutex"
SCRIPT_VERSION = "0.4.5"

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\seguisb.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]

if __name__ == "__main__":
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter

    # Physical-pixel rendering so the toast isn't DWM-upscaled (soft, undersized) on displays over 100% scaling; safe only because this is a standalone process, not OBS's embedded Python.
    with suppress(Exception):
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    def round_px(x):
        # Round-half-up, not Python's round-half-to-even, so a .5 doesn't break differently between otherwise-identical toasts depending on parity alone.
        return math.floor(x + 0.5)

    def hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))

    def load_font(size):
        for path in FONT_CANDIDATES:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()

    def thick_line(draw, points, width, fill):
        pts = list(points)
        draw.line(pts, fill=fill, width=width, joint="curve" if len(pts) > 2 else None)
        r = width / 2
        for x, y in (pts[0], pts[-1]):
            draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)

    def load_icon(name):
        return Image.open(Path(__file__).with_name("icons") / f"{name}.png").convert(
            "RGBA"
        )

    def paste_icon(base, icon_name, cx, cy, target_diameter, ss):
        icon = load_icon(icon_name)
        scale = target_diameter / ICON_CONTENT_REF
        size = round_px(ICON_CANVAS * scale)
        icon = icon.resize((size, size), Image.LANCZOS)
        y_correction = round_px(ICON_Y_OFFSET_SRC_PX.get(icon_name, 0) * scale)
        y_nudge = round_px(ICON_Y_NUDGE_PX * ss)
        dest_x = round_px(cx - size / 2)
        dest_y = round_px(cy - size / 2) - y_correction + y_nudge
        base.alpha_composite(icon, dest=(dest_x, dest_y))

    def render_toast(spec, wnd_h, ss=3):
        pad = round_px(wnd_h * 16 / 96)
        rect_h = wnd_h - 2 * pad
        corner_r = rect_h * 0.125
        badge_r = rect_h * (18 / 64)
        divider_x = pad + rect_h
        divider_inset = rect_h * 0.25
        text_x = divider_x + rect_h * 0.1875
        font_size = max(round_px(rect_h * (18 / 64)), 10)

        S = lambda v: round_px(v * ss)
        font = load_font(S(font_size))
        # Auto-size width to the text, with the right inset matching the badge's left inset.
        tb = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), spec["text"], font=font)
        content_inset = rect_h * (0.5 - 18 / 64)
        rect_w = round_px((text_x - pad) + (tb[2] - tb[0]) / ss + content_inset)
        wnd_w = rect_w + 2 * pad

        shadow_margin = round_px(wnd_h * 0.35)
        shadow_dy = round_px(wnd_h * 0.06)
        shadow_blur = round_px(wnd_h * 0.13)

        canvas_w, canvas_h = wnd_w + 2 * shadow_margin, wnd_h + 2 * shadow_margin

        base = Image.new("RGBA", (S(canvas_w), S(canvas_h)), (0, 0, 0, 0))

        shadow = Image.new("RGBA", (S(canvas_w), S(canvas_h)), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (
                S(shadow_margin + pad),
                S(shadow_margin + pad + shadow_dy),
                S(shadow_margin + pad + rect_w),
                S(shadow_margin + pad + rect_h + shadow_dy),
            ),
            radius=S(corner_r),
            fill=(0, 0, 0, 140),
        )
        base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(S(shadow_blur))))

        mask = Image.new("L", (S(rect_w), S(rect_h)), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, S(rect_w) - 1, S(rect_h) - 1), radius=S(corner_r), fill=255
        )
        grad = Image.new("RGB", (S(rect_w), S(rect_h)))
        top_c, bot_c = (40, 40, 43), (20, 20, 22)
        gd = ImageDraw.Draw(grad)
        for y in range(S(rect_h)):
            t = y / max(S(rect_h) - 1, 1)
            gd.line(
                [(0, y), (S(rect_w), y)],
                fill=tuple(
                    round_px(top_c[i] + (bot_c[i] - top_c[i]) * t) for i in range(3)
                ),
            )
        grad_rgba = grad.convert("RGBA")
        grad_rgba.putalpha(mask)
        base.alpha_composite(
            grad_rgba, dest=(S(shadow_margin + pad), S(shadow_margin + pad))
        )

        draw = ImageDraw.Draw(base)
        bcx, bcy = S(shadow_margin + pad + rect_h / 2), S(shadow_margin + wnd_h / 2)
        br = S(badge_r)
        if spec["self_contained"]:
            # Matches the glyph-only badge circle's diameter exactly, so every badge is the same size.
            paste_icon(
                base,
                spec["icon"],
                bcx,
                bcy,
                target_diameter=SELF_CONTAINED_ICON_SCALE * br,
                ss=ss,
            )
        else:
            draw.ellipse(
                (bcx - br, bcy - br, bcx + br, bcy + br), fill=hex_to_rgb(spec["badge"])
            )
            paste_icon(
                base,
                spec["icon"],
                bcx,
                bcy,
                target_diameter=GLYPH_ICON_SCALE * br,
                ss=ss,
            )

        dx = S(shadow_margin + divider_x)
        thick_line(
            draw,
            [
                (dx, S(shadow_margin + pad + divider_inset)),
                (dx, S(shadow_margin + pad + rect_h - divider_inset)),
            ],
            S(2),
            (110, 110, 115, 255),
        )

        center_y = S(shadow_margin + wnd_h / 2)
        text_x_px = S(shadow_margin + text_x)
        # Fixed-reference vertical centering (not each string's own bbox), so labels with and without descenders land on an identical row instead of visibly jumping between toasts.
        ref_bbox = draw.textbbox((0, 0), "Ag", font=font)
        ty = center_y - (ref_bbox[1] + ref_bbox[3]) / 2 + S(2)
        bbox = draw.textbbox((0, 0), spec["text"], font=font)
        draw.text(
            (text_x_px - bbox[0], ty),
            spec["text"],
            font=font,
            fill=(255, 255, 255, 255),
        )

        return (
            base.resize((canvas_w, canvas_h), Image.LANCZOS),
            shadow_margin,
            rect_h,
            wnd_w,
        )

    def to_premultiplied_bgra(img: "Image.Image") -> bytes:
        r, g, b, a = img.split()
        premul = Image.merge(
            "RGBA",
            (
                ImageChops.multiply(b, a),
                ImageChops.multiply(g, a),
                ImageChops.multiply(r, a),
                a,
            ),
        )
        return premul.tobytes("raw", "RGBA")

    # -------------------- Win32 layered window --------------------
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", ctypes.c_ubyte),
            ("BlendFlags", ctypes.c_ubyte),
            ("SourceConstantAlpha", ctypes.c_ubyte),
            ("AlphaFormat", ctypes.c_ubyte),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOPMOST = 0x00000008
    WS_POPUP = 0x80000000
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    ULW_ALPHA = 0x00000002
    AC_SRC_OVER = 0x00
    AC_SRC_ALPHA = 0x01
    BI_RGB = 0
    HWND_TOPMOST = wintypes.HWND(-1)
    SW_SHOWNA = 8

    # Explicit argtypes throughout - ctypes' default marshalling can silently truncate pointer-sized values on 64-bit Windows, a nastier failure mode than a clean exception.
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND,
        wintypes.HDC,
        ctypes.POINTER(POINT),
        ctypes.POINTER(SIZE),
        wintypes.HDC,
        ctypes.POINTER(POINT),
        wintypes.COLORREF,
        ctypes.POINTER(BLENDFUNCTION),
        wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL

    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL

    gdi32.CreateRectRgn.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdi32.CreateRectRgn.restype = wintypes.HRGN
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    user32.SetWindowRgn.restype = ctypes.c_int

    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    WAIT_OBJECT_0 = 0x0
    WAIT_ABANDONED = 0x80
    WAIT_INFINITE = 0xFFFFFFFF

    def acquire_state_lock():
        # A named mutex (visible across processes by name) prevents two same-kind toasts from both reading "nothing showing yet" and both creating an orphaned window.
        mutex = kernel32.CreateMutexW(None, False, TOAST_MUTEX_NAME)
        result = kernel32.WaitForSingleObject(mutex, WAIT_INFINITE)
        if result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            # Never took ownership, so close without ReleaseMutex, which on an unowned mutex can corrupt another process's lock.
            kernel32.CloseHandle(mutex)
            return None
        return mutex

    def release_state_lock(mutex):
        if mutex is None:
            return
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)

    def push_bitmap(hwnd, img, x, y):
        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = img.width
        bmi.bmiHeader.biHeight = (
            -img.height
        )  # negative = top-down, matches PIL row order
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        bits_ptr = ctypes.c_void_p()
        hbitmap = gdi32.CreateDIBSection(
            hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(bits_ptr), None, 0
        )
        if not hbitmap or not bits_ptr.value:
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
            return
        old_bitmap = gdi32.SelectObject(hdc_mem, hbitmap)

        pixel_data = to_premultiplied_bgra(img)
        ctypes.memmove(bits_ptr, pixel_data, len(pixel_data))

        size = SIZE(img.width, img.height)
        pos = POINT(x, y)
        src_pos = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

        user32.UpdateLayeredWindow(
            hwnd,
            hdc_screen,
            ctypes.byref(pos),
            ctypes.byref(size),
            hdc_mem,
            ctypes.byref(src_pos),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )

        gdi32.SelectObject(hdc_mem, old_bitmap)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

    def create_layered_window(img, x, y):
        hInstance = kernel32.GetModuleHandleW(None)
        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
            "STATIC",
            "",
            WS_POPUP,
            x,
            y,
            img.width,
            img.height,
            None,
            None,
            hInstance,
            None,
        )
        if not hwnd:
            return None
        push_bitmap(hwnd, img, x, y)
        user32.ShowWindow(hwnd, SW_SHOWNA)
        return hwnd

    def clip_to_primary_monitor(hwnd, x, y, w, h, screen_w, screen_h):
        # Clips to GetSystemMetrics(0/1) so mid-slide and off-screen animation positions don't land on an adjacent monitor in an extended desktop.
        left = max(0, -x)
        top = max(0, -y)
        right = max(left, min(w, screen_w - x))
        bottom = max(top, min(h, screen_h - y))
        if left == 0 and top == 0 and right >= w and bottom >= h:
            user32.SetWindowRgn(hwnd, None, True)
            return
        hrgn = gdi32.CreateRectRgn(left, top, right, bottom)
        if not user32.SetWindowRgn(hwnd, hrgn, True):
            gdi32.DeleteObject(hrgn)

    def move_window(hwnd, x, y, w, h, screen_w, screen_h):
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        clip_to_primary_monitor(hwnd, x, y, w, h, screen_w, screen_h)

    def ease_out_cubic(t):
        return 1 - (1 - t) ** 3

    def animate_slide(
        hwnd, x_from, x_to, y, w, h, screen_w, screen_h, duration=0.18, steps=24
    ):
        for i in range(steps + 1):
            t = ease_out_cubic(i / steps)
            move_window(
                hwnd,
                round_px(x_from + (x_to - x_from) * t),
                y,
                w,
                h,
                screen_w,
                screen_h,
            )
            time.sleep(duration / steps)

    # -------------------- cross-process coordination --------------------
    def state_file_path() -> Path:
        return Path(__file__).with_name("toast_state.json")

    def read_state() -> dict:
        with suppress(Exception):
            return json.loads(state_file_path().read_text())
        return {}

    def write_state(state: dict):
        tmp = state_file_path().with_name(f"toast_state.{os.getpid()}.tmp")
        with suppress(Exception):
            tmp.write_text(json.dumps(state))
            os.replace(tmp, state_file_path())

    def prune_dead_entries(state: dict) -> dict:
        return {k: v for k, v in state.items() if user32.IsWindow(v["hwnd"])}

    def show_toast(kind: str, toast_key: str, cfg: dict):
        spec = TOASTS.get(toast_key)
        if not spec:
            return
        if cfg.get("text_override"):
            spec = {**spec, "text": cfg["text_override"]}

        vpos = cfg.get("vpos") or "top"
        hpos = cfg.get("hpos") or "right"
        scale = cfg.get("scale") or 1.0
        hold_seconds = cfg.get("hold_seconds") or TOAST_HOLD_SECONDS
        # `or` would wrongly discard an explicit 0ms (instant slide), which the UI allows.
        slide_in_ms = cfg.get("slide_in_ms")
        slide_out_ms = cfg.get("slide_out_ms")
        slide_in_s = (180 if slide_in_ms is None else slide_in_ms) / 1000
        slide_out_s = (150 if slide_out_ms is None else slide_out_ms) / 1000

        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        base_wnd_h = cfg.get("height_override") or screen_h / 12
        wnd_h = round_px(base_wnd_h * scale)

        img, shadow_margin, rect_h, wnd_w = render_toast(spec, wnd_h)
        margin = cfg.get("edge_margin")
        margin_x = margin_y = margin if margin is not None else round_px(screen_h / 80)
        # Visible gap is (stack_step - rect_h), since rect_h already excludes the pill's own internal padding.
        desired_gap = cfg.get("stack_gap")
        if desired_gap is None:
            desired_gap = margin_y / 2
        stack_step = rect_h + desired_gap

        if hpos == "left":
            target_x = margin_x - shadow_margin
        elif hpos == "center":
            target_x = (screen_w - wnd_w) // 2 - shadow_margin
        else:
            target_x = screen_w - wnd_w - margin_x - shadow_margin
        start_x = -img.width if hpos == "left" else screen_w

        # Decide-and-create happens inside the cross-process lock - two same-kind toasts arriving together must not both conclude "nothing showing yet".
        mutex = acquire_state_lock()
        try:
            state = prune_dead_entries(read_state())
            existing = state.get(kind)
            if existing:
                push_bitmap(existing["hwnd"], img, existing["x"], existing["y"])
                existing["last_update"] = time.time()
                state[kind] = existing
                write_state(state)
                hwnd = None
            else:
                used_slots = {v["slot"] for v in state.values()}
                slot = next(
                    (s for s in range(TOAST_MAX_SLOTS) if s not in used_slots), 0
                )
                stack_offset = slot * stack_step
                if vpos == "top":
                    target_y = round_px(margin_y - shadow_margin + stack_offset)
                else:
                    target_y = round_px(
                        screen_h - wnd_h - margin_y - shadow_margin - stack_offset
                    )
                hwnd = create_layered_window(img, start_x, target_y)
                if hwnd:
                    state[kind] = {
                        "hwnd": hwnd,
                        "x": target_x,
                        "y": target_y,
                        "slot": slot,
                        "last_update": time.time(),
                    }
                    write_state(state)
        finally:
            release_state_lock(mutex)

        if not hwnd:
            return  # reused an existing toast, or the window failed to create - nothing to own

        animate_slide(
            hwnd,
            start_x,
            target_x,
            target_y,
            img.width,
            img.height,
            screen_w,
            screen_h,
            duration=slide_in_s,
        )

        while True:
            time.sleep(STATE_POLL_SECONDS)
            entry = read_state().get(kind)
            # A missing/changed entry means the state file was lost or reassigned; still tear down the window this process owns.
            if not entry or entry.get("hwnd") != hwnd:
                break
            if time.time() - entry["last_update"] >= hold_seconds:
                break

        animate_slide(
            hwnd,
            target_x,
            start_x,
            target_y,
            img.width,
            img.height,
            screen_w,
            screen_h,
            duration=slide_out_s,
        )
        user32.DestroyWindow(hwnd)

        mutex = acquire_state_lock()
        try:
            state = read_state()
            if state.get(kind, {}).get("hwnd") == hwnd:
                del state[kind]
                write_state(state)
        finally:
            release_state_lock(mutex)

    if len(sys.argv) > 3:
        show_toast(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
    sys.exit(0)


# -------------------- constants / settings keys --------------------
class CONSTANTS:
    FILENAME_PROHIBITED_CHARS = r'/\:"<>*?|%'
    PATH_PROHIBITED_CHARS = r'"<>*?|%'
    DEFAULT_FILENAME_FORMAT = "%NAME_%d.%m.%Y_%H-%M-%S"


class ConfigTypes:
    PROFILE = 0
    USER = 1


class VARIABLES:
    script_settings = None
    hotkey_ids: dict = {}
    aliases: dict[Path, str] = {}
    clip_exe_history: deque | None = None
    mic_signal_handler = None
    buffer_restart_depth = 0
    buffer_restart_lock = Lock()
    auto_buffer_exceptions: set = set()
    # The info dict holds an open SYNCHRONIZE handle so the PID can't be recycled while tracked.
    linked_games: dict = {}
    handled_games: dict = {}
    game_empty_since = 0.0
    pending_hook_pid = 0
    display_override = False
    display_override_deadline = 0.0
    sanity_warned: set = set()  # messages already surfaced this session, to avoid re-nagging
    disk_over_limit = False  # latch so the folder-size warning fires once per threshold crossing
    disk_warn_msg = ""  # set by the walk thread, surfaced (and cleared) on the OBS thread
    last_disk_check = 0.0
    buffer_start_at = 0.0  # last replay_buffer_start() call, so reconcile_buffer waits it out


class PN:
    GR_PATHS = "gr_paths"
    GR_SOURCES = "gr_sources"
    GR_NOTIFICATIONS = "gr_notifications"
    GR_POPUP = "gr_popup"
    GR_SOUNDS = "gr_sounds"
    GR_ALIASES = "gr_aliases"
    GR_CAPTURE = "gr_capture"
    GR_AUTO_BUFFER = "gr_auto_buffer"

    BASE_PATH = "base_path"
    FILENAME_TEMPLATE = "filename_template"
    REPLACE_SPACES = "replace_spaces"

    CLIP_FOLDER_WARN_GB = "clip_folder_warn_gb"

    GAME_SOURCE_NAME = "game_source_name"
    DESKTOP_SOURCE_NAME = "desktop_source_name"
    MIC_SOURCE_NAME = "mic_source_name"
    DISPLAY_CLIP_FOLDER = "display_clip_folder"

    VOLUME_OFFSET_DB = "volume_offset_db"

    NOTIFY_CLIP_SOUND = "notify_clip_sound"
    NOTIFY_CLIP_POPUP = "notify_clip_popup"
    NOTIFY_TOGGLE_SOUND = "notify_toggle_sound"
    NOTIFY_TOGGLE_POPUP = "notify_toggle_popup"
    NOTIFY_CLIP_DISABLED = "notify_clip_disabled"

    POPUP_VPOS = "popup_vpos"
    POPUP_HPOS = "popup_hpos"
    POPUP_SCALE = "popup_scale"
    POPUP_HEIGHT_OVERRIDE = "popup_height_override"
    POPUP_EDGE_MARGIN = "popup_edge_margin"
    POPUP_STACK_GAP = "popup_stack_gap"
    POPUP_HOLD_SECONDS = "popup_hold_seconds"
    POPUP_SLIDE_IN_MS = "popup_slide_in_ms"
    POPUP_SLIDE_OUT_MS = "popup_slide_out_ms"

    ALIASES_LIST = "aliases_list"

    RESTART_BUFFER = "restart_buffer"
    RESTART_BUFFER_LOOP = "restart_buffer_loop"

    AUTO_GAME_CLIPPING = "auto_buffer_fullscreen"
    GAME_POLL_MS = "auto_buffer_poll_ms"
    GAME_EXIT_GRACE_S = "auto_buffer_stop_delay_s"
    AUTO_BUFFER_EXCEPTIONS = "auto_buffer_exceptions"
    AUTO_SWITCH_ON_HOOK_FAIL = "auto_switch_on_hook_fail"
    DISPLAY_OVERRIDE_AUTOOFF_MIN = "display_override_autooff_min"

    HK_GAME_ON = "hk_game_on"
    HK_GAME_OFF = "hk_game_off"
    HK_GAME_TOGGLE = "hk_game_toggle"
    HK_DISPLAY_ON = "hk_display_on"
    HK_DISPLAY_OFF = "hk_display_off"
    HK_DISPLAY_TOGGLE = "hk_display_toggle"
    HK_TOGGLE_MIC = "hk_toggle_mic"
    HK_TOGGLE_MONITOR = "hk_toggle_monitor"


def _print(*values):
    print(f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}]", *values)


def get_script_dir() -> Path:
    return Path(script_path())


# -------------------- windows process detection --------------------
# Explicit argtypes/restype throughout - ctypes' default int marshalling silently truncates handles and 64-bit counters on Win64.
user32_win = ctypes.windll.user32
kernel32_win = ctypes.windll.kernel32

SW_SHOWMAXIMIZED = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


user32_win.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32_win.GetWindowRect.restype = wintypes.BOOL
user32_win.GetSystemMetrics.argtypes = [ctypes.c_int]
user32_win.GetSystemMetrics.restype = ctypes.c_int
user32_win.GetForegroundWindow.restype = wintypes.HWND
user32_win.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32_win.GetClassNameW.restype = ctypes.c_int
user32_win.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]
user32_win.GetWindowThreadProcessId.restype = wintypes.DWORD
user32_win.GetWindowPlacement.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(WINDOWPLACEMENT),
]
user32_win.GetWindowPlacement.restype = wintypes.BOOL
user32_win.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32_win.GetLastInputInfo.restype = wintypes.BOOL
kernel32_win.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32_win.OpenProcess.restype = wintypes.HANDLE
kernel32_win.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32_win.CloseHandle.restype = wintypes.BOOL
kernel32_win.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32_win.WaitForSingleObject.restype = wintypes.DWORD
kernel32_win.GetTickCount64.restype = ctypes.c_ulonglong
kernel32_win.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32_win.QueryFullProcessImageNameW.restype = wintypes.BOOL


def get_active_window_pid() -> int:
    hwnd = user32_win.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32_win.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def get_window_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32_win.GetClassNameW(hwnd, buf, 256)
    return buf.value


def get_executable_path(pid: int) -> Path:
    # QueryFullProcessImageNameW + PROCESS_QUERY_LIMITED_INFORMATION resolves elevated/anti-cheat processes that GetModuleFileNameEx + VM_READ cannot.
    handle = kernel32_win.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise OSError(f"Process {pid} does not exist.")
    buf = ctypes.create_unicode_buffer(
        32767
    )  # not MAX_PATH (260) - long install paths are common (Steam, OneDrive)
    size = wintypes.DWORD(32767)
    ok = kernel32_win.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    kernel32_win.CloseHandle(handle)
    if not ok:
        raise RuntimeError(f"Cannot get executable path for process {pid}.")
    return Path(buf.value)


def get_time_since_last_input() -> int:
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if user32_win.GetLastInputInfo(ctypes.byref(info)):
        return (kernel32_win.GetTickCount64() - info.dwTime) // 1000
    return 0


def open_tracking_handle(pid: int) -> int:
    # SYNCHRONIZE lets WaitForSingleObject report exit; holding the handle blocks PID reuse.
    return kernel32_win.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )


def process_exited(handle: int) -> bool:
    return kernel32_win.WaitForSingleObject(handle, 0) == 0  # WAIT_OBJECT_0


def play_sound(path: str):
    if path:
        with suppress(Exception):
            winsound.PlaySound(path, winsound.SND_ASYNC)


def play_sound_with_gain(path: str, db_offset: float):
    if not path or not os.path.exists(path):
        return
    if abs(db_offset) < 0.01:
        play_sound(path)
        return
    # Own fd-backed file since winsound plays by reference (SND_FILENAME) - a shared temp path could be overwritten mid-playback.
    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="clip_manager_gain_")
    os.close(fd)
    try:
        with wave.open(path, "rb") as wf:
            params = wf.getparams()
            frames = wf.readframes(wf.getnframes())
        adjusted = audioop.mul(frames, params.sampwidth, 10 ** (db_offset / 20))
        with wave.open(tmp_path, "wb") as wf_out:
            wf_out.setparams(params)
            wf_out.writeframes(adjusted)
        play_sound(tmp_path)
    except Exception:
        _print("Failed to apply volume offset, playing sound unmodified.")
        _print(traceback.format_exc())
        with suppress(Exception):
            os.remove(tmp_path)
        play_sound(path)
        return
    # SND_ASYNC returns immediately, so delete only after playback would have finished.
    clip_seconds = params.nframes / params.framerate if params.framerate else 10
    Thread(
        target=_delete_after_delay, args=(tmp_path, clip_seconds + 2), daemon=True
    ).start()


def _delete_after_delay(path: str, delay: float):
    time.sleep(delay)
    with suppress(Exception):
        os.remove(path)


def sound_setting_name(toast_key: str) -> str:
    return f"sound_{toast_key}"


def get_sound_path(toast_key: str) -> str:
    s = VARIABLES.script_settings
    custom = obs.obs_data_get_string(s, sound_setting_name(toast_key)) if s else ""
    if custom:
        return custom
    default_name = SOUND_DEFAULT_FILES.get(toast_key, "")
    return str(get_script_dir() / "sounds" / default_name) if default_name else ""


def play_notification_sound(toast_key: str):
    if not VARIABLES.script_settings:
        return
    db_offset = obs.obs_data_get_double(VARIABLES.script_settings, PN.VOLUME_OFFSET_DB)
    play_sound_with_gain(get_sound_path(toast_key), db_offset)


def get_pythonw_path() -> str:
    # Must match the Python OBS itself was pointed at (Tools > Python Settings), not sys.exec_prefix - a mismatch here caused past "Could not load library" failures.
    base = get_obs_config("Python", "Path64bit", str, ConfigTypes.USER)
    if not base:
        _print("OBS Python path (Tools > Scripting > Python Settings) is not set; popups cannot render.")
    return os.path.join(base, "pythonw.exe")


def show_popup(kind: str, toast_key: str, text_override: str = "", hold: float = 0.0):
    s = VARIABLES.script_settings
    cfg = {
        "vpos": obs.obs_data_get_string(s, PN.POPUP_VPOS) or "top",
        "hpos": obs.obs_data_get_string(s, PN.POPUP_HPOS) or "right",
        "scale": obs.obs_data_get_double(s, PN.POPUP_SCALE),
        "height_override": obs.obs_data_get_int(s, PN.POPUP_HEIGHT_OVERRIDE),
        "edge_margin": obs.obs_data_get_int(s, PN.POPUP_EDGE_MARGIN),
        "stack_gap": obs.obs_data_get_int(s, PN.POPUP_STACK_GAP),
        "hold_seconds": hold or obs.obs_data_get_double(s, PN.POPUP_HOLD_SECONDS),
        "slide_in_ms": obs.obs_data_get_int(s, PN.POPUP_SLIDE_IN_MS),
        "slide_out_ms": obs.obs_data_get_int(s, PN.POPUP_SLIDE_OUT_MS),
        "text_override": text_override,
    }
    try:
        subprocess.Popen(
            [get_pythonw_path(), __file__, kind, toast_key, json.dumps(cfg)]
        )
    except Exception:
        _print(traceback.format_exc())


# -------------------- obs config helpers --------------------
OBS_VERSION_MAJOR = int(obs.obs_get_version_string().split(".")[0])


def get_obs_config(
    section: str,
    param: str,
    value_type: type = str,
    config_type: int = ConfigTypes.PROFILE,
):
    if config_type == ConfigTypes.PROFILE:
        cfg = obs.obs_frontend_get_profile_config()
    else:
        cfg = (
            obs.obs_frontend_get_user_config()
            if OBS_VERSION_MAJOR >= 31
            else obs.obs_frontend_get_global_config()
        )

    if not section or not param:
        return cfg
    fn = {
        str: obs.config_get_string,
        int: obs.config_get_int,
        bool: obs.config_get_bool,
    }[value_type]
    return fn(cfg, section, param)


def get_base_path(settings=None) -> Path:
    settings = settings if settings is not None else VARIABLES.script_settings
    # script_defaults() runs before script_settings is populated, and obs_data_get_string(None, ...) crashes OBS outright.
    script_path_setting = (
        obs.obs_data_get_string(settings, PN.BASE_PATH) if settings is not None else ""
    )
    if script_path_setting:
        return Path(script_path_setting)
    mode = get_obs_config("Output", "Mode")
    return Path(
        get_obs_config("SimpleOutput", "FilePath")
        if mode == "Simple"
        else get_obs_config("AdvOut", "RecFilePath")
    )


def get_replay_buffer_max_time() -> int:
    mode = get_obs_config("Output", "Mode")
    seconds = (
        get_obs_config("SimpleOutput", "RecRBTime", int)
        if mode == "Simple"
        else get_obs_config("AdvOut", "RecRBTime", int)
    )
    if not seconds:
        _print("Replay buffer length unreadable; assuming 300s.")
        return 300
    return seconds


def get_last_replay_file_name() -> str:
    output = obs.obs_frontend_get_replay_buffer_output()
    if not output:
        return ""
    cd = obs.calldata_create()
    try:
        obs.proc_handler_call(
            obs.obs_output_get_proc_handler(output), "get_last_replay", cd
        )
        return obs.calldata_string(cd, "path")
    finally:
        obs.calldata_destroy(cd)
        obs.obs_output_release(output)


# -------------------- aliases (path > name overrides for exe-derived names) --------------------
def load_aliases(script_settings_dict: dict):
    new_aliases = {}
    for entry in script_settings_dict.get(PN.ALIASES_LIST) or []:
        value = entry.get("value", "")
        if ">" not in value:
            continue
        path_str, name = (part.strip() for part in value.split(">", 1))
        path_str = os.path.expandvars(path_str)
        if any(c in path_str for c in CONSTANTS.PATH_PROHIBITED_CHARS):
            continue
        if any(c in name for c in CONSTANTS.FILENAME_PROHIBITED_CHARS):
            continue
        new_aliases[Path(path_str)] = name
    VARIABLES.aliases = new_aliases
    _print(f"{len(VARIABLES.aliases)} aliases loaded.")


def get_alias(executable_path: Path) -> str | None:
    if executable_path in VARIABLES.aliases:
        return VARIABLES.aliases[executable_path]
    for parent in executable_path.parents:
        if parent in VARIABLES.aliases:
            return VARIABLES.aliases[parent]
    return None


# -------------------- auto buffer (fullscreen detection + desktop capture link) --------------------
def load_auto_buffer_exceptions(settings):
    # Typed array accessor, not a JSON round-trip like load_aliases - a default-only (never explicitly saved) value may not appear via obs_data_get_json, and this is the one list here that ships with a non-empty default.
    if settings is None:
        return
    names = set()
    arr = obs.obs_data_get_array(settings, PN.AUTO_BUFFER_EXCEPTIONS)
    for i in range(obs.obs_data_array_count(arr)):
        item = obs.obs_data_array_item(arr, i)
        value = (obs.obs_data_get_string(item, "value") or "").strip().lower()
        if value:
            names.add(value)
        obs.obs_data_release(item)
    obs.obs_data_array_release(arr)
    VARIABLES.auto_buffer_exceptions = names


def is_exe_excepted(exe_path: Path) -> bool:
    name = exe_path.name.lower()
    full = str(exe_path).lower().replace("/", "\\")
    for entry in VARIABLES.auto_buffer_exceptions:
        e = entry.replace("/", "\\").rstrip("\\")
        if name == e or full == e or full.startswith(e + "\\"):
            return True
    return False


def is_foreground_window_fullscreen() -> tuple[bool, int]:
    hwnd = user32_win.GetForegroundWindow()
    if not hwnd:
        return False, 0
    # The desktop shell window (Progman/WorkerW) spans the screen and isn't maximized, so it clears every check below and gets hooked as a fullscreen game.
    if get_window_class(hwnd) in ("Progman", "WorkerW"):
        return False, 0
    pid = wintypes.DWORD()
    user32_win.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return False, pid.value  # OBS itself
    # A maximized window's rect also covers the screen, so exclude it - only borderless/exclusive fullscreen should count.
    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(WINDOWPLACEMENT)
    if (
        user32_win.GetWindowPlacement(hwnd, ctypes.byref(placement))
        and placement.showCmd == SW_SHOWMAXIMIZED
    ):
        return False, pid.value
    rect = wintypes.RECT()
    if not user32_win.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False, pid.value
    # Primary monitor only, not wherever the window happens to be - GetSystemMetrics(0/1) is the primary monitor's size with its top-left always at the desktop origin (0,0).
    primary_w, primary_h = user32_win.GetSystemMetrics(0), user32_win.GetSystemMetrics(
        1
    )
    fullscreen = (
        rect.left <= 0
        and rect.top <= 0
        and rect.right >= primary_w
        and rect.bottom >= primary_h
    )
    return fullscreen, pid.value


# -------------------- capture state machine --------------------
def _setting_str(key: str) -> str:
    return obs.obs_data_get_string(VARIABLES.script_settings, key)


def source_width(name: str) -> int:
    if not name:
        return 0
    src = obs.obs_get_source_by_name(name)
    if not src:
        return 0
    try:
        return obs.obs_source_get_width(src)
    finally:
        obs.obs_source_release(src)


def set_scene_source_visible(name: str, visible: bool):
    if not name:
        return
    scene_source = obs.obs_frontend_get_current_scene()
    if not scene_source:
        return
    try:
        item = obs.obs_scene_find_source(
            obs.obs_scene_from_source(scene_source), name
        )
        if item and obs.obs_sceneitem_visible(item) != visible:
            obs.obs_sceneitem_set_visible(item, visible)
    finally:
        obs.obs_source_release(scene_source)


def source_exists(name: str) -> bool:
    if not name:
        return False
    src = obs.obs_get_source_by_name(name)
    if src:
        obs.obs_source_release(src)
    return bool(src)


def source_in_current_scene(name: str) -> bool:
    scene_source = obs.obs_frontend_get_current_scene()
    if not scene_source:
        return False
    try:
        scene = obs.obs_scene_from_source(scene_source)
        return bool(obs.obs_scene_find_source(scene, name))
    finally:
        obs.obs_source_release(scene_source)


def warn_once(msg: str, toast_key: str = "warning"):
    if msg in VARIABLES.sanity_warned:
        return
    VARIABLES.sanity_warned.add(msg)
    _print(f"[{toast_key}] {msg}")
    notify(toast_key, toast_key, msg, hold=6.0)


def capture_target() -> str:
    if VARIABLES.display_override:
        return "display"
    if VARIABLES.linked_games:
        return "game"
    return "off"


def apply_capture_state():
    target = capture_target()
    set_scene_source_visible(_setting_str(PN.GAME_SOURCE_NAME), target == "game")
    set_scene_source_visible(_setting_str(PN.DESKTOP_SOURCE_NAME), target == "display")
    if VARIABLES.buffer_restart_depth > 0:
        return  # mid internal restart; it ends with the buffer running, next poll reconciles
    running = obs.obs_frontend_replay_buffer_active()
    if target == "off" and running:
        obs.obs_frontend_replay_buffer_stop()
        notify("buffer_off", "buffer")
    elif target != "off" and not running:
        obs.obs_frontend_replay_buffer_start()
        VARIABLES.buffer_start_at = time.time()
        notify("buffer_on", "buffer")


def _close_handles(*handles):
    for h in handles:
        with suppress(Exception):
            kernel32_win.CloseHandle(h)


def clear_capture_tracking():
    _close_handles(*(g["handle"] for g in VARIABLES.linked_games.values()))
    _close_handles(*VARIABLES.handled_games.values())
    VARIABLES.linked_games = {}
    VARIABLES.handled_games = {}
    VARIABLES.game_empty_since = 0.0
    VARIABLES.pending_hook_pid = 0


def reap_dead_games():
    had_linked = bool(VARIABLES.linked_games)
    for pid, info in list(VARIABLES.linked_games.items()):
        if process_exited(info["handle"]):
            _close_handles(VARIABLES.linked_games.pop(pid)["handle"])
    for pid, handle in list(VARIABLES.handled_games.items()):
        if process_exited(handle):
            _close_handles(VARIABLES.handled_games.pop(pid))
    # Start the grace clock only on the transition to "no linked game", not for an idle session.
    if had_linked and not VARIABLES.linked_games and not VARIABLES.game_empty_since:
        VARIABLES.game_empty_since = time.time()


def grace_seconds() -> int:
    v = obs.obs_data_get_int(VARIABLES.script_settings, PN.GAME_EXIT_GRACE_S)
    return v if v >= 0 else 30


def maybe_stop_after_grace():
    if (
        VARIABLES.game_empty_since
        and not VARIABLES.linked_games
        and not VARIABLES.pending_hook_pid  # a new game is hooking; don't stop then restart
        and not VARIABLES.display_override
        and obs.obs_frontend_replay_buffer_active()
        and time.time() - VARIABLES.game_empty_since >= grace_seconds()
    ):
        VARIABLES.game_empty_since = 0.0
        apply_capture_state()
        notify("game_off", "game")


def begin_game_hook(pid: int):
    VARIABLES.pending_hook_pid = pid
    set_scene_source_visible(_setting_str(PN.GAME_SOURCE_NAME), True)
    obs.timer_remove(game_hook_check)
    obs.timer_add(game_hook_check, 3000)


def game_hook_check():
    obs.timer_remove(game_hook_check)
    pid, VARIABLES.pending_hook_pid = VARIABLES.pending_hook_pid, 0
    if not pid or VARIABLES.display_override:
        # Override was engaged during the wait; drop this hook, it re-detects when override ends.
        apply_capture_state()
        return
    with suppress(Exception):
        finish_game_hook(pid)


def _exe_label(pid: int) -> str:
    with suppress(Exception):
        return get_executable_path(pid).stem
    return "game"


def finish_game_hook(pid: int):
    game_name = _setting_str(PN.GAME_SOURCE_NAME)
    if source_width(game_name) > 0:
        handle = open_tracking_handle(pid)
        if not handle:
            set_scene_source_visible(game_name, bool(VARIABLES.linked_games))
            return
        exe = None
        with suppress(Exception):
            exe = get_executable_path(pid)
        VARIABLES.linked_games[pid] = {"handle": handle, "exe": exe}
        VARIABLES.game_empty_since = 0.0
        apply_capture_state()
        notify("game_on", "game")
        return
    set_scene_source_visible(game_name, bool(VARIABLES.linked_games))
    handle = open_tracking_handle(pid)
    if handle:
        VARIABLES.handled_games[pid] = handle
    label = _exe_label(pid)
    if not game_name:
        warn_once("No game capture source set; game clipping can't confirm a hook.")
    elif not source_exists(game_name):
        warn_once(f"Game capture source '{game_name}' not found.")
    elif not source_in_current_scene(game_name):
        warn_once(f"Game capture source '{game_name}' is not in the current scene.")
    else:
        _print(
            f"[game] '{game_name}' hooked nothing for {label} - check its Mode is "
            "'Capture any fullscreen application'"
        )
    notify("game_failed", "game", f"Couldn't capture {label}")
    if obs.obs_data_get_bool(VARIABLES.script_settings, PN.AUTO_SWITCH_ON_HOOK_FAIL):
        enable_display_override(auto=True)


def detect_new_game():
    if VARIABLES.pending_hook_pid:
        return
    fullscreen, pid = is_foreground_window_fullscreen()
    if not (fullscreen and pid):
        return
    if pid in VARIABLES.linked_games or pid in VARIABLES.handled_games:
        return
    # Fail open (not-excepted) if the exe lookup fails - elevated / anti-cheat games are exactly the ones this matters for.
    with suppress(Exception):
        if is_exe_excepted(get_executable_path(pid)):
            return
    begin_game_hook(pid)


def reconcile_buffer():
    # Self-heal: if the buffer should be running (a game is linked, or override is on) but
    # isn't - OBS stopped it externally, an encoder hiccup, OBS's own buffer restarted -
    # bring it back. Never stops one; that stays the job of the explicit transitions.
    if (
        VARIABLES.buffer_restart_depth == 0
        and capture_target() != "off"
        and not obs.obs_frontend_replay_buffer_active()
        and time.time() - VARIABLES.buffer_start_at >= 8.0  # let a pending start settle first
    ):
        obs.obs_frontend_replay_buffer_start()
        VARIABLES.buffer_start_at = time.time()
        notify("buffer_on", "buffer")


def game_poll_callback():
    if VARIABLES.disk_warn_msg:
        msg, VARIABLES.disk_warn_msg = VARIABLES.disk_warn_msg, ""
        _print(f"[warning] {msg}")
        notify("warning", "warning", msg, hold=8.0)
    if VARIABLES.buffer_restart_depth > 0:
        return
    with suppress(Exception):
        reap_dead_games()
        if VARIABLES.display_override:
            check_display_override_timeout()
        elif obs.obs_data_get_bool(VARIABLES.script_settings, PN.AUTO_GAME_CLIPPING):
            detect_new_game()
        maybe_stop_after_grace()
        reconcile_buffer()


def setup_game_poll_timer():
    obs.timer_remove(game_poll_callback)
    interval = (
        obs.obs_data_get_int(VARIABLES.script_settings, PN.GAME_POLL_MS) or 3000
    )
    obs.timer_add(game_poll_callback, interval)


# -------------------- sanity checks + disk warning --------------------
def run_sanity_checks():
    s = VARIABLES.script_settings
    game = obs.obs_data_get_string(s, PN.GAME_SOURCE_NAME)
    desktop = obs.obs_data_get_string(s, PN.DESKTOP_SOURCE_NAME)
    mic = obs.obs_data_get_string(s, PN.MIC_SOURCE_NAME)
    auto = obs.obs_data_get_bool(s, PN.AUTO_GAME_CLIPPING)

    if auto and not game:
        warn_once("Auto game clipping is on but no game capture source is set.", "info")
    for label, name in (("Game capture", game), ("Display capture", desktop)):
        if not name:
            continue
        if not source_exists(name):
            warn_once(f"{label} source '{name}' not found.")
        elif not source_in_current_scene(name):
            warn_once(f"{label} source '{name}' is not in the current scene.")
    if mic and not source_exists(mic):
        warn_once(f"Mic source '{mic}' not found.")


def _folder_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            with suppress(OSError):
                total += os.path.getsize(os.path.join(dirpath, f))
    return total


def _disk_check_worker(base: Path, limit_gb: int):
    # Runs off-thread: os.* only, no obs.* calls. The toast is surfaced by game_poll_callback.
    with suppress(Exception):
        if not base.is_dir():
            return
        used_gb = _folder_bytes(base) / 1024**3
        if used_gb >= limit_gb:
            if not VARIABLES.disk_over_limit:
                VARIABLES.disk_over_limit = True
                VARIABLES.disk_warn_msg = (
                    f"Clips folder is {used_gb:.0f} GB - over your {limit_gb} GB limit"
                )
        else:
            VARIABLES.disk_over_limit = False


def disk_check_tick():
    # Resolve path + limit on the OBS thread; the walk (can be slow) runs off-thread.
    if time.time() - VARIABLES.last_disk_check < 60:
        return
    limit_gb = obs.obs_data_get_int(VARIABLES.script_settings, PN.CLIP_FOLDER_WARN_GB)
    if limit_gb <= 0:
        return
    VARIABLES.last_disk_check = time.time()
    Thread(
        target=_disk_check_worker, args=(get_base_path(), limit_gb), daemon=True
    ).start()


# -------------------- capture actions (hotkeys) --------------------
def game_clipping_on():
    if VARIABLES.display_override:
        return  # display override outranks game clipping; turn it off first
    fg = get_active_window_pid()
    if fg in VARIABLES.handled_games:
        _close_handles(VARIABLES.handled_games.pop(fg))
    if fg in VARIABLES.linked_games or VARIABLES.pending_hook_pid:
        return
    fullscreen, pid = is_foreground_window_fullscreen()
    if not (fullscreen and pid):
        notify("game_failed", "game", "No fullscreen game found")
        return
    if pid in VARIABLES.linked_games:
        return  # already clipping it (foreground raced between the two GetForegroundWindow calls)
    if pid in VARIABLES.handled_games:
        _close_handles(VARIABLES.handled_games.pop(pid))
    begin_game_hook(pid)


def game_clipping_off():
    pending = VARIABLES.pending_hook_pid
    if pending:
        VARIABLES.pending_hook_pid = 0
        obs.timer_remove(game_hook_check)
        h = open_tracking_handle(pending)
        if h:
            VARIABLES.handled_games[pending] = h
    fg = get_active_window_pid()
    targets = [fg] if fg in VARIABLES.linked_games else list(VARIABLES.linked_games)
    if not targets and not pending:
        return
    for pid in targets:
        VARIABLES.handled_games[pid] = VARIABLES.linked_games.pop(pid)["handle"]
    VARIABLES.game_empty_since = 0.0  # deliberate stop, no grace
    apply_capture_state()
    notify("game_off", "game")


def game_clipping_toggle():
    if VARIABLES.linked_games or VARIABLES.pending_hook_pid:
        game_clipping_off()
    else:
        game_clipping_on()


def _reset_override_deadline():
    mins = obs.obs_data_get_int(
        VARIABLES.script_settings, PN.DISPLAY_OVERRIDE_AUTOOFF_MIN
    )
    VARIABLES.display_override_deadline = time.time() + mins * 60 if mins > 0 else 0.0


def enable_display_override(auto: bool = False):
    if VARIABLES.display_override:
        _reset_override_deadline()
        return
    VARIABLES.display_override = True
    _reset_override_deadline()
    apply_capture_state()
    notify("desktop_on", "desktop", "Switched to desktop capture" if auto else "")


def disable_display_override(timed_out: bool = False):
    if not VARIABLES.display_override:
        return
    VARIABLES.display_override = False
    VARIABLES.display_override_deadline = 0.0
    apply_capture_state()
    notify("desktop_off", "desktop", "Desktop capture timed out" if timed_out else "")
    if VARIABLES.linked_games:
        notify("game_on", "game")  # a game was still linked; game clipping resumes


def display_override_toggle():
    if VARIABLES.display_override:
        disable_display_override()
    else:
        enable_display_override()


def check_display_override_timeout():
    d = VARIABLES.display_override_deadline
    if d and time.time() >= d:
        disable_display_override(timed_out=True)


# -------------------- clip naming and moving --------------------
def _foregrounded_linked_exe() -> Path | None:
    if not VARIABLES.linked_games:
        return None
    fg = VARIABLES.linked_games.get(get_active_window_pid())
    info = fg or next(iter(VARIABLES.linked_games.values()))
    exe = info.get("exe")
    return exe if exe and str(exe) else None


def gen_clip_base_name() -> str:
    # Prefer the linked game's exe; else the most-recorded buffer-window process, not the one focused right now (alt-tab at save time would misname it).
    exe_path = _foregrounded_linked_exe()
    if exe_path is None and VARIABLES.clip_exe_history:
        exe_path = Counter(VARIABLES.clip_exe_history).most_common(1)[0][0]
    if exe_path is None:
        with suppress(Exception):
            exe_path = get_executable_path(get_active_window_pid())
    if exe_path is None:
        return "Unknown"  # elevated / anti-cheat process we could never resolve
    return get_alias(exe_path) or exe_path.stem


def gen_filename(base_name: str, template: str) -> str:
    filename = template.replace("%NAME", base_name)
    filename = datetime.now().strftime(filename)
    if any(c in filename for c in CONSTANTS.FILENAME_PROHIBITED_CHARS):
        raise ValueError(
            f"Generated filename contains prohibited characters: {filename}"
        )
    return filename


def ensure_unique_filename(file_path: Path) -> Path:
    parent, stem, suffix = file_path.parent, file_path.stem, file_path.suffix
    counter = 1
    while file_path.exists():
        file_path = parent / f"{stem} ({counter}){suffix}"
        counter += 1
    return file_path


def move_clip_file() -> Path:
    old_path = get_last_replay_file_name()
    if VARIABLES.display_override and not VARIABLES.linked_games:
        clip_name = _setting_str(PN.DISPLAY_CLIP_FOLDER) or "Desktop"
    else:
        clip_name = gen_clip_base_name()

    template = obs.obs_data_get_string(VARIABLES.script_settings, PN.FILENAME_TEMPLATE)
    filename = gen_filename(clip_name, template) + f".{old_path.split('.')[-1]}"
    clip_name_folder = clip_name
    if obs.obs_data_get_bool(VARIABLES.script_settings, PN.REPLACE_SPACES):
        filename = filename.replace(" ", "_")
        clip_name_folder = clip_name_folder.replace(" ", "_")
    if clip_name_folder in (".", "..") or any(
        c in clip_name_folder for c in CONSTANTS.FILENAME_PROHIBITED_CHARS
    ):
        raise ValueError(f"Unsafe clip folder name: {clip_name_folder!r}")

    folder = get_base_path() / clip_name_folder
    os.makedirs(str(folder), exist_ok=True)
    new_path = ensure_unique_filename(folder / filename)

    shutil.move(old_path, str(new_path))
    _print(f"Clip moved to {new_path}")
    return new_path


# -------------------- notifications (never allowed to break the save/toggle flow) --------------------
def notify_clip(success: bool):
    with suppress(Exception):
        toast_key = "replay_saved" if success else "replay_failed"
        if obs.obs_data_get_bool(VARIABLES.script_settings, PN.NOTIFY_CLIP_SOUND):
            play_notification_sound(toast_key)
        if obs.obs_data_get_bool(VARIABLES.script_settings, PN.NOTIFY_CLIP_POPUP):
            show_popup("replay", toast_key)


def notify(toast_key: str, kind: str, text_override: str = "", hold: float = 0.0):
    with suppress(Exception):
        if obs.obs_data_get_bool(VARIABLES.script_settings, PN.NOTIFY_TOGGLE_SOUND):
            play_notification_sound(toast_key)
        if obs.obs_data_get_bool(VARIABLES.script_settings, PN.NOTIFY_TOGGLE_POPUP):
            show_popup(kind, toast_key, text_override, hold)


# -------------------- replay buffer save flow --------------------
def restart_replay_buffering():
    _print("Restarting replay buffering...")
    with VARIABLES.buffer_restart_lock:
        VARIABLES.buffer_restart_depth += 1
    try:
        replay_output = obs.obs_frontend_get_replay_buffer_output()
        obs.obs_frontend_replay_buffer_stop()
        if replay_output is not None:
            # Bounded so a wedged output can't pin buffer_restart_depth above zero forever.
            deadline = time.time() + 10
            while (
                not obs.obs_output_can_begin_data_capture(replay_output, 0)
                and time.time() < deadline
            ):
                time.sleep(0.1)
            obs.obs_output_release(replay_output)
        obs.obs_frontend_replay_buffer_start()
    finally:
        with VARIABLES.buffer_restart_lock:
            VARIABLES.buffer_restart_depth -= 1
    _print("Replay buffering restarted.")


def restart_replay_buffering_callback():
    obs.timer_remove(restart_replay_buffering_callback)
    replay_length = get_replay_buffer_max_time()
    idle = get_time_since_last_input()
    if idle < replay_length:
        obs.timer_add(
            restart_replay_buffering_callback, max((replay_length - idle) * 1000, 2000)
        )
        return
    Thread(target=restart_replay_buffering, daemon=True).start()


def append_clip_exe_history():
    with suppress(Exception):
        VARIABLES.clip_exe_history.appendleft(
            get_executable_path(get_active_window_pid())
        )


def on_buffer_save_callback(event):
    if event != obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_SAVED:
        return
    # game_empty_since keeps clips saving through the exit-grace window, where capture_target() is already "off" but the user still wants the last play.
    if capture_target() == "off" and not VARIABLES.game_empty_since:
        _print("Replay saved while clipping is disabled; leaving the clip untouched.")
        if obs.obs_data_get_bool(VARIABLES.script_settings, PN.NOTIFY_CLIP_DISABLED):
            show_popup("info", "info", "Clipping is disabled")
        return
    _print("Replay buffer saved, moving clip...")
    try:
        move_clip_file()
        if obs.obs_data_get_bool(VARIABLES.script_settings, PN.RESTART_BUFFER):
            Thread(target=restart_replay_buffering, daemon=True).start()
        if VARIABLES.display_override:
            _reset_override_deadline()
        notify_clip(True)
    except Exception:
        _print("Failed to move clip file.")
        _print(traceback.format_exc())
        notify_clip(False)


def on_buffer_started_callback(event):
    if event != obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTED:
        return
    VARIABLES.clip_exe_history = deque([], maxlen=get_replay_buffer_max_time())
    obs.timer_remove(append_clip_exe_history)  # this event also fires from the restart cycle
    obs.timer_remove(restart_replay_buffering_callback)
    obs.timer_add(append_clip_exe_history, 1000)
    if loop_time := obs.obs_data_get_int(
        VARIABLES.script_settings, PN.RESTART_BUFFER_LOOP
    ):
        obs.timer_add(restart_replay_buffering_callback, loop_time * 1000)


def on_buffer_stopped_callback(event):
    if event != obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_STOPPED:
        return
    obs.timer_remove(append_clip_exe_history)
    obs.timer_remove(restart_replay_buffering_callback)
    if VARIABLES.clip_exe_history is not None:
        VARIABLES.clip_exe_history.clear()


# -------------------- reactive mic notifications --------------------
# React to OBS's own mute signal so the toast fires regardless of trigger - our hotkey, OBS's native per-source hotkey, a Stream Deck, the Sources dock.
def on_mic_mute_signal(calldata):
    muted = obs.calldata_bool(calldata, "muted")
    notify("mic_off" if muted else "mic_on", "mic")


def connect_mic_signal():
    name = obs.obs_data_get_string(VARIABLES.script_settings, PN.MIC_SOURCE_NAME)
    source = obs.obs_get_source_by_name(name) if name else None
    if name and not source:
        # Don't tear down a working connection just because the source is briefly unresolvable.
        _print(f"[mic] source '{name}' not found; keeping any existing connection")
        return
    if VARIABLES.mic_signal_handler:
        obs.signal_handler_disconnect(
            VARIABLES.mic_signal_handler, "mute", on_mic_mute_signal
        )
        VARIABLES.mic_signal_handler = None
    if not source:
        _print("[mic] no mic source name set")
        return
    sh = obs.obs_source_get_signal_handler(source)
    obs.signal_handler_connect(sh, "mute", on_mic_mute_signal)
    VARIABLES.mic_signal_handler = sh
    obs.obs_source_release(source)
    _print(f"[mic] mute signal connected to '{name}'")


def on_frontend_ready_callback(event):
    # script_load can run before the scene collection (and the sources) exist.
    if event == obs.OBS_FRONTEND_EVENT_FINISHED_LOADING:
        connect_mic_signal()
        run_sanity_checks()
        disk_check_tick()


# -------------------- hotkeys --------------------
def toggle_mic_mute():
    name = obs.obs_data_get_string(VARIABLES.script_settings, PN.MIC_SOURCE_NAME)
    if not name:
        _print("Mic source name is not set.")
        return
    source = obs.obs_get_source_by_name(name)
    if not source:
        _print(f"Mic source '{name}' not found.")
        return
    obs.obs_source_set_muted(source, not obs.obs_source_muted(source))
    obs.obs_source_release(source)


def toggle_mic_monitoring():
    name = obs.obs_data_get_string(VARIABLES.script_settings, PN.MIC_SOURCE_NAME)
    if not name:
        _print("Mic source name is not set.")
        return
    source = obs.obs_get_source_by_name(name)
    if not source:
        _print(f"Mic source '{name}' not found.")
        return
    currently_on = (
        obs.obs_source_get_monitoring_type(source) != obs.OBS_MONITORING_TYPE_NONE
    )
    new_type = (
        obs.OBS_MONITORING_TYPE_NONE
        if currently_on
        else obs.OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT
    )
    obs.obs_source_set_monitoring_type(source, new_type)
    obs.obs_source_release(source)
    # No OBS signal fires for a monitoring-type change (unlike mute), so this notifies directly.
    notify("monitor_off" if currently_on else "monitor_on", "monitor")


def _hk(action):
    return lambda pressed: action() if pressed else None


def load_hotkeys():
    keys = (
        (PN.HK_GAME_ON, "[Clip Manager] Game clipping on", _hk(game_clipping_on)),
        (PN.HK_GAME_OFF, "[Clip Manager] Game clipping off", _hk(game_clipping_off)),
        (
            PN.HK_GAME_TOGGLE,
            "[Clip Manager] Game clipping toggle",
            _hk(game_clipping_toggle),
        ),
        (
            PN.HK_DISPLAY_ON,
            "[Clip Manager] Display override on",
            _hk(enable_display_override),
        ),
        (
            PN.HK_DISPLAY_OFF,
            "[Clip Manager] Display override off",
            _hk(disable_display_override),
        ),
        (
            PN.HK_DISPLAY_TOGGLE,
            "[Clip Manager] Display override toggle",
            _hk(display_override_toggle),
        ),
        (PN.HK_TOGGLE_MIC, "[Clip Manager] Toggle mic mute", _hk(toggle_mic_mute)),
        (
            PN.HK_TOGGLE_MONITOR,
            "[Clip Manager] Toggle mic monitoring",
            _hk(toggle_mic_monitoring),
        ),
    )
    for name, desc, callback in keys:
        hotkey_id = obs.obs_hotkey_register_frontend(name, desc, callback)
        VARIABLES.hotkey_ids[name] = hotkey_id
        data = obs.obs_data_get_array(VARIABLES.script_settings, name)
        obs.obs_hotkey_load(hotkey_id, data)
        obs.obs_data_array_release(data)


# -------------------- properties UI --------------------
def update_aliases_callback(p, prop, data):
    load_aliases(json.loads(obs.obs_data_get_json(data)))
    return True


def update_auto_buffer_exceptions_callback(p, prop, data):
    load_auto_buffer_exceptions(data)
    return True


def update_game_poll_timer_callback(p, prop, data):
    setup_game_poll_timer()
    return True


def update_mic_source_callback(p, prop, data):
    connect_mic_signal()
    return True


def update_restart_loop_callback(p, prop, data):
    obs.timer_remove(restart_replay_buffering_callback)
    loop_time = obs.obs_data_get_int(data, PN.RESTART_BUFFER_LOOP)
    if loop_time and obs.obs_frontend_replay_buffer_active():
        obs.timer_add(restart_replay_buffering_callback, loop_time * 1000)
    return True


def add_source_dropdown(g, prop_name, label):
    prop = obs.obs_properties_add_list(
        g, prop_name, label, obs.OBS_COMBO_TYPE_EDITABLE, obs.OBS_COMBO_FORMAT_STRING
    )
    sources = obs.obs_enum_sources()
    if sources:
        for source in sources:
            name = obs.obs_source_get_name(source)
            obs.obs_property_list_add_string(prop, name, name)
        obs.source_list_release(sources)
    return prop


def setup_paths_group(g):
    obs.obs_properties_add_path(
        g, PN.BASE_PATH, "Clips folder", obs.OBS_PATH_DIRECTORY, None, str(get_base_path())
    )
    obs.obs_properties_add_text(
        g, PN.FILENAME_TEMPLATE, "Filename template", obs.OBS_TEXT_DEFAULT
    )
    obs.obs_properties_add_text(
        g, PN.DISPLAY_CLIP_FOLDER, "Desktop clips subfolder", obs.OBS_TEXT_DEFAULT
    )
    obs.obs_properties_add_bool(g, PN.REPLACE_SPACES, "Spaces to underscores")
    obs.obs_properties_add_int(
        g, PN.CLIP_FOLDER_WARN_GB, "Warn over folder size (GB, 0 = off)", 0, 10000, 10
    )
    obs.obs_properties_add_text(
        g, "paths_info", "Template takes %NAME and strftime codes.", obs.OBS_TEXT_INFO
    )


def setup_sources_group(g):
    add_source_dropdown(g, PN.GAME_SOURCE_NAME, "Game capture source")
    add_source_dropdown(g, PN.DESKTOP_SOURCE_NAME, "Display capture source")
    mic_prop = add_source_dropdown(g, PN.MIC_SOURCE_NAME, "Mic source")
    obs.obs_properties_add_text(
        g, "sources_info", 'Bind keys in Settings > Hotkeys ("Clip Manager").', obs.OBS_TEXT_INFO
    )
    # Mic signal binds to one named source, so a name change needs a reconnect.
    obs.obs_property_set_modified_callback(mic_prop, update_mic_source_callback)


def setup_notifications_group(g):
    obs.obs_properties_add_bool(g, PN.NOTIFY_CLIP_SOUND, "Sound on clip saved / failed")
    obs.obs_properties_add_bool(g, PN.NOTIFY_CLIP_POPUP, "Popup on clip saved / failed")
    obs.obs_properties_add_bool(g, PN.NOTIFY_TOGGLE_SOUND, "Sound on capture toggles")
    obs.obs_properties_add_bool(g, PN.NOTIFY_TOGGLE_POPUP, "Popup on capture toggles")
    obs.obs_properties_add_bool(
        g, PN.NOTIFY_CLIP_DISABLED, "Popup when saving with clipping off"
    )


def setup_popup_group(g):
    vpos = obs.obs_properties_add_list(
        g,
        PN.POPUP_VPOS,
        "Position (vertical)",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(vpos, "Top", "top")
    obs.obs_property_list_add_string(vpos, "Bottom", "bottom")

    hpos = obs.obs_properties_add_list(
        g,
        PN.POPUP_HPOS,
        "Position (horizontal)",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(hpos, "Left", "left")
    obs.obs_property_list_add_string(hpos, "Center", "center")
    obs.obs_property_list_add_string(hpos, "Right", "right")

    obs.obs_properties_add_float_slider(g, PN.POPUP_SCALE, "Scale", 0.5, 2.0, 0.05)
    obs.obs_properties_add_int(
        g, PN.POPUP_HEIGHT_OVERRIDE, "Height (px, 0 = auto)", 0, 800, 5
    )
    obs.obs_properties_add_int(g, PN.POPUP_EDGE_MARGIN, "Edge margin (px)", 0, 300, 1)
    obs.obs_properties_add_int(g, PN.POPUP_STACK_GAP, "Stack gap (px)", 0, 200, 1)
    obs.obs_properties_add_float_slider(
        g, PN.POPUP_HOLD_SECONDS, "Time on screen (s)", 0.5, 10.0, 0.1
    )
    obs.obs_properties_add_int(g, PN.POPUP_SLIDE_IN_MS, "Slide-in (ms)", 0, 1000, 10)
    obs.obs_properties_add_int(g, PN.POPUP_SLIDE_OUT_MS, "Slide-out (ms)", 0, 1000, 10)
    obs.obs_properties_add_text(
        g,
        "popup_size_info",
        "Width auto-fits the text; Scale multiplies height and text.",
        obs.OBS_TEXT_INFO,
    )


def setup_sounds_group(g):
    obs.obs_properties_add_float_slider(
        g, PN.VOLUME_OFFSET_DB, "Volume offset (dB)", -40.0, 12.0, 0.5
    )
    for toast_key, spec in TOASTS.items():
        default_path = get_script_dir() / "sounds" / SOUND_DEFAULT_FILES[toast_key]
        obs.obs_properties_add_path(
            g,
            sound_setting_name(toast_key),
            spec["text"],
            obs.OBS_PATH_FILE,
            "WAV (*.wav)",
            str(default_path),
        )


def setup_aliases_group(g):
    obs.obs_properties_add_text(
        g, "aliases_info", "path > Display Name, one per line.", obs.OBS_TEXT_INFO
    )
    aliases_list = obs.obs_properties_add_editable_list(
        g, PN.ALIASES_LIST, "Aliases", obs.OBS_EDITABLE_LIST_TYPE_STRINGS, None, None
    )
    obs.obs_property_set_modified_callback(aliases_list, update_aliases_callback)


def setup_capture_group(g):
    obs.obs_properties_add_bool(g, PN.AUTO_GAME_CLIPPING, "Auto game clipping")
    poll_prop = obs.obs_properties_add_int(
        g, PN.GAME_POLL_MS, "Detection interval (ms)", 500, 10000, 100
    )
    obs.obs_properties_add_int(
        g, PN.GAME_EXIT_GRACE_S, "Grace after game exits (s)", 0, 600, 5
    )
    obs.obs_properties_add_bool(
        g, PN.AUTO_SWITCH_ON_HOOK_FAIL, "Fall back to desktop on fail"
    )
    obs.obs_properties_add_int(
        g, PN.DISPLAY_OVERRIDE_AUTOOFF_MIN, "Desktop auto-off (min)", 0, 1440, 5
    )
    obs.obs_properties_add_text(
        g,
        "capture_info",
        "Detects a fullscreen app on the primary monitor. Desktop auto-off 0 = never.",
        obs.OBS_TEXT_INFO,
    )
    obs.obs_properties_add_text(
        g,
        "exceptions_info",
        "Exceptions: exe name or folder path, one per line.",
        obs.OBS_TEXT_INFO,
    )
    exceptions_list = obs.obs_properties_add_editable_list(
        g,
        PN.AUTO_BUFFER_EXCEPTIONS,
        "Exceptions",
        obs.OBS_EDITABLE_LIST_TYPE_STRINGS,
        None,
        None,
    )
    obs.obs_property_set_modified_callback(poll_prop, update_game_poll_timer_callback)
    obs.obs_property_set_modified_callback(
        exceptions_list, update_auto_buffer_exceptions_callback
    )


def setup_replay_buffer_group(g):
    obs.obs_properties_add_bool(g, PN.RESTART_BUFFER, "Restart after every save")
    loop_prop = obs.obs_properties_add_int(
        g, PN.RESTART_BUFFER_LOOP, "Periodic restart (s, 0 = off)", 0, 7200, 10
    )
    obs.obs_properties_add_text(
        g,
        "restart_info",
        "Periodic restart works around an OBS bug in long sessions.",
        obs.OBS_TEXT_INFO,
    )
    obs.obs_property_set_modified_callback(loop_prop, update_restart_loop_callback)


def script_properties():
    p = obs.obs_properties_create()
    obs.obs_properties_add_text(
        p, "version_info", f"Clip Manager v{SCRIPT_VERSION}", obs.OBS_TEXT_INFO
    )
    groups = {
        PN.GR_SOURCES: ("Sources", setup_sources_group),
        PN.GR_CAPTURE: ("Capture", setup_capture_group),
        PN.GR_PATHS: ("Clip files", setup_paths_group),
        PN.GR_AUTO_BUFFER: ("Replay buffer", setup_replay_buffer_group),
        PN.GR_NOTIFICATIONS: ("Notifications", setup_notifications_group),
        PN.GR_POPUP: ("Popup", setup_popup_group),
        PN.GR_SOUNDS: ("Sounds", setup_sounds_group),
        PN.GR_ALIASES: ("Aliases", setup_aliases_group),
    }
    for key, (label, setup_fn) in groups.items():
        sub = obs.obs_properties_create()
        obs.obs_properties_add_group(p, key, label, obs.OBS_GROUP_NORMAL, sub)
        setup_fn(sub)
    return p


def script_defaults(s):
    obs.obs_data_set_default_string(s, PN.BASE_PATH, str(get_base_path(s)))
    obs.obs_data_set_default_string(
        s, PN.FILENAME_TEMPLATE, CONSTANTS.DEFAULT_FILENAME_FORMAT
    )
    obs.obs_data_set_default_bool(s, PN.REPLACE_SPACES, True)
    obs.obs_data_set_default_string(s, PN.DISPLAY_CLIP_FOLDER, "Desktop")
    obs.obs_data_set_default_int(s, PN.CLIP_FOLDER_WARN_GB, 100)
    obs.obs_data_set_default_bool(s, PN.NOTIFY_CLIP_SOUND, True)
    obs.obs_data_set_default_bool(s, PN.NOTIFY_CLIP_POPUP, True)
    obs.obs_data_set_default_bool(s, PN.NOTIFY_TOGGLE_SOUND, True)
    obs.obs_data_set_default_bool(s, PN.NOTIFY_TOGGLE_POPUP, True)
    obs.obs_data_set_default_bool(s, PN.NOTIFY_CLIP_DISABLED, False)
    obs.obs_data_set_default_string(s, PN.POPUP_VPOS, "top")
    obs.obs_data_set_default_string(s, PN.POPUP_HPOS, "right")
    obs.obs_data_set_default_double(s, PN.POPUP_SCALE, 1.0)
    obs.obs_data_set_default_int(s, PN.POPUP_HEIGHT_OVERRIDE, 0)
    obs.obs_data_set_default_int(s, PN.POPUP_EDGE_MARGIN, 16)
    obs.obs_data_set_default_int(s, PN.POPUP_STACK_GAP, 8)
    obs.obs_data_set_default_double(s, PN.POPUP_HOLD_SECONDS, 2.0)
    obs.obs_data_set_default_int(s, PN.POPUP_SLIDE_IN_MS, 150)
    obs.obs_data_set_default_int(s, PN.POPUP_SLIDE_OUT_MS, 150)
    obs.obs_data_set_default_double(s, PN.VOLUME_OFFSET_DB, 0.0)
    for toast_key in TOASTS:
        default_path = get_script_dir() / "sounds" / SOUND_DEFAULT_FILES[toast_key]
        obs.obs_data_set_default_string(
            s, sound_setting_name(toast_key), str(default_path)
        )
    obs.obs_data_set_default_bool(s, PN.RESTART_BUFFER, False)
    obs.obs_data_set_default_int(s, PN.RESTART_BUFFER_LOOP, 3600)
    obs.obs_data_set_default_bool(s, PN.AUTO_GAME_CLIPPING, True)
    obs.obs_data_set_default_int(s, PN.GAME_POLL_MS, 3000)
    obs.obs_data_set_default_int(s, PN.GAME_EXIT_GRACE_S, 30)
    obs.obs_data_set_default_bool(s, PN.AUTO_SWITCH_ON_HOOK_FAIL, True)
    obs.obs_data_set_default_int(s, PN.DISPLAY_OVERRIDE_AUTOOFF_MIN, 60)
    default_exceptions = obs.obs_data_array_create()
    # One bare exe name per browser covers every channel (stable/beta/dev/canary/nightly) - on Windows they differ by install folder, not binary name - via the bare-name match in is_exe_excepted.
    browser_exes = (
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        "opera.exe",
        "vivaldi.exe",
    )
    non_game_fullscreen_exes = (
        "explorer.exe",
        "LockApp.exe",
        "vlc.exe",
        "mpv.exe",
        "mpc-hc64.exe",
        "mpc-hc.exe",
        "PotPlayerMini64.exe",
    )
    for exe_name in browser_exes + non_game_fullscreen_exes:
        item = obs.obs_data_create_from_json(
            json.dumps({"hidden": False, "selected": False, "value": exe_name})
        )
        obs.obs_data_array_push_back(default_exceptions, item)
        obs.obs_data_release(item)
    obs.obs_data_set_default_array(s, PN.AUTO_BUFFER_EXCEPTIONS, default_exceptions)
    obs.obs_data_array_release(default_exceptions)


def script_update(settings):
    VARIABLES.script_settings = settings


def script_save(settings):
    for name, hotkey_id in VARIABLES.hotkey_ids.items():
        arr = obs.obs_hotkey_save(hotkey_id)
        obs.obs_data_set_array(settings, name, arr)
        obs.obs_data_array_release(arr)


def script_load(settings):
    VARIABLES.script_settings = settings
    load_aliases(json.loads(obs.obs_data_get_json(settings)))
    load_auto_buffer_exceptions(settings)

    # Stale HWNDs here belong to a previous OBS session and Windows can recycle HWND values, so a leftover entry could make a fresh toast overwrite an unrelated window - clear it on every load.
    with suppress(Exception):
        (get_script_dir() / "toast_state.json").unlink(missing_ok=True)

    obs.obs_frontend_add_event_callback(on_buffer_save_callback)
    obs.obs_frontend_add_event_callback(on_buffer_started_callback)
    obs.obs_frontend_add_event_callback(on_buffer_stopped_callback)
    obs.obs_frontend_add_event_callback(on_frontend_ready_callback)
    load_hotkeys()

    connect_mic_signal()
    setup_game_poll_timer()
    obs.timer_add(disk_check_tick, 3_600_000)
    disk_check_tick()

    if obs.obs_frontend_replay_buffer_active():
        on_buffer_started_callback(obs.OBS_FRONTEND_EVENT_REPLAY_BUFFER_STARTED)


def script_unload():
    obs.timer_remove(append_clip_exe_history)
    obs.timer_remove(restart_replay_buffering_callback)
    obs.timer_remove(game_poll_callback)
    obs.timer_remove(game_hook_check)
    obs.timer_remove(disk_check_tick)
    if VARIABLES.mic_signal_handler:
        obs.signal_handler_disconnect(
            VARIABLES.mic_signal_handler, "mute", on_mic_mute_signal
        )
    # Without these, a reload leaves the old callbacks/hotkeys registered alongside the new ones - every replay save / scene change / bound key would then fire twice.
    obs.obs_frontend_remove_event_callback(on_buffer_save_callback)
    obs.obs_frontend_remove_event_callback(on_buffer_started_callback)
    obs.obs_frontend_remove_event_callback(on_buffer_stopped_callback)
    obs.obs_frontend_remove_event_callback(on_frontend_ready_callback)
    for hotkey_id in VARIABLES.hotkey_ids.values():
        obs.obs_hotkey_unregister(hotkey_id)
    VARIABLES.hotkey_ids = {}
    clear_capture_tracking()


def script_description():
    return f"""<div style="font-size: 20pt;">Clip Manager <span style="font-size: 11pt; opacity: 0.7;">v{SCRIPT_VERSION}</span></div>
<div style="font-size: 10pt;">
Saves replay buffer clips into per-game folders (named from the captured game, with alias
overrides), with popup + sound notifications. Use OBS's own Save Replay hotkey to save
clips - the naming/moving logic runs off OBS's save event, not a hotkey of ours.
<br/><br/>
<b>Game clipping</b> (on by default) starts the replay buffer and shows the game capture
source when a fullscreen app is detected on the primary monitor, and stops when it exits.
An exceptions list keeps browsers etc. from counting. If OBS can't hook the game it says
so and can fall back to desktop capture.
<br/><br/>
<b>Display override</b> is a manual hotkey: shows the display capture source and buffer
regardless of game detection, auto-off after a configurable time. Its clips go to their
own subfolder.
<br/><br/>
Bind "Game clipping on/off/toggle", "Display override on/off/toggle", "Toggle mic mute",
and "Toggle mic monitoring" under Settings &gt; Hotkeys (search "Clip Manager").
<br/><br/>
Sound files and a volume offset (dB) are configurable per notification under Sounds.
Source name fields are editable dropdowns. Popups require Pillow in OBS's configured
Python: pip install pillow
</div>"""
