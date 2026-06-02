import numpy as np
from PySide6.QtCore    import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QDoubleSpinBox, QPushButton,
    QDialogButtonBox, QTableWidget, QTableWidgetItem,
    QHeaderView,
)
from PySide6.QtGui import QFont, QColor


class CalibrationDistanceDialog(QDialog):
    """
    Muestra las 3 distancias medidas entre los puntos de calibración.
    El usuario ingresa UNA distancia real; las otras dos se calculan
    automáticamente (escala uniforme — no hay deformación).
    """

    def __init__(self, meas_12: float, meas_13: float, meas_23: float,
                 unit_factor: float, unit_suffix: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Calibración de distancias")
        self.setMinimumWidth(440)

        self._meas   = [meas_12, meas_13, meas_23]
        self._uf     = unit_factor
        self._us     = unit_suffix
        self._pairs  = ["P1 → P2", "P1 → P3", "P2 → P3"]
        self._updating = False

        self._ref_dist_mm: float | None = None

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Ingresá la distancia real de <b>cualquiera</b> de los pares. "
            "Las demás se calculan automáticamente "
            f"(escala uniforme, unidad: <b>{self._us}</b>)."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Table
        grp = QGroupBox("Distancias entre los 3 puntos")
        glayout = QVBoxLayout(grp)

        self._table = QTableWidget(3, 3)
        self._table.setHorizontalHeaderLabels([
            "Puntos",
            f"Medido en malla ({self._us})",
            f"Distancia real ({self._us})",
        ])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setFixedHeight(118)

        self._spins: list[QDoubleSpinBox] = []
        for i, (pair, meas_raw) in enumerate(zip(self._pairs, self._meas)):
            lbl = QTableWidgetItem(pair)
            lbl.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 0, lbl)

            meas_item = QTableWidgetItem(f"{meas_raw * self._uf:.4f}")
            meas_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            meas_item.setForeground(QColor(140, 140, 140))
            self._table.setItem(i, 1, meas_item)

            spin = QDoubleSpinBox()
            spin.setRange(0, 999999)
            spin.setValue(0)
            spin.setDecimals(4)
            spin.setSpecialValueText("—")
            spin.setSuffix(f" {self._us}")
            spin.valueChanged.connect(lambda val, idx=i: self._on_spin_changed(idx))
            self._table.setCellWidget(i, 2, spin)
            self._spins.append(spin)

        glayout.addWidget(self._table)
        layout.addWidget(grp)

        # Scale factor display
        row = QHBoxLayout()
        row.addWidget(QLabel("Factor de escala:"))
        self._lbl_factor = QLabel("—")
        self._lbl_factor.setFont(QFont("", 10, QFont.Weight.Bold))
        row.addWidget(self._lbl_factor)
        row.addStretch()
        layout.addLayout(row)

        self._lbl_note = QLabel("Ingresá una distancia real para comenzar.")
        self._lbl_note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self._lbl_note)

        # Buttons
        btns = QDialogButtonBox()
        self._btn_save = QPushButton("Guardar calibración")
        self._btn_save.setDefault(True)
        btn_cancel = QPushButton("Cancelar")
        btns.addButton(self._btn_save,  QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton(btn_cancel,      QDialogButtonBox.ButtonRole.RejectRole)
        self._btn_save.clicked.connect(self._on_save)
        btn_cancel.clicked.connect(self.reject)
        layout.addWidget(btns)

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_spin_changed(self, changed_idx: int):
        if self._updating:
            return
        real_display = self._spins[changed_idx].value()
        meas_raw     = self._meas[changed_idx]

        if real_display <= 0 or meas_raw <= 0:
            self._lbl_factor.setText("—")
            self._lbl_note.setText("Ingresá una distancia real para comenzar.")
            return

        real_mm = real_display / self._uf
        scale   = real_mm / meas_raw

        self._lbl_factor.setText(f"×{scale:.6f}")
        self._lbl_note.setText(
            f"Todos los pares escalarán ×{scale:.6f} — "
            "el factor se aplica uniformemente."
        )

        # Propagate to the other two spins
        self._updating = True
        for i, (spin, m) in enumerate(zip(self._spins, self._meas)):
            if i != changed_idx:
                spin.setValue(m * scale * self._uf)
        self._updating = False

    def _on_save(self):
        # Find any spin with a positive value
        for spin, meas_raw in zip(self._spins, self._meas):
            real_display = spin.value()
            if real_display > 0 and meas_raw > 0:
                real_mm = real_display / self._uf
                scale   = real_mm / meas_raw
                # ref_dist stored as the effective real P1→P2 distance in mm
                self._ref_dist_mm = self._meas[0] * scale
                self.accept()
                return

        # No input — use measured P1→P2 as-is
        self._ref_dist_mm = self._meas[0]
        self.accept()

    # ── result ────────────────────────────────────────────────────────────────

    def ref_dist_mm(self) -> float:
        return self._ref_dist_mm
