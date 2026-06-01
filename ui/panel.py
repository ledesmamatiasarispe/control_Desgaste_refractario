import os
from dataclasses import dataclass, field
from typing import List

from PySide6.QtCore    import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QFileDialog, QInputDialog, QMessageBox, QFrame,
)
from PySide6.QtGui import QColor, QFont

from core.loader import SUPPORTED_FILTER


@dataclass
class Campaign:
    name:      str
    file_path: str
    color:     tuple = field(default=(180, 200, 220))


class CampaignPanel(QWidget):
    """Left sidebar: list of loaded meshes / campaigns."""

    load_requested    = Signal(str, str)   # (file_path, name)
    select_requested  = Signal(int)        # index in list
    remove_requested  = Signal(int)
    compare_requested = Signal(int, int)   # (ref_idx, cur_idx)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(230)
        self._campaigns: List[Campaign] = []
        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        title = QLabel("Campañas")
        title.setFont(QFont("", 10, QFont.Weight.Bold))
        root.addWidget(title)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.currentRowChanged.connect(self._on_select)
        root.addWidget(self._list)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        btn_add = QPushButton("+ Agregar malla…")
        btn_add.clicked.connect(self._add_mesh)
        root.addWidget(btn_add)

        btn_rem = QPushButton("✕ Quitar seleccionada")
        btn_rem.clicked.connect(self._remove_selected)
        root.addWidget(btn_rem)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep2)

        btn_cmp = QPushButton("⬛ Comparar dos campañas…")
        btn_cmp.clicked.connect(self._compare)
        root.addWidget(btn_cmp)

        root.addStretch()

    # ── slots ─────────────────────────────────────────────────────────────

    def _add_mesh(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir malla 3D", "D:\\stl hornos", SUPPORTED_FILTER
        )
        if not path:
            return
        default_name = os.path.splitext(os.path.basename(path))[0]
        name, ok = QInputDialog.getText(
            self, "Nombre de campaña", "Nombre:", text=default_name
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
        if len(self._campaigns) < 2:
            QMessageBox.information(self, "Comparar",
                "Necesitás al menos 2 campañas cargadas.")
            return
        names = [c.name for c in self._campaigns]
        from ui.comparison_dialog import ComparisonDialog
        dlg = ComparisonDialog(names, self)
        dlg.exec()

    # ── public ────────────────────────────────────────────────────────────

    def add_campaign(self, name: str, path: str):
        c = Campaign(name=name, file_path=path)
        self._campaigns.append(c)
        item = QListWidgetItem(f"  {name}")
        item.setForeground(QColor(*c.color))
        self._list.addItem(item)
        self._list.setCurrentRow(len(self._campaigns) - 1)

    def remove_campaign(self, index: int):
        if 0 <= index < len(self._campaigns):
            self._campaigns.pop(index)
            self._list.takeItem(index)

    def get_campaign(self, index: int) -> Campaign | None:
        if 0 <= index < len(self._campaigns):
            return self._campaigns[index]
        return None

    def count(self) -> int:
        return len(self._campaigns)
