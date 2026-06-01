from typing import List

from PySide6.QtCore    import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QCheckBox, QDoubleSpinBox, QPushButton,
    QLabel, QDialogButtonBox, QGroupBox, QProgressBar,
)
from PySide6.QtCore import QThread, Signal

from core.heatmap import COLORMAPS


class _Worker(QThread):
    done    = Signal(object)   # WearResult
    error   = Signal(str)

    def __init__(self, ref_data, cur_data, align: bool):
        super().__init__()
        self.ref_data = ref_data
        self.cur_data = cur_data
        self.do_align = align

    def run(self):
        try:
            from core.wear      import compute_wear
            from core.alignment import icp_align
            cur = self.cur_data
            if self.do_align:
                cur = icp_align(cur, self.ref_data)
            result = compute_wear(self.ref_data, cur)
            self.done.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class ComparisonDialog(QDialog):
    def __init__(self, campaign_names: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Comparar campañas — Análisis de desgaste")
        self.setMinimumWidth(380)
        self._names   = campaign_names
        self._result  = None
        self._ref_idx = 0
        self._cur_idx = 1
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Selección de campañas
        grp = QGroupBox("Selección")
        form = QFormLayout(grp)

        self._cbo_ref = QComboBox()
        self._cbo_cur = QComboBox()
        for n in self._names:
            self._cbo_ref.addItem(n)
            self._cbo_cur.addItem(n)
        if len(self._names) > 1:
            self._cbo_cur.setCurrentIndex(1)

        form.addRow("Referencia (estado inicial):", self._cbo_ref)
        form.addRow("Actual (estado a medir):",     self._cbo_cur)
        layout.addWidget(grp)

        # Opciones
        grp2 = QGroupBox("Opciones")
        form2 = QFormLayout(grp2)

        self._chk_align = QCheckBox("Alinear con ICP antes de comparar")
        self._chk_align.setChecked(True)
        form2.addRow(self._chk_align)

        self._cbo_cmap = QComboBox()
        for cm in COLORMAPS:
            self._cbo_cmap.addItem(cm)
        form2.addRow("Mapa de colores:", self._cbo_cmap)

        self._spin_max = QDoubleSpinBox()
        self._spin_max.setRange(0, 9999)
        self._spin_max.setValue(0)
        self._spin_max.setSpecialValueText("Auto (percentil 95)")
        self._spin_max.setSuffix(" mm")
        form2.addRow("Desgaste máximo en escala:", self._spin_max)

        layout.addWidget(grp2)

        # Progress
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._lbl_status = QLabel("")
        layout.addWidget(self._lbl_status)

        # Buttons
        btns = QDialogButtonBox()
        self._btn_run = QPushButton("Calcular desgaste")
        self._btn_run.setDefault(True)
        btns.addButton(self._btn_run, QDialogButtonBox.ButtonRole.AcceptRole)
        btn_cancel = QPushButton("Cancelar")
        btns.addButton(btn_cancel, QDialogButtonBox.ButtonRole.RejectRole)
        btn_cancel.clicked.connect(self.reject)
        self._btn_run.clicked.connect(self._run)
        layout.addWidget(btns)

    def _run(self):
        ri = self._cbo_ref.currentIndex()
        ci = self._cbo_cur.currentIndex()
        if ri == ci:
            self._lbl_status.setText("⚠ Elegí dos campañas distintas.")
            return

        self._ref_idx = ri
        self._cur_idx = ci

        # Retrieve MeshData from parent window
        from app.main_window import MainWindow
        mw = self.parent()
        while mw and not isinstance(mw, MainWindow):
            mw = mw.parent()
        if mw is None:
            self._lbl_status.setText("Error: ventana principal no encontrada.")
            return

        ref_data = mw.get_mesh_data(ri)
        cur_data = mw.get_mesh_data(ci)
        if ref_data is None or cur_data is None:
            self._lbl_status.setText("Error: malla no cargada.")
            return

        self._btn_run.setEnabled(False)
        self._progress.setVisible(True)
        self._lbl_status.setText("Calculando…")

        self._worker = _Worker(
            ref_data, cur_data,
            align=self._chk_align.isChecked()
        )
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_done(self, result):
        self._progress.setVisible(False)
        self._lbl_status.setText(
            f"✓  Máx: {result.max_wear:.2f}  |  "
            f"Media: {result.mean_wear:.2f}  |  "
            f"P95: {result.p95_wear:.2f}"
        )
        self._wear_result = result
        self._btn_run.setEnabled(True)

        # Apply heatmap via parent window
        from app.main_window import MainWindow
        from core.heatmap import distances_to_colors
        mw = self.parent()
        while mw and not isinstance(mw, MainWindow):
            mw = mw.parent()
        if mw:
            clamp = self._spin_max.value() or None
            colors = distances_to_colors(
                result.distances,
                colormap=self._cbo_cmap.currentText(),
                clamp_max=clamp,
            )
            mw.show_comparison(self._ref_idx, self._cur_idx, colors, result)

        self.accept()

    def _on_error(self, msg):
        self._progress.setVisible(False)
        self._lbl_status.setText(f"Error: {msg}")
        self._btn_run.setEnabled(True)

    def selection(self):
        return self._cbo_ref.currentIndex(), self._cbo_cur.currentIndex()
