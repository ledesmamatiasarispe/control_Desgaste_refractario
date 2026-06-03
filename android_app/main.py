"""
Refractory Capture — TEST MINIMO
Si esta pantalla aparece, Kivy funciona en el dispositivo.
"""
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button


class TestApp(App):
    def build(self):
        layout = BoxLayout(orientation="vertical", padding=40, spacing=20)

        layout.add_widget(Label(
            text="[b]Refractory Capture[/b]\nTest OK",
            markup=True,
            font_size="28sp",
            halign="center",
        ))

        layout.add_widget(Label(
            text="Si ves esta pantalla,\nKivy funciona correctamente.",
            font_size="18sp",
            halign="center",
        ))

        btn = Button(
            text="TOCA AQUI",
            size_hint=(1, 0.3),
            font_size="22sp",
        )
        btn.bind(on_press=lambda _: setattr(
            btn, "text", "Funciona!"))
        layout.add_widget(btn)

        return layout


if __name__ == "__main__":
    TestApp().run()
