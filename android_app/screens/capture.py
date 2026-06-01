"""Pantalla de captura de fotogramas + datos IMU."""

import io
import json
import math
import threading
import time
from typing import List

from kivy.clock import Clock
from kivy.uix.screenmanager import Screen
from kivy.uix.camera import Camera
from kivy.graphics.texture import Texture
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDRaisedButton, MDFlatButton
from kivymd.uix.label import MDLabel
from kivymd.uix.progressbar import MDProgressBar
from kivymd.uix.card import MDCard

# Plyer sensors (work on Android; return zeros on desktop)
try:
    from plyer import accelerometer, gyroscope
    _PLYER = True
except Exception:
    _PLYER = False

MIN_FRAMES      = 30
TARGET_FRAMES   = 60
CAPTURE_INTERVAL = 0.3   # seconds between auto-captures
GYRO_MAX_RAD    = 0.6    # rad/s — above this → frame discarded (too blurry)


class CaptureScreen(Screen):
    server_ip: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._frames: List[dict] = []   # {frame_id, image_bytes, imu, camera}
        self._capturing  = False
        self._cap_thread = None
        self._camera_w   = 1280
        self._camera_h   = 720
        self._build_ui()

    def on_enter(self):
        self._frames.clear()
        self._update_counter()
        if _PLYER:
            try:
                accelerometer.enable()
                gyroscope.enable()
            except Exception:
                pass

    def on_leave(self):
        self._stop_capture()
        if _PLYER:
            try:
                accelerometer.disable()
                gyroscope.disable()
            except Exception:
                pass

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = MDBoxLayout(orientation="vertical")

        # Camera preview
        self._camera = Camera(
            resolution=(self._camera_w, self._camera_h),
            play=True,
            size_hint=(1, 0.65),
        )
        root.add_widget(self._camera)

        # Overlay indicators
        overlay = MDBoxLayout(
            orientation="horizontal",
            size_hint=(1, None),
            height=40,
            padding=(12, 4),
            spacing=12,
        )
        self._gyro_lbl = MDLabel(
            text="● Estable",
            halign="left",
            font_style="Caption",
            size_hint_x=0.4,
        )
        overlay.add_widget(self._gyro_lbl)
        self._counter_lbl = MDLabel(
            text="0 / %d fotos" % TARGET_FRAMES,
            halign="right",
            font_style="Caption",
            size_hint_x=0.6,
        )
        overlay.add_widget(self._counter_lbl)
        root.add_widget(overlay)

        # Progress bar
        self._progress = MDProgressBar(
            value=0, max=TARGET_FRAMES,
            size_hint=(1, None), height=6,
        )
        root.add_widget(self._progress)

        # Controls
        ctrl = MDBoxLayout(
            orientation="horizontal",
            size_hint=(1, None),
            height=72,
            padding=(16, 8),
            spacing=16,
        )
        self._btn_back = MDFlatButton(
            text="← Volver",
            on_release=lambda _: setattr(self.manager, "current", "connect"),
        )
        ctrl.add_widget(self._btn_back)

        self._btn_capture = MDRaisedButton(
            text="● INICIAR",
            on_release=self._toggle_capture,
            size_hint_x=0.5,
        )
        ctrl.add_widget(self._btn_capture)

        self._btn_send = MDRaisedButton(
            text="ENVIAR →",
            on_release=self._on_send,
            disabled=True,
        )
        ctrl.add_widget(self._btn_send)
        root.add_widget(ctrl)

        self._hint_lbl = MDLabel(
            text="Rodeá el horno lentamente para capturar todos los ángulos",
            halign="center",
            font_style="Caption",
            size_hint=(1, None),
            height=28,
        )
        root.add_widget(self._hint_lbl)

        self.add_widget(root)

    # ── capture loop ──────────────────────────────────────────────────────────

    def _toggle_capture(self, *_):
        if not self._capturing:
            self._start_capture()
        else:
            self._stop_capture()

    def _start_capture(self):
        self._capturing = True
        self._btn_capture.text = "■ DETENER"
        self._hint_lbl.text = "Capturando… rodeá el horno despacio"
        self._cap_thread = threading.Thread(
            target=self._capture_loop, daemon=True)
        self._cap_thread.start()
        Clock.schedule_interval(self._poll_gyro, 0.1)

    def _stop_capture(self):
        self._capturing = False
        self._btn_capture.text = "● INICIAR"
        Clock.unschedule(self._poll_gyro)

    def _capture_loop(self):
        while self._capturing:
            gyro_mag = self._gyro_magnitude()
            if gyro_mag > GYRO_MAX_RAD:
                Clock.schedule_once(lambda _: self._mark_gyro(False), 0)
                time.sleep(0.05)
                continue

            Clock.schedule_once(lambda _: self._mark_gyro(True), 0)
            self._grab_frame()

            if len(self._frames) >= TARGET_FRAMES:
                self._stop_capture()
                Clock.schedule_once(lambda _: self._on_target_reached(), 0)
                break

            time.sleep(CAPTURE_INTERVAL)

    def _grab_frame(self):
        try:
            tex = self._camera.texture
            if tex is None:
                return
            # Convert texture pixels → JPEG bytes
            img_bytes = _texture_to_jpeg(tex, quality=80,
                                          max_w=1280, max_h=720)
            if img_bytes is None:
                return

            frame = {
                "frame_id":    len(self._frames),
                "timestamp_ms": int(time.time() * 1000),
                "imu":          self._read_imu(),
                "camera":       {
                    "focal_px": _estimate_focal(tex.width, tex.height),
                    "width":    tex.width,
                    "height":   tex.height,
                },
                "_image_bytes": img_bytes,   # not sent in JSON
            }
            self._frames.append(frame)
            Clock.schedule_once(
                lambda _, n=len(self._frames): self._update_counter(n), 0)
        except Exception as e:
            pass   # texture not ready yet, skip frame

    # ── UI updates ────────────────────────────────────────────────────────────

    def _update_counter(self, n: int = 0):
        self._counter_lbl.text = f"{n} / {TARGET_FRAMES} fotos"
        self._progress.value = min(n, TARGET_FRAMES)
        if n >= MIN_FRAMES:
            self._btn_send.disabled = False
            self._btn_send.text = f"ENVIAR {n} →"

    def _mark_gyro(self, stable: bool):
        if stable:
            self._gyro_lbl.text = "● Estable"
            self._gyro_lbl.color = (0.2, 0.8, 0.3, 1)
        else:
            self._gyro_lbl.text = "⚠ Muy rápido"
            self._gyro_lbl.color = (0.9, 0.4, 0.1, 1)

    def _poll_gyro(self, _dt):
        stable = self._gyro_magnitude() <= GYRO_MAX_RAD
        self._mark_gyro(stable)

    def _on_target_reached(self):
        self._hint_lbl.text = f"✓ {TARGET_FRAMES} fotos capturadas. Tocá ENVIAR para procesar."

    # ── navigation ────────────────────────────────────────────────────────────

    def _on_send(self, *_):
        if len(self._frames) < MIN_FRAMES:
            return
        prog_screen = self.manager.get_screen("progress")
        prog_screen.frames    = self._frames.copy()
        prog_screen.server_ip = self.server_ip
        self.manager.current = "progress"

    # ── sensors ───────────────────────────────────────────────────────────────

    def _gyro_magnitude(self) -> float:
        if not _PLYER:
            return 0.0
        try:
            g = gyroscope.rotation
            if g is None:
                return 0.0
            return math.sqrt(sum(x * x for x in g))
        except Exception:
            return 0.0

    def _read_imu(self) -> dict:
        if not _PLYER:
            return {"accel": [0, 0, 9.8], "gyro": [0, 0, 0], "orient": [0, 0, 0]}
        try:
            acc  = list(accelerometer.acceleration or (0, 0, 9.8))
            gyro = list(gyroscope.rotation or (0, 0, 0))
            return {"accel": acc, "gyro": gyro, "orient": [0, 0, 0]}
        except Exception:
            return {"accel": [0, 0, 9.8], "gyro": [0, 0, 0], "orient": [0, 0, 0]}


# ── image helpers ─────────────────────────────────────────────────────────────

def _texture_to_jpeg(tex: Texture, quality: int = 80,
                      max_w: int = 1280, max_h: int = 720) -> bytes | None:
    try:
        from PIL import Image as PILImage

        pixels = bytes(tex.pixels)
        fmt    = tex.colorfmt.upper()
        mode   = {"RGBA": "RGBA", "RGB": "RGB", "BGRA": "RGBA"}.get(fmt, "RGBA")
        img    = PILImage.frombytes(mode, (tex.width, tex.height), pixels)

        # Kivy textures are flipped vertically
        img = img.transpose(PILImage.FLIP_TOP_BOTTOM)

        if mode == "RGBA":
            img = img.convert("RGB")

        # Downscale if needed
        img.thumbnail((max_w, max_h), PILImage.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def _estimate_focal(width: int, height: int) -> float:
    """Rough focal length estimate: typical smartphone FOV ~70 degrees."""
    fov_deg = 70.0
    import math
    return (max(width, height) / 2.0) / math.tan(math.radians(fov_deg / 2.0))
