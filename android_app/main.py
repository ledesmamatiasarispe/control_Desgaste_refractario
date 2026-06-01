"""
Refractory Capture — Android App
Kivy entry point. Run on desktop for testing: python main.py
Build APK: buildozer android debug
"""

import os
os.environ.setdefault("KIVY_NO_ENV_CONFIG", "1")

# ── MUST be first: inject update path before any app module is imported ──────
import updater
updater.inject_update_path()
# ─────────────────────────────────────────────────────────────────────────────

from kivy.clock import Clock
from kivy.uix.screenmanager import ScreenManager, SlideTransition
from kivymd.app import MDApp
from kivymd.uix.dialog import MDDialog
from kivymd.uix.button import MDFlatButton, MDRaisedButton

from screens.connect  import ConnectScreen
from screens.capture  import CaptureScreen
from screens.progress import ProgressScreen
from version import VERSION


class RefractoryApp(MDApp):
    title = "Refractory Capture"

    def build(self):
        self.theme_cls.theme_style    = "Dark"
        self.theme_cls.primary_palette = "Blue"

        sm = ScreenManager(transition=SlideTransition())
        sm.add_widget(ConnectScreen(name="connect"))
        sm.add_widget(CaptureScreen(name="capture"))
        sm.add_widget(ProgressScreen(name="progress"))
        return sm

    def on_start(self):
        # Show current version in window title (desktop) or quietly log
        applied = updater.get_applied_version()
        running = applied or VERSION
        self.title = f"Refractory Capture  v{running}"

        # Check for updates silently in background
        updater.check_in_background(self._on_update_found)

    # ── update handling ───────────────────────────────────────────────────────

    def _on_update_found(self, info: updater.UpdateInfo):
        """Called from background thread when a newer version is available."""
        Clock.schedule_once(lambda _: self._show_update_dialog(info), 0)

    def _show_update_dialog(self, info: updater.UpdateInfo):
        self._update_info = info
        notes = info.release_notes[:200] + "…" if len(info.release_notes) > 200 \
                else info.release_notes
        self._update_dialog = MDDialog(
            title="Actualización disponible",
            text=(
                f"v{info.current_version}  →  v{info.remote_version}\n\n"
                f"{notes}\n\n"
                "Se descarga el APK y el instalador del sistema se abre automáticamente."
            ),
            buttons=[
                MDFlatButton(
                    text="Ahora no",
                    on_release=lambda _: self._update_dialog.dismiss(),
                ),
                MDRaisedButton(
                    text="⬇ Instalar",
                    on_release=lambda _: self._start_update(),
                ),
            ],
        )
        self._update_dialog.open()

    def _start_update(self):
        self._update_dialog.dismiss()
        self._progress_dialog = MDDialog(
            title="Descargando APK…",
            text="Conectando con GitHub…",
        )
        self._progress_dialog.open()

        import threading
        threading.Thread(
            target=updater.download_and_install,
            args=(self._update_info,),
            kwargs={"progress_cb": self._update_cb},
            daemon=True,
        ).start()

    def _update_cb(self, pct: int, msg: str):
        Clock.schedule_once(
            lambda _, m=msg: setattr(self._progress_dialog, "text", m), 0
        )


if __name__ == "__main__":
    RefractoryApp().run()
