"""Pantalla de conexión al servidor PC."""

import json
import pathlib
import threading

from kivy.uix.screenmanager import Screen
from kivy.clock import Clock
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDRaisedButton
from kivymd.uix.label import MDLabel
from kivymd.uix.textfield import MDTextField
from kivymd.uix.card import MDCard
import requests

CONFIG_FILE = pathlib.Path.home() / ".refractory_mobile.json"
DEFAULT_PORT = 5005
TIMEOUT = 4


class ConnectScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._last_ip = self._load_ip()
        self._build_ui()

    def _build_ui(self):
        root = MDBoxLayout(
            orientation="vertical",
            padding=24,
            spacing=16,
        )

        # Title
        root.add_widget(MDLabel(
            text="Refractory Capture",
            halign="center",
            font_style="H4",
            size_hint_y=None,
            height=60,
        ))

        root.add_widget(MDLabel(
            text="Conectate al servidor del PC",
            halign="center",
            font_style="Body1",
            size_hint_y=None,
            height=32,
        ))

        # Card with IP input
        card = MDCard(
            orientation="vertical",
            padding=20,
            spacing=12,
            size_hint=(1, None),
            height=200,
            elevation=4,
        )

        self._ip_field = MDTextField(
            hint_text="IP del PC (ej: 192.168.1.100)",
            text=self._last_ip,
            mode="outlined",
            icon_right="server-network",
        )
        card.add_widget(self._ip_field)

        self._status_lbl = MDLabel(
            text="",
            halign="center",
            font_style="Caption",
            size_hint_y=None,
            height=32,
        )
        card.add_widget(self._status_lbl)

        self._connect_btn = MDRaisedButton(
            text="  CONECTAR  ",
            on_release=self._on_connect,
            pos_hint={"center_x": 0.5},
        )
        card.add_widget(self._connect_btn)

        root.add_widget(card)
        root.add_widget(MDLabel())  # spacer
        self.add_widget(root)

    # ── actions ──────────────────────────────────────────────────────────────

    def _on_connect(self, *_):
        ip = self._ip_field.text.strip()
        if not ip:
            self._set_status("Ingresá la IP del PC", "error")
            return
        self._set_status("Conectando…", "info")
        self._connect_btn.disabled = True
        threading.Thread(target=self._ping, args=(ip,), daemon=True).start()

    def _ping(self, ip: str):
        url = f"http://{ip}:{DEFAULT_PORT}/ping"
        try:
            r = requests.get(url, timeout=TIMEOUT)
            data = r.json()
            if data.get("ok"):
                self._save_ip(ip)
                Clock.schedule_once(lambda _: self._on_connected(ip), 0)
            else:
                Clock.schedule_once(lambda _: self._set_status(
                    "Respuesta inesperada del servidor", "error"), 0)
        except requests.ConnectionError:
            Clock.schedule_once(lambda _: self._set_status(
                f"No se encontró servidor en {ip}:{DEFAULT_PORT}", "error"), 0)
        except Exception as e:
            Clock.schedule_once(lambda _: self._set_status(
                f"Error: {e}", "error"), 0)
        finally:
            Clock.schedule_once(lambda _: setattr(
                self._connect_btn, "disabled", False), 0)

    def _on_connected(self, ip: str):
        self._set_status(f"✓ Conectado a {ip}", "success")
        # Pass server IP to other screens
        sm = self.manager
        sm.get_screen("capture").server_ip = ip
        sm.get_screen("progress").server_ip = ip
        Clock.schedule_once(lambda _: setattr(sm, "current", "capture"), 0.5)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str, kind: str = "info"):
        colors = {"info": (0.6, 0.6, 0.6, 1),
                  "success": (0.2, 0.7, 0.3, 1),
                  "error": (0.9, 0.2, 0.2, 1)}
        self._status_lbl.text = msg
        self._status_lbl.color = colors.get(kind, colors["info"])

    def _load_ip(self) -> str:
        try:
            if CONFIG_FILE.exists():
                return json.loads(CONFIG_FILE.read_text()).get("ip", "")
        except Exception:
            pass
        return ""

    def _save_ip(self, ip: str):
        try:
            CONFIG_FILE.write_text(json.dumps({"ip": ip}))
        except Exception:
            pass
