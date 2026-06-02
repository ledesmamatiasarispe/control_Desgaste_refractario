import json
import os
import pathlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

_LAST_DIR_FILE = pathlib.Path.home() / ".refractory_last_import.json"


def _get_last_dir() -> str:
    try:
        if _LAST_DIR_FILE.exists():
            d = json.loads(_LAST_DIR_FILE.read_text()).get("last_dir", "")
            if pathlib.Path(d).is_dir():
                return d
    except Exception:
        pass
    return ""


def _set_last_dir(path: str):
    try:
        _LAST_DIR_FILE.write_text(json.dumps({"last_dir": path}))
    except Exception:
        pass

from PySide6.QtCore    import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFileDialog, QInputDialog, QMessageBox, QFrame,
)
from PySide6.QtGui import QColor, QFont

from core.loader import SUPPORTED_FILTER


@dataclass
class Scan:
    name:      str
    file_path: str
    load_date: Optional[str] = None
    color:     tuple = field(default=(180, 200, 220))


class CampaignPanel(QWidget):
    """Left sidebar: list of loaded scans within a campaign."""

    load_requested    = Signal(str, str)   # (file_path, name)
    select_requested  = Signal(int)        # index in list
    remove_requested  = Signal(int)
    compare_requested = Signal(int, int)   # (ref_idx, cur_idx)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(230)
        self._scans: List[Scan] = []
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        title = QLabel("Escaneos")
        title.setFont(QFont("", 10, QFont.Weight.Bold))
        root.addWidget(title)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.currentRowChanged.connect(self._on_select)
        root.addWidget(self._list)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        btn_add = QPushButton("+ Agregar escaneo…")
        btn_add.clicked.connect(self._add_mesh)
        root.addWidget(btn_add)

        btn_rem = QPushButton("✕ Quitar seleccionado")
        btn_rem.clicked.connect(self._remove_selected)
        root.addWidget(btn_rem)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep2)

        btn_cmp = QPushButton("⬛ Comparar escaneos…")
        btn_cmp.clicked.connect(self._compare)
        root.addWidget(btn_cmp)

        root.addStretch()

    # ── slots ─────────────────────────────────────────────────────────────

    def _add_mesh(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir malla 3D", _get_last_dir(), SUPPORTED_FILTER
        )
        if not path:
            return
        _set_last_dir(str(pathlib.Path(path).parent))
        default_name = os.path.splitext(os.path.basename(path))[0]
        name, ok = QInputDialog.getText(
            self, "Nombre de escaneo", "Nombre:", text=default_name
        )
        if not ok or not name.strip():
            name = default_name
        self.load_requested.emit(path, name.strip())

    def _remove_selected(self):
        row = self._list.currentRow()
        if row < 0:
            return
        self.remove_requested.emit(row)

    def _on_select(self, row: int):
        if row >= 0:
            self.select_requested.emit(row)

    def _compare(self):
        if len(self._scans) < 2:
            QMessageBox.information(self, "Comparar",
                "Necesitás al menos 2 escaneos cargados.")
            return
        names = [s.name for s in self._scans]
        from ui.comparison_dialog import ComparisonDialog
        dlg = ComparisonDialog(names, self)
        dlg.exec()

    # ── public ────────────────────────────────────────────────────────────

    def add_scan(self, name: str, path: str, load_date: Optional[str] = None):
        s = Scan(name=name, file_path=path, load_date=load_date)
        self._scans.append(s)
        if load_date:
            try:
                dt = datetime.fromisoformat(load_date)
                date_str = dt.strftime("%Y-%m-%d")
            except Exception:
                date_str = load_date[:10]
            label = f"  {name}\n  {date_str}"
        else:
            label = f"  {name}"
        item = QListWidgetItem(label)
        item.setForeground(QColor(*s.color))
        self._list.addItem(item)
        self._list.setCurrentRow(len(self._scans) - 1)

    def remove_scan(self, index: int):
        if 0 <= index < len(self._scans):
            self._scans.pop(index)
            self._list.takeItem(index)

    def get_scan(self, index: int) -> Scan | None:
        if 0 <= index < len(self._scans):
            return self._scans[index]
        return None

    def count(self) -> int:
        return len(self._scans)
