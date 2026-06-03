import json
import pathlib

from PySide6.QtWidgets import (
    QDialog, QTabWidget, QWidget, QFormLayout, QVBoxLayout,
    QHBoxLayout, QLineEdit, QPushButton, QLabel, QDialogButtonBox,
    QFileDialog, QSpinBox, QGroupBox,
)
from PySide6.QtCore import Qt

SETTINGS_FILE = pathlib.Path.home() / ".refractory_settings.json"

DEFAULTS = {
    "work_root":   str(pathlib.Path.home() / "AppData" / "Local" / "Temp" / "refractory_capture"),
    "output_dir":  r"D:\stl hornos\reconstructions",
    "server_port": 5005,
}


def load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text())
            return {**DEFAULTS, **data}
    except Exception:
        pass
    return dict(DEFAULTS)


def save_settings(settings: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except Exception:
        pass


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configuración")
        self.setMinimumWidth(520)
        self._settings = load_settings()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        tabs = QTabWidget()
        tabs.addTab(self._build_server_tab(), "Servidor de captura")
        root.addWidget(tabs)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ── Pestaña servidor ──────────────────────────────────────────────────────

    def _build_server_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)

        # Temp images
        grp_temp = QGroupBox("Imágenes temporales")
        form_temp = QFormLayout(grp_temp)

        self._edit_work = QLineEdit(self._settings["work_root"])
        btn_work = QPushButton("…")
        btn_work.setFixedWidth(32)
        btn_work.clicked.connect(lambda: self._browse(self._edit_work))
        row_work = QHBoxLayout()
        row_work.addWidget(self._edit_work)
        row_work.addWidget(btn_work)
        form_temp.addRow("Directorio temporal:", row_work)

        lbl_info = QLabel(
            "Las fotos del celular y los archivos intermedios se guardan aquí\n"
            "durante la reconstrucción y se borran al terminar."
        )
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("color: gray; font-size: 11px;")
        form_temp.addRow(lbl_info)
        layout.addWidget(grp_temp)

        # Output mesh dir
        grp_out = QGroupBox("Meshes reconstruidos")
        form_out = QFormLayout(grp_out)

        self._edit_output = QLineEdit(self._settings["output_dir"])
        btn_output = QPushButton("…")
        btn_output.setFixedWidth(32)
        btn_output.clicked.connect(lambda: self._browse(self._edit_output))
        row_out = QHBoxLayout()
        row_out.addWidget(self._edit_output)
        row_out.addWidget(btn_output)
        form_out.addRow("Directorio de salida:", row_out)

        lbl_out = QLabel("Los archivos OBJ generados se copian aquí al terminar.")
        lbl_out.setWordWrap(True)
        lbl_out.setStyleSheet("color: gray; font-size: 11px;")
        form_out.addRow(lbl_out)
        layout.addWidget(grp_out)

        # Server port
        grp_port = QGroupBox("Red")
        form_port = QFormLayout(grp_port)
        self._spin_port = QSpinBox()
        self._spin_port.setRange(1024, 65535)
        self._spin_port.setValue(self._settings["server_port"])
        form_port.addRow("Puerto del servidor:", self._spin_port)
        lbl_port = QLabel("El celular se conecta a esta IP:puerto. Requiere reiniciar la app.")
        lbl_port.setWordWrap(True)
        lbl_port.setStyleSheet("color: gray; font-size: 11px;")
        form_port.addRow(lbl_port)
        layout.addWidget(grp_port)

        layout.addStretch()
        return w

    # ── helpers ───────────────────────────────────────────────────────────────

    def _browse(self, edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(
            self, "Seleccionar carpeta", edit.text() or str(pathlib.Path.home())
        )
        if folder:
            edit.setText(folder)

    def _accept(self):
        self._settings["work_root"]   = self._edit_work.text().strip()
        self._settings["output_dir"]  = self._edit_output.text().strip()
        self._settings["server_port"] = self._spin_port.value()
        save_settings(self._settings)
        self.accept()

    def get_settings(self) -> dict:
        return dict(self._settings)
