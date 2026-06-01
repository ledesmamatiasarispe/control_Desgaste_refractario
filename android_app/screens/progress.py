"""Pantalla de upload y seguimiento de reconstrucción."""

import json
import threading
import time
from typing import List

import requests
from kivy.clock import Clock
from kivy.uix.screenmanager import Screen
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDRaisedButton, MDFlatButton
from kivymd.uix.label import MDLabel
from kivymd.uix.progressbar import MDProgressBar

DEFAULT_PORT   = 5005
MAX_RETRIES    = 3
RETRY_DELAY    = 2.0   # seconds between retries
POLL_INTERVAL  = 3.0   # seconds between status polls


class ProgressScreen(Screen):
    server_ip: str = ""
    frames:    List[dict] = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._job_id     = None
        self._cancelled  = False
        self._build_ui()

    def on_enter(self):
        self._cancelled = False
        self._reset_ui()
        Clock.schedule_once(lambda _: self._start(), 0.3)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = MDBoxLayout(
            orientation="vertical",
            padding=24,
            spacing=16,
        )

        root.add_widget(MDLabel(
            text="Procesando escaneo",
            halign="center",
            font_style="H5",
            size_hint_y=None,
            height=56,
        ))

        # Upload progress
        root.add_widget(MDLabel(
            text="Subiendo fotogramas",
            font_style="Subtitle1",
            size_hint_y=None,
            height=28,
        ))
        self._upload_lbl = MDLabel(
            text="Esperando…",
            font_style="Caption",
            size_hint_y=None,
            height=24,
        )
        root.add_widget(self._upload_lbl)
        self._upload_bar = MDProgressBar(
            value=0, max=100,
            size_hint=(1, None), height=8,
        )
        root.add_widget(self._upload_bar)

        # Reconstruction progress
        root.add_widget(MDLabel(
            text="Reconstrucción 3D",
            font_style="Subtitle1",
            size_hint_y=None,
            height=28,
        ))
        self._recon_lbl = MDLabel(
            text="—",
            font_style="Caption",
            size_hint_y=None,
            height=24,
        )
        root.add_widget(self._recon_lbl)
        self._recon_bar = MDProgressBar(
            value=0, max=100,
            size_hint=(1, None), height=8,
        )
        root.add_widget(self._recon_bar)

        root.add_widget(MDLabel())  # spacer

        # Result label
        self._result_lbl = MDLabel(
            text="",
            halign="center",
            font_style="Body1",
            size_hint_y=None,
            height=40,
        )
        root.add_widget(self._result_lbl)

        # Buttons
        btns = MDBoxLayout(
            orientation="horizontal",
            size_hint=(1, None),
            height=56,
            spacing=16,
        )
        self._btn_cancel = MDFlatButton(
            text="Cancelar",
            on_release=self._cancel,
        )
        btns.add_widget(self._btn_cancel)

        self._btn_retry = MDRaisedButton(
            text="Reintentar",
            on_release=self._retry,
            disabled=True,
        )
        btns.add_widget(self._btn_retry)

        self._btn_new = MDRaisedButton(
            text="Nuevo escaneo",
            on_release=lambda _: setattr(self.manager, "current", "capture"),
            disabled=True,
        )
        btns.add_widget(self._btn_new)
        root.add_widget(btns)

        self.add_widget(root)

    def _reset_ui(self):
        self._upload_bar.value  = 0
        self._recon_bar.value   = 0
        self._upload_lbl.text   = "Esperando…"
        self._recon_lbl.text    = "—"
        self._result_lbl.text   = ""
        self._btn_retry.disabled = True
        self._btn_new.disabled   = True

    # ── upload + poll ─────────────────────────────────────────────────────────

    def _start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        base = f"http://{self.server_ip}:{DEFAULT_PORT}"
        frames = self.frames

        # ── Step 1: create job ──
        try:
            r = requests.post(f"{base}/new_job", timeout=6)
            self._job_id = r.json()["job_id"]
        except Exception as e:
            self._ui_error(f"No se pudo crear trabajo: {e}")
            return

        # ── Step 2: check which frames server already has (resume support) ──
        try:
            r = requests.get(f"{base}/received_frames/{self._job_id}", timeout=6)
            already = set(r.json().get("frames", []))
        except Exception:
            already = set()

        pending = [f for f in frames if f["frame_id"] not in already]

        # ── Step 3: upload frames ──
        total    = len(frames)
        uploaded = len(already)

        for frame in pending:
            if self._cancelled:
                return
            for attempt in range(MAX_RETRIES):
                try:
                    meta = {k: v for k, v in frame.items() if k != "_image_bytes"}
                    r = requests.post(
                        f"{base}/upload_frame/{self._job_id}/{frame['frame_id']}",
                        files={"image": ("frame.jpg", frame["_image_bytes"],
                                         "image/jpeg")},
                        data={"meta": json.dumps(meta)},
                        timeout=30,
                    )
                    if r.ok:
                        uploaded += 1
                        pct = int(uploaded / total * 100)
                        Clock.schedule_once(
                            lambda _, p=pct, u=uploaded, t=total:
                            self._ui_upload(p, f"{u}/{t} fotos subidas"), 0)
                        break
                except Exception as e:
                    if attempt == MAX_RETRIES - 1:
                        Clock.schedule_once(
                            lambda _, e=e: self._ui_upload(
                                None, f"⚠ Error en frame {frame['frame_id']}: {e}"), 0)
                    time.sleep(RETRY_DELAY)

        # ── Step 4: start reconstruction ──
        try:
            requests.post(
                f"{base}/start_reconstruct/{self._job_id}",
                json={"total_frames": total},
                timeout=10,
            )
        except Exception as e:
            self._ui_error(f"No se pudo iniciar reconstrucción: {e}")
            return

        # ── Step 5: poll status ──
        self._poll_status(base)

    def _poll_status(self, base: str):
        while not self._cancelled:
            try:
                r = requests.get(
                    f"{base}/status/{self._job_id}", timeout=10)
                data = r.json()
                status   = data.get("status", "")
                progress = data.get("progress", 0)
                message  = data.get("message", "")

                Clock.schedule_once(
                    lambda _, p=progress, m=message:
                    self._ui_recon(p, m), 0)

                if status == "done":
                    Clock.schedule_once(lambda _: self._ui_done(), 0)
                    return
                if status == "error":
                    err = data.get("error", "Error desconocido")
                    Clock.schedule_once(lambda _, e=err: self._ui_error(e), 0)
                    return
            except Exception as e:
                pass  # network blip, keep polling

            time.sleep(POLL_INTERVAL)

    # ── UI callbacks (must run on main thread via Clock) ─────────────────────

    def _ui_upload(self, pct, msg: str):
        if pct is not None:
            self._upload_bar.value = pct
        self._upload_lbl.text = msg

    def _ui_recon(self, pct: int, msg: str):
        self._recon_bar.value = pct
        self._recon_lbl.text  = msg

    def _ui_done(self):
        self._recon_bar.value  = 100
        self._result_lbl.text  = "✓ Mesh generado — abrilo en el PC desde\nD:\\stl hornos\\reconstructions\\"
        self._result_lbl.color = (0.2, 0.8, 0.3, 1)
        self._btn_new.disabled = False
        self._btn_cancel.text  = "← Volver"

    def _ui_error(self, msg: str):
        self._result_lbl.text  = f"Error: {msg}"
        self._result_lbl.color = (0.9, 0.2, 0.2, 1)
        self._btn_retry.disabled = False

    # ── controls ──────────────────────────────────────────────────────────────

    def _cancel(self, *_):
        self._cancelled = True
        self.manager.current = "capture"

    def _retry(self, *_):
        self._btn_retry.disabled = True
        self._reset_ui()
        self._job_id = None
        Clock.schedule_once(lambda _: self._start(), 0.2)
