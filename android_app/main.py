"""
Refractory Capture — Android App
Kivy entry point. Run on desktop for testing: python main.py
Build APK: buildozer android debug
"""

import os
import sys
import traceback

os.environ.setdefault("KIVY_NO_ENV_CONFIG", "1")

# ── MUST be first: inject update path before any app module is imported ──────
try:
    import updater
    updater.inject_update_path()
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────────

try:
    from kivy.clock import Clock
    from kivy.uix.screenmanager import ScreenManager, SlideTransition
    from kivy.uix.label import Label
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.scrollview import ScrollView
    from kivymd.app import MDApp
    from kivymd.uix.dialog import MDDialog
    from kivymd.uix.button import MDFlatButton, MDRaisedButton

    from screens.connect  import ConnectScreen
    from screens.capture  import CaptureScreen
    from screens.progress import ProgressScreen
    from version import VERSION
    _IMPORT_ERROR = None
except Exception as e:
    _IMPORT_ERROR = traceback.format_exc()
    # Minimal fallback so Kivy can at least show the error
    try:
        from kivy.app import App
        from kivy.uix.label import Label
        from kivy.uix.scrollview import ScrollView
        from kivy.uix.boxlayout import BoxLayout
    except Exception:
        pass


# ── Fallback app shown when imports fail ─────────────────────────────────────

class _ErrorApp:
    """Minimal Kivy App that shows the crash traceback on screen."""
    def run(self):
        try:
            from kivy.app import App
            from kivy.uix.label import Label
            from kivy.uix.scrollview import ScrollView

            class CrashApp(App):
                def build(self):
                    lbl = Label(
                        text=f"[b]ERROR AL INICIAR[/b]\n\n{_IMPORT_ERROR}",
                        markup=True,
                        font_size="12sp",
                        size_hint_y=None,
                        valign="top",
                    )
                    lbl.bind(texture_size=lambda inst, val: setattr(inst, "height", val[1]))
                    sv = ScrollView()
                    sv.add_widget(lbl)
                    return sv

            CrashApp().run()
        except Exception as e:
            # Absolute last resort: write to a log file
            try:
                with open("/sdcard/refractory_crash.txt", "w") as f:
                    f.write(_IMPORT_ERROR or str(e))
            except Exception:
                pass


# ── Main app ──────────────────────────────────────────────────────────────────

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
        applied = updater.get_applied_version()
        running = applied or VERSION
        self.title = f"Refractory Capture  v{running}"
        updater.check_in_background(self._on_update_found)

    # ── update handling ───────────────────────────────────────────────────────

    def _on_update_found(self, info):
        Clock.schedule_once(lambda _: self._show_update_dialog(info), 0)

    def _show_update_dialog(self, info):
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

    def _update_cb(self, pct, msg):
        Clock.schedule_once(
            lambda _, m=msg: setattr(self._progress_dialog, "text", m), 0
        )


if __name__ == "__main__":
    if _IMPORT_ERROR:
        _ErrorApp().run()
    else:
        try:
            RefractoryApp().run()
        except Exception:
            _IMPORT_ERROR = traceback.format_exc()
            _ErrorApp().run()
