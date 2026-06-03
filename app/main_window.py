import json
import pathlib
import numpy as np
from typing import Dict, List, Optional

from PySide6.QtCore    import Qt, QThread, Signal, QObject
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QStatusBar, QToolBar, QMessageBox, QLabel, QSplitter,
    QGroupBox, QFormLayout, QComboBox, QSlider, QFrame,
    QFileDialog, QInputDialog, QPushButton, QMenu,
)
from PySide6.QtGui import QAction, QFont, QColor
from PySide6.QtCore import QSize

from app.gl_widget  import GLWidget, Mode
from ui.panel       import CampaignPanel
from core.loader    import MeshData, load_file
from core.heatmap   import COLORMAPS
from core.project   import (save_project, load_project, get_recent,
                             PROJECT_FILTER, PROJECT_EXT,
                             CampaignData, ScanMeta)


# ── background loader ────────────────────────────────────────────────────────

class _LoadWorker(QObject):
    done  = Signal(object, str)   # (MeshData, name)
    error = Signal(str)

    def __init__(self, path: str, name: str):
        super().__init__()
        self.path = path
        self.name = name

    def run(self):
        try:
            data = load_file(self.path)
            data.source_path = self.path   # always store original path, not any temp path
            self.done.emit(data, self.name)
        except Exception as e:
            self.error.emit(str(e))


# _ICPWorker removed — ICP now runs synchronously in the main thread
# to avoid BLAS/LAPACK crashes from Qt worker threads.


# ── main window ──────────────────────────────────────────────────────────────

_UNITS = {
    "mm":    (1.0,       "mm",    3),
    "cm":    (0.1,       "cm",    4),
    "m":     (0.001,     "m",     6),
    "pulg.": (1/25.4,    "pulg.", 4),
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Refractory Analyzer")
        self.resize(1300, 800)

        self._mesh_cache: Dict[int, MeshData]     = {}
        self._pristine_cache: Dict[int, MeshData] = {}   # mesh as originally loaded
        self._active_idx: Optional[int]            = None
        self._load_thread: Optional[QThread]   = None
        self._align_ref_dist: Optional[float]  = None
        self._project_path: Optional[str]        = None
        self._campaign_data: Optional[CampaignData] = None
        self._dirty: bool                        = False

        self._unit_factor   = 1.0
        self._unit_suffix   = "mm"
        self._unit_decimals = 3
        self._raw_radius: Optional[float] = None
        self._wear_vmax:  float           = 0.0
        self._modified_mesh_paths: set    = set()   # paths written by this session

        self._calib_file = pathlib.Path.home() / ".refractory_calibration.json"

        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._build_status()
        self._load_calibration()
        self._start_embedded_server()

        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._restore_last_project)

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left panel
        self._panel = CampaignPanel()
        self._panel.load_requested.connect(self._on_load_requested)
        self._panel.select_requested.connect(self._on_select)
        self._panel.remove_requested.connect(self._on_remove)
        self._panel.compare_requested.connect(self._on_compare)
        root.addWidget(self._panel)

        # GL viewer
        self._gl = GLWidget()
        self._gl.align_ready.connect(self._on_align_ready)
        self._gl.calibrate_ready.connect(self._on_calibrate_ready)
        self._gl.measure_done.connect(self._on_measurement)
        self._gl.crop_ready.connect(self._on_crop_ready)

        # Right panel
        right = self._build_right_panel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._gl)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([900, 260])
        root.addWidget(splitter)

    def _fmt(self, raw: float) -> str:
        v = raw * self._unit_factor
        return f"{v:.{self._unit_decimals}f} {self._unit_suffix}"

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(260)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)

        title = QLabel("Propiedades")
        title.setFont(QFont("", 10, QFont.Weight.Bold))
        layout.addWidget(title)

        # Unit selector
        unit_row = QWidget()
        ulay = QHBoxLayout(unit_row)
        ulay.setContentsMargins(0, 0, 0, 4)
        ulay.addWidget(QLabel("Unidad:"))
        self._cbo_units = QComboBox()
        for u in _UNITS:
            self._cbo_units.addItem(u)
        self._cbo_units.setMaximumWidth(80)
        self._cbo_units.currentTextChanged.connect(self._on_unit_change)
        ulay.addWidget(self._cbo_units)
        ulay.addStretch()
        layout.addWidget(unit_row)

        # Mesh stats
        grp_mesh = QGroupBox("Malla activa")
        form_mesh = QFormLayout(grp_mesh)
        self._lbl_verts  = QLabel("—")
        self._lbl_faces  = QLabel("—")
        self._lbl_radius = QLabel("—")
        form_mesh.addRow("Vértices:", self._lbl_verts)
        form_mesh.addRow("Caras:",    self._lbl_faces)
        form_mesh.addRow("Radio:",    self._lbl_radius)
        layout.addWidget(grp_mesh)

        # Alignment reference
        grp_align = QGroupBox("Alineación 3 puntos")
        form_align = QFormLayout(grp_align)
        self._lbl_ref_dist = QLabel("—  (sin referencia)")
        form_align.addRow("Dist. ref P1→P2:", self._lbl_ref_dist)
        from PySide6.QtWidgets import QPushButton
        btn_reset_scale = QPushButton("Limpiar referencia de escala")
        btn_reset_scale.clicked.connect(self._reset_align_ref)
        form_align.addRow(btn_reset_scale)
        layout.addWidget(grp_align)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # Wear stats
        grp_wear = QGroupBox("Desgaste")
        form_wear = QFormLayout(grp_wear)
        self._lbl_max  = QLabel("—")
        self._lbl_mean = QLabel("—")
        self._lbl_p95  = QLabel("—")
        form_wear.addRow("Máx:",  self._lbl_max)
        form_wear.addRow("Media:", self._lbl_mean)
        form_wear.addRow("P95:",  self._lbl_p95)
        layout.addWidget(grp_wear)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep2)

        # Colormap
        grp_cmap = QGroupBox("Mapa de colores")
        form_cmap = QVBoxLayout(grp_cmap)
        self._cbo_cmap = QComboBox()
        for cm in COLORMAPS:
            self._cbo_cmap.addItem(cm)
        self._cbo_cmap.currentTextChanged.connect(self._on_cmap_change)
        form_cmap.addWidget(self._cbo_cmap)

        self._colorbar_lbl = QLabel()
        self._colorbar_lbl.setFixedSize(220, 16)
        self._colorbar_lbl.setScaledContents(True)
        form_cmap.addWidget(self._colorbar_lbl)

        scale_row = QHBoxLayout()
        self._lbl_scale_min = QLabel("0")
        self._lbl_scale_max = QLabel("—")
        scale_row.addWidget(self._lbl_scale_min)
        scale_row.addStretch()
        scale_row.addWidget(self._lbl_scale_max)
        form_cmap.addLayout(scale_row)

        layout.addWidget(grp_cmap)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep3)

        # Clip planes
        from PySide6.QtWidgets import QSlider
        grp_clip = QGroupBox("Cortes")
        vlay = QVBoxLayout(grp_clip)

        vlay.addWidget(QLabel("Horizontal (Y):"))
        self._slider_h = QSlider(Qt.Orientation.Horizontal)
        self._slider_h.setRange(0, 100)
        self._slider_h.setValue(100)
        self._slider_h.setTickInterval(25)
        self._slider_h.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._slider_h.valueChanged.connect(self._update_clips)
        vlay.addWidget(self._slider_h)

        vlay.addWidget(QLabel("Vertical (X):"))
        self._slider_v = QSlider(Qt.Orientation.Horizontal)
        self._slider_v.setRange(0, 100)
        self._slider_v.setValue(100)
        self._slider_v.setTickInterval(25)
        self._slider_v.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._slider_v.valueChanged.connect(self._update_clips)
        vlay.addWidget(self._slider_v)

        btn_reset_clips = QPushButton("Restablecer cortes")
        btn_reset_clips.clicked.connect(self._reset_clips)
        vlay.addWidget(btn_reset_clips)
        layout.addWidget(grp_clip)

        sep4 = QFrame(); sep4.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep4)

        # Measurements
        grp_meas = QGroupBox("Mediciones (📏 M)")
        mlay = QVBoxLayout(grp_meas)
        self._meas_list = QLabel("—")
        self._meas_list.setWordWrap(True)
        self._meas_list.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._meas_list.setMinimumHeight(60)
        mlay.addWidget(self._meas_list)
        btn_clear_meas = QPushButton("Limpiar mediciones")
        btn_clear_meas.clicked.connect(self._clear_measurements)
        mlay.addWidget(btn_clear_meas)
        layout.addWidget(grp_meas)

        layout.addStretch()
        return w

    def _build_menu(self):
        mb = self.menuBar()

        # ── File ──
        file_menu = mb.addMenu("Archivo")

        act_new = QAction("Nueva campaña", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._new_project)
        file_menu.addAction(act_new)

        act_open = QAction("Abrir campaña…", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self._open_project)
        file_menu.addAction(act_open)

        # Recent submenu
        self._recent_menu = file_menu.addMenu("Recientes")
        self._refresh_recent_menu()

        file_menu.addSeparator()

        act_save = QAction("Guardar campaña", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._save_project)
        file_menu.addAction(act_save)

        act_saveas = QAction("Guardar campaña como…", self)
        act_saveas.setShortcut("Ctrl+Shift+S")
        act_saveas.triggered.connect(self._save_project_as)
        file_menu.addAction(act_saveas)

        file_menu.addSeparator()

        act_close_camp = QAction("🔒 Cerrar campaña", self)
        act_close_camp.triggered.connect(self._close_campaign)
        file_menu.addAction(act_close_camp)

        file_menu.addSeparator()
        act_settings = QAction("⚙ Configuración…", self)
        act_settings.setShortcut("Ctrl+,")
        act_settings.triggered.connect(self._open_settings)
        file_menu.addAction(act_settings)

        file_menu.addSeparator()
        act_quit = QAction("Salir", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        recent = get_recent()
        if not recent:
            self._recent_menu.addAction("(sin proyectos recientes)").setEnabled(False)
        for path in recent:
            name = pathlib.Path(path).stem
            act  = QAction(f"{name}  —  {path}", self)
            act.triggered.connect(lambda checked, p=path: self._open_project_path(p))
            self._recent_menu.addAction(act)

    def _build_toolbar(self):
        tb = QToolBar("Herramientas")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)

        def act(text, shortcut=None, checkable=False, tip=None):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(shortcut)
            if tip:
                a.setToolTip(tip)
            a.setCheckable(checkable)
            return a

        self._act_fit    = act("⊡ Encuadrar",    "F",  tip="Centrar vista en la malla")
        self._act_wire   = act("⬡ Contorno",      "W",  checkable=True, tip="Vista de contorno")
        self._act_nav    = act("↖ Navegar",       None, checkable=True, tip="Modo navegación")
        self._act_calib  = act("📐 Calibrar 3pt", None, checkable=True,
                               tip="Seleccioná 3 puntos de referencia — se guardan como calibración")
        self._act_align  = act("△ Alinear 3pt",   None, checkable=True,
                               tip="Alinear esta malla usando los 3 puntos calibrados")
        self._act_measure = act("📏 Medir",        "M",  checkable=True,
                               tip="Medir distancia entre dos puntos (misma malla o entre dos mallas)")
        self._act_crop       = act("⭕ Recortar crisol", "C", checkable=True,
                                   tip="Seleccioná 3 puntos en el borde del crisol para recortar el exterior")
        self._act_ref_volume = act("🫧 Volumen desgaste", None, checkable=True,
                                   tip="Mostrar referencia como sólido transparente para ver el volumen de material perdido")
        self._act_icp    = act("🎯 ICP tornillos", None,
                               tip="Refinar alineación con ICP usando solo la geometría cerca de los tornillos")
        self._act_icp.setEnabled(False)
        self._act_icp_full = act("🎯 ICP general", None,
                                 tip="Refinar alineación con ICP usando el mesh completo")
        self._act_icp_full.setEnabled(False)
        self._act_hmap   = act("⬛ Quitar mapa",   None, tip="Volver a color base")

        self._act_nav.setChecked(True)

        tb.addAction(self._act_fit)
        tb.addSeparator()
        tb.addAction(self._act_wire)
        tb.addSeparator()
        tb.addAction(self._act_nav)
        tb.addAction(self._act_calib)
        tb.addAction(self._act_align)
        tb.addAction(self._act_measure)
        tb.addAction(self._act_crop)
        tb.addSeparator()
        tb.addAction(self._act_ref_volume)
        tb.addAction(self._act_icp)
        tb.addAction(self._act_icp_full)
        tb.addAction(self._act_hmap)

        self._act_fit.triggered.connect(self._gl.fit_view)
        self._act_wire.toggled.connect(self._gl.toggle_wireframe)
        self._act_nav.triggered.connect(lambda: self._set_mode(Mode.NAVIGATE))
        self._act_calib.triggered.connect(lambda: self._set_mode(Mode.CALIBRATE_3PT))
        self._act_align.triggered.connect(lambda: self._set_mode(Mode.ALIGN_3PT))
        self._act_measure.triggered.connect(lambda: self._set_mode(Mode.MEASURE))
        self._act_crop.triggered.connect(lambda: self._set_mode(Mode.CROP_CYLINDER))
        self._act_ref_volume.toggled.connect(
            lambda on: self._gl.set_ref_mode("solid_transparent" if on else "wireframe")
        )
        self._act_icp.triggered.connect(lambda: self._run_icp(near_bolts=True))
        self._act_icp_full.triggered.connect(lambda: self._run_icp(near_bolts=False))
        self._act_hmap.triggered.connect(self._clear_heatmap)

    def _build_status(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status_main = QLabel("Sin malla cargada")
        sb.addWidget(self._status_main, 1)
        self._gl.status_message.connect(self._status_main.setText)
        self._server_lbl = QLabel("📡  iniciando…")
        self._server_lbl.setToolTip("IP del servidor — ingresala en la app del celular")
        sb.addPermanentWidget(self._server_lbl)

    # ── startup restore ──────────────────────────────────────────────────────

    def _restore_last_project(self):
        recent = get_recent()
        if recent:
            self._open_project_path(recent[0])

    # ── unit selector ────────────────────────────────────────────────────────

    def _on_unit_change(self, unit: str):
        self._unit_factor, self._unit_suffix, self._unit_decimals = _UNITS[unit]
        self._gl.set_unit(self._unit_factor, self._unit_suffix, self._unit_decimals)
        self._refresh_unit_labels()

    def _refresh_unit_labels(self):
        if self._raw_radius is not None:
            self._lbl_radius.setText(self._fmt(self._raw_radius))
        if hasattr(self, '_current_wear_result'):
            r = self._current_wear_result
            self._lbl_max.setText(self._fmt(r.max_wear))
            self._lbl_mean.setText(self._fmt(r.mean_wear))
            self._lbl_p95.setText(self._fmt(r.p95_wear))
        if self._align_ref_dist is not None:
            self._lbl_ref_dist.setText(f"{self._fmt(self._align_ref_dist)}  ✓ calibrado")
        meas = self._gl._measurements
        if meas:
            lines = [f"{i+1}:  {m.label}" for i, m in enumerate(meas)]
            self._meas_list.setText("\n".join(lines))

    # ── mode helpers ─────────────────────────────────────────────────────────

    def _set_mode(self, mode: Mode):
        self._act_nav.setChecked(mode == Mode.NAVIGATE)
        self._act_calib.setChecked(mode == Mode.CALIBRATE_3PT)
        self._act_align.setChecked(mode == Mode.ALIGN_3PT)
        self._act_measure.setChecked(mode == Mode.MEASURE)
        self._act_crop.setChecked(mode == Mode.CROP_CYLINDER)
        self._gl.set_mode(mode)

    # ── campaign slots ───────────────────────────────────────────────────────

    def _on_load_requested(self, path: str, name: str):
        self._status_main.setText(f"Cargando {name}…")
        # Run in background thread
        self._load_thread = QThread()
        self._worker = _LoadWorker(path, name)
        self._worker.moveToThread(self._load_thread)
        self._load_thread.started.connect(self._worker.run)
        self._worker.done.connect(self._on_mesh_loaded)
        self._worker.error.connect(self._on_load_error)
        self._worker.done.connect(self._load_thread.quit)
        self._worker.error.connect(self._load_thread.quit)
        self._load_thread.start()

    def _on_mesh_loaded(self, data: MeshData, name: str):
        from datetime import datetime
        from uuid import uuid4
        now = datetime.now().isoformat()
        idx = self._panel.count()  # index before adding

        if self._campaign_data is None:
            self._campaign_data = CampaignData(
                id=str(uuid4()), name="", scans=[], meshes=[],
                calibration=None, start_date=now, end_date=None,
            )
        elif not self._campaign_data.start_date:
            self._campaign_data.start_date = now

        self._campaign_data.scans.append(ScanMeta(
            id=str(uuid4()), name=name,
            source_path=data.source_path, load_date=now,
        ))

        self._panel.add_scan(name, data.source_path, load_date=now)
        self._mesh_cache[idx]     = data
        self._pristine_cache[idx] = data   # never overwritten — always the raw loaded mesh
        self._select_mesh(idx)
        self._mark_dirty()

    def _on_load_error(self, msg: str):
        QMessageBox.critical(self, "Error al cargar", msg)
        self._status_main.setText("Error al cargar malla.")

    def _on_select(self, index: int):
        self._select_mesh(index)

    def _on_remove(self, index: int):
        if self._campaign_data and 0 <= index < len(self._campaign_data.scans):
            self._campaign_data.scans.pop(index)

        self._mesh_cache.pop(index, None)
        self._pristine_cache.pop(index, None)
        # Re-index cache keys above the removed index
        new_cache, new_pristine = {}, {}
        for k, v in self._mesh_cache.items():
            new_cache[k if k < index else k - 1] = v
        for k, v in self._pristine_cache.items():
            new_pristine[k if k < index else k - 1] = v
        self._mesh_cache     = new_cache
        self._pristine_cache = new_pristine
        self._panel.remove_scan(index)

        if self._active_idx == index:
            self._active_idx = None
            if self._panel.count() > 0:
                self._select_mesh(min(index, self._panel.count() - 1))


    def _on_compare(self, ref: int, cur: int):
        pass  # comparison is handled directly by ComparisonDialog

    # ── base scan helpers ────────────────────────────────────────────────────

    def _base_idx(self) -> Optional[int]:
        """Index of the calibrated (base) scan, or None if not set."""
        if self._campaign_data and self._campaign_data.calibrated_scan_idx is not None:
            return self._campaign_data.calibrated_scan_idx
        return None

    def _base_mesh(self):
        idx = self._base_idx()
        return self._mesh_cache.get(idx) if idx is not None else None

    # ── ICP button state ─────────────────────────────────────────────────────

    def _refresh_icp_btn(self, idx=None):
        if idx is None:
            idx = self._active_idx
        scan = (self._campaign_data.scans[idx]
                if self._campaign_data and idx is not None
                   and idx < len(self._campaign_data.scans)
                else None)
        base_idx    = self._base_idx()
        base_ready  = (idx is not None and base_idx is not None
                       and idx != base_idx and self._base_mesh() is not None)
        has_pts     = scan is not None and bool(scan.align_pts)
        self._act_icp.setEnabled(base_ready and has_pts)
        self._act_icp_full.setEnabled(base_ready)

    # ── align markers ────────────────────────────────────────────────────────

    def _show_scan_align_pts(self, idx):
        """Restore the stored alignment markers for the given scan index."""
        scan = (self._campaign_data.scans[idx]
                if self._campaign_data and idx is not None
                   and idx < len(self._campaign_data.scans)
                else None)
        self._gl.show_align_pts(scan.align_pts if scan else None)

    # ── mesh selection ───────────────────────────────────────────────────────

    def _select_mesh(self, index: int):
        self._active_idx = index
        data = self._mesh_cache.get(index)
        if data is None:
            return
        self._gl.load_mesh(data)
        self._gl.set_reference_mesh(None)   # clear reference when selecting alone
        self._raw_radius = data.radius
        self._lbl_verts.setText(f"{data.vertex_count:,}")
        self._lbl_faces.setText(f"{data.face_count:,}")
        self._lbl_radius.setText(self._fmt(data.radius))
        self._lbl_max.setText("—")
        self._lbl_mean.setText("—")
        self._lbl_p95.setText("—")
        self._show_scan_align_pts(index)
        self._refresh_icp_btn(index)

    # ── clip planes ──────────────────────────────────────────────────────────

    def _update_clips(self):
        h = self._slider_h.value() / 100.0
        v = self._slider_v.value() / 100.0
        self._gl.set_clip_planes(h, v)

    def _reset_clips(self):
        self._slider_h.setValue(100)
        self._slider_v.setValue(100)

    # ── heatmap / comparison ─────────────────────────────────────────────────

    def show_comparison(self, ref_idx: int, cur_idx: int,
                        colors: np.ndarray, result):
        """Load both meshes and apply heatmap to current."""
        ref_data = self._mesh_cache.get(ref_idx)
        cur_data = self._mesh_cache.get(cur_idx)
        if ref_data is None or cur_data is None:
            return

        self._active_idx = cur_idx
        self._gl.load_mesh(cur_data)
        self._gl.apply_heatmap(colors)
        self._gl.set_reference_mesh(ref_data)

        self._raw_radius = cur_data.radius
        self._lbl_verts.setText(f"{cur_data.vertex_count:,}")
        self._lbl_faces.setText(f"{cur_data.face_count:,}")
        self._lbl_radius.setText(self._fmt(cur_data.radius))
        self._lbl_max.setText(self._fmt(result.max_wear))
        self._lbl_mean.setText(self._fmt(result.mean_wear))
        self._lbl_p95.setText(self._fmt(result.p95_wear))

        self._current_wear_result   = result
        self._current_wear_cmap     = self._cbo_cmap.currentText()
        self._current_wear_mesh_idx = cur_idx
        self._update_colorbar()

        # Re-apply current slider positions
        self._update_clips()

    def show_heatmap(self, mesh_idx: int, colors: np.ndarray, result):
        self._select_mesh(mesh_idx)
        self._gl.apply_heatmap(colors)
        self._lbl_max.setText(self._fmt(result.max_wear))
        self._lbl_mean.setText(self._fmt(result.mean_wear))
        self._lbl_p95.setText(self._fmt(result.p95_wear))
        self._current_wear_result  = result
        self._current_wear_cmap    = self._cbo_cmap.currentText()
        self._current_wear_mesh_idx = mesh_idx
        self._update_colorbar()

    def _clear_heatmap(self):
        self._gl.clear_heatmap()
        self._lbl_max.setText("—")
        self._lbl_mean.setText("—")
        self._lbl_p95.setText("—")

    def _on_cmap_change(self, cmap_name: str):
        if hasattr(self, '_current_wear_result'):
            from core.heatmap import distances_to_colors
            colors, vmax = distances_to_colors(
                self._current_wear_result.distances,
                colormap=cmap_name,
            )
            self._current_wear_cmap = cmap_name
            self._wear_vmax = vmax
            self._gl.apply_heatmap(colors)
            self._update_colorbar()

    def _update_colorbar(self):
        from core.heatmap import colorbar_image
        from PySide6.QtGui import QImage, QPixmap
        cmap  = getattr(self, '_current_wear_cmap', 'plasma')
        img   = colorbar_image(cmap, width=220, height=16)
        h, w  = img.shape[:2]
        qi    = QImage(img.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
        self._colorbar_lbl.setPixmap(QPixmap.fromImage(qi))
        if self._wear_vmax > 0:
            self._lbl_scale_min.setText(self._fmt(0))
            self._lbl_scale_max.setText(self._fmt(self._wear_vmax))

    # ── modified mesh persistence ────────────────────────────────────────────

    def _save_modified_mesh(self, data: MeshData, idx: int, suffix: str) -> str:
        """
        Export the modified mesh as a PLY file.
        - Primera modificación: crea un archivo nuevo junto al original.
        - Modificaciones posteriores: sobreescribe el mismo archivo (source_path ya es nuestro).
        Actualiza data.source_path y retorna la ruta.
        """
        import trimesh as _trimesh

        current = data.source_path or ""
        if current in self._modified_mesh_paths and pathlib.Path(current).exists():
            out_path = current          # sobreescribir archivo previo
        else:
            orig = pathlib.Path(current) if current else None
            if orig and orig.parent.exists():
                save_dir  = orig.parent
                base_name = orig.stem
            else:
                save_dir = pathlib.Path.home() / ".refractory_modified"
                save_dir.mkdir(exist_ok=True)
                scan = self._panel.get_scan(idx)
                base_name = scan.name if scan else f"mesh_{idx}"

            candidate = save_dir / f"{base_name}_{suffix}.ply"
            n = 1
            while candidate.exists():
                candidate = save_dir / f"{base_name}_{suffix}_{n}.ply"
                n += 1
            out_path = str(candidate)

        tm = _trimesh.Trimesh(
            vertices=data.vertices,
            faces=data.faces,
            vertex_normals=data.normals,
            process=False,
        )
        tm.export(out_path)
        self._modified_mesh_paths.add(out_path)
        data.source_path = out_path
        return out_path

    # ── close campaign ───────────────────────────────────────────────────────

    def _close_campaign(self):
        from datetime import datetime
        if self._campaign_data is None:
            QMessageBox.information(self, "Cerrar campaña",
                "No hay ninguna campaña abierta.")
            return
        if self._campaign_data.end_date:
            QMessageBox.information(self, "Cerrar campaña",
                "Esta campaña ya fue cerrada.")
            return
        self._campaign_data.end_date = datetime.now().isoformat()
        if self._project_path:
            self._do_save(self._project_path)
            self._status_main.setText("Campaña cerrada ✓")
        else:
            QMessageBox.information(self, "Cerrar campaña",
                "Guardá la campaña primero (Ctrl+S) para registrar el cierre.")
            self._campaign_data.end_date = None   # revert

    # ── 3-point calibration & alignment ─────────────────────────────────────

    def _load_calibration(self):
        """Restore saved reference distance from previous session."""
        try:
            if self._calib_file.exists():
                data = json.loads(self._calib_file.read_text())
                dist = float(data.get("ref_dist", 0))
                if dist > 0:
                    self._align_ref_dist = dist
                    self._lbl_ref_dist.setText(
                        f"{self._fmt(dist)}  ✓ calibrado"
                    )
        except Exception:
            pass  # corrupted file — ignore

    def _save_calibration(self, dist: float):
        try:
            self._calib_file.write_text(json.dumps({"ref_dist": dist}))
        except Exception:
            pass

    def _reset_align_ref(self):
        self._align_ref_dist = None
        self._lbl_ref_dist.setText("—  (sin calibración)")
        try:
            self._calib_file.unlink(missing_ok=True)
        except Exception:
            pass
        self._status_main.setText("Calibración de escala eliminada.")

    def _on_calibrate_ready(self, pts: list):
        """3 pts picked in CALIBRATE mode — show distance dialog, save reference, align."""
        from core.alignment import three_point_align
        from ui.calibration_dialog import CalibrationDistanceDialog
        idx  = self._active_idx
        data = self._pristine_cache.get(idx) if idx is not None else None
        if data is None:
            return
        try:
            from core.alignment import refine_pts_to_local_centroid
            from PySide6.QtWidgets import QApplication
            raw = [p.tolist() for p in (pts[0], pts[1], pts[2])]
            ref = refine_pts_to_local_centroid(raw, data.vertices)
            p1 = np.array(ref[0], dtype=np.float32)
            p2 = np.array(ref[1], dtype=np.float32)
            p3 = np.array(ref[2], dtype=np.float32)

            # Show original clicks (yellow) vs refined centroids (colored) before transforming
            self._gl.show_refinement_preview(raw, ref)
            QApplication.processEvents()

            meas_12 = float(np.linalg.norm((p2 - p1).astype(np.float64)))
            meas_13 = float(np.linalg.norm((p3 - p1).astype(np.float64)))
            meas_23 = float(np.linalg.norm((p3 - p2).astype(np.float64)))

            dlg = CalibrationDistanceDialog(
                meas_12, meas_13, meas_23,
                self._unit_factor, self._unit_suffix,
                parent=self,
            )
            if dlg.exec() != CalibrationDistanceDialog.DialogCode.Accepted:
                self._status_main.setText("Calibración cancelada.")
                return

            ref_dist = dlg.ref_dist_mm()

            self._align_ref_dist = ref_dist
            self._save_calibration(ref_dist)
            self._lbl_ref_dist.setText(f"{self._fmt(ref_dist)}  ✓ calibrado")

            aligned, T4, scale = three_point_align(data, pts[0], pts[1], pts[2],
                                                  target_dist=ref_dist)
            try:
                saved = self._save_modified_mesh(aligned, idx, "calibrado")
                self._status_main.setText(
                    f"✓ Calibración guardada — dist P1→P2 ref: {self._fmt(ref_dist)} — {saved}"
                )
            except Exception as save_err:
                self._status_main.setText(
                    f"✓ Calibración aplicada (no pudo guardarse en disco: {save_err})"
                )
            # Mark this scan as the campaign base
            if self._campaign_data:
                self._campaign_data.calibrated_scan_idx = idx

            # Store calibration points transformed to the new coordinate space
            _R = T4[:3, :3].astype(np.float64)
            _t = T4[:3,  3].astype(np.float64)
            def _xpt(p):
                return (scale * (_R @ p.astype(np.float64) + _t)).tolist()
            if self._campaign_data and idx is not None \
                    and idx < len(self._campaign_data.scans):
                self._campaign_data.scans[idx].align_pts = [
                    _xpt(p1), _xpt(p2), _xpt(p3)
                ]
            self._mesh_cache[idx] = aligned
            self._gl.load_mesh(aligned)
            self._show_scan_align_pts(idx)
            self._mark_dirty()
        except Exception as e:
            import traceback
            QMessageBox.critical(self, "Error de calibración",
                                 f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")

    def _on_align_ready(self, pts: list):
        """3 pts picked in ALIGN mode — apply alignment + scale to calibration."""
        from core.alignment import three_point_align
        idx  = self._active_idx
        data = self._pristine_cache.get(idx) if idx is not None else None
        if data is None:
            return
        if self._align_ref_dist is None:
            QMessageBox.information(
                self, "Sin calibración",
                "Primero calibrá la referencia con '📐 Calibrar 3pt'\n"
                "usando los mismos 3 puntos físicos en el mesh de referencia."
            )
            return
        try:
            from core.alignment import umeyama_align

            # Use Umeyama if calibration pts from scan 0 are available:
            # maps the 3 freshly picked raw pts → the 3 calibrated target pts,
            # finding optimal scale + R + t simultaneously (robust to noisy picks).
            # Fall back to single-ratio three_point_align if no calibration pts.
            base_idx = self._base_idx()
            base_scan = (self._campaign_data.scans[base_idx]
                         if self._campaign_data and base_idx is not None
                            and base_idx < len(self._campaign_data.scans)
                         else None)
            base_align_pts = base_scan.align_pts if base_scan else None

            if base_align_pts is not None:
                from core.alignment import refine_pts_to_local_centroid
                from PySide6.QtWidgets import QApplication
                raw_pts = [p.tolist() for p in (pts[0], pts[1], pts[2])]
                src_pts_raw = refine_pts_to_local_centroid(raw_pts, data.vertices)

                # Show original clicks (yellow) vs refined centroids (colored)
                self._gl.show_refinement_preview(raw_pts, src_pts_raw)
                QApplication.processEvents()

                aligned, T4, scale = umeyama_align(data, src_pts_raw, base_align_pts)
            else:
                p1, p2 = pts[0], pts[1]
                current_dist = float(np.linalg.norm((p2 - p1).astype(np.float64)))
                scale = self._align_ref_dist / current_dist
                aligned, T4, scale = three_point_align(
                    data, pts[0], pts[1], pts[2],
                    target_dist=self._align_ref_dist
                )

            try:
                saved = self._save_modified_mesh(aligned, idx, "alineado")
                self._status_main.setText(
                    f"✓ Alineado — escala ×{scale:.4f} — {saved}"
                )
            except Exception as save_err:
                self._status_main.setText(
                    f"✓ Alineado ×{scale:.4f} (no pudo guardarse: {save_err})"
                )
            # Store transformed alignment points
            _R = T4[:3, :3].astype(np.float64)
            _t = T4[:3,  3].astype(np.float64)
            def _xpt(p):
                return (scale * (_R @ p.astype(np.float64) + _t)).tolist()
            if self._campaign_data and idx < len(self._campaign_data.scans):
                self._campaign_data.scans[idx].align_pts = [
                    _xpt(pts[0]), _xpt(pts[1]), _xpt(pts[2])
                ]
            self._mesh_cache[idx] = aligned
            self._gl.load_mesh(aligned)
            self._show_scan_align_pts(idx)
            self._mark_dirty()
            self._refresh_icp_btn(idx)
        except Exception as e:  # noqa: BLE001
            import traceback
            QMessageBox.critical(self, "Error de alineación",
                                 f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")

    def _run_icp(self, near_bolts: bool = True):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        from core.alignment import icp_align, icp_align_near_pts

        idx      = self._active_idx
        base_idx = self._base_idx()
        cur_mesh = self._mesh_cache.get(idx) if idx is not None else None
        ref_mesh = self._base_mesh()
        if cur_mesh is None or ref_mesh is None or idx == base_idx:
            return

        if near_bolts:
            src_pts = (self._campaign_data.scans[idx].align_pts
                       if self._campaign_data and idx < len(self._campaign_data.scans)
                       else None)
            tgt_pts = (self._campaign_data.scans[base_idx].align_pts
                       if self._campaign_data and base_idx is not None
                          and base_idx < len(self._campaign_data.scans)
                       else None)
            patch_radius = cur_mesh.radius * 0.15
            label = "cerca de los tornillos"
        else:
            src_pts = tgt_pts = patch_radius = None
            label = "general"

        self._act_icp.setEnabled(False)
        self._act_icp_full.setEnabled(False)
        self._status_main.setText(f"Refinando con ICP {label}…")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            if near_bolts and src_pts and tgt_pts:
                refined = icp_align_near_pts(cur_mesh, ref_mesh,
                                             src_pts, tgt_pts, patch_radius)
            else:
                refined = icp_align(cur_mesh, ref_mesh, pre_align=False)
            refined.source_path = cur_mesh.source_path
            self._on_icp_done(refined, idx)
        except Exception as e:
            self._on_icp_error(str(e))
        finally:
            QApplication.restoreOverrideCursor()

    def _on_icp_done(self, refined: MeshData, idx: int):
        try:
            saved = self._save_modified_mesh(refined, idx, "alineado")
            self._status_main.setText(f"✓ Alineación refinada con ICP — {saved}")
        except Exception as e:
            self._status_main.setText(f"✓ ICP completado (no pudo guardarse: {e})")
        self._mesh_cache[idx] = refined
        self._gl.load_mesh(refined)
        self._show_scan_align_pts(idx)
        self._mark_dirty()
        self._refresh_icp_btn(idx)

    def _on_icp_error(self, msg: str):
        self._status_main.setText(f"Error en ICP: {msg}")
        self._refresh_icp_btn()

    # ── cylinder crop ────────────────────────────────────────────────────────

    def _on_crop_ready(self, pts: list):
        from core.crop import circumscribed_circle, crop_cylinder
        idx  = self._active_idx
        data = self._mesh_cache.get(idx) if idx is not None else None
        if data is None:
            return
        try:
            center, radius, axis = circumscribed_circle(pts[0], pts[1], pts[2])
        except ValueError as e:
            QMessageBox.warning(self, "Recorte", str(e))
            return

        try:
            cropped = crop_cylinder(data, center, radius, axis)
        except ValueError as e:
            QMessageBox.warning(self, "Recorte", str(e))
            return

        # Show preview
        self._gl.load_mesh(cropped)
        self._raw_radius = cropped.radius
        self._lbl_verts.setText(f"{cropped.vertex_count:,}")
        self._lbl_faces.setText(f"{cropped.face_count:,}")
        self._lbl_radius.setText(self._fmt(cropped.radius))

        eliminadas = data.face_count - cropped.face_count
        msg = (
            f"<b>Vista previa del recorte</b><br><br>"
            f"Radio del círculo: <b>{self._fmt(radius)}</b><br>"
            f"Caras originales: {data.face_count:,}<br>"
            f"Caras resultantes: {cropped.face_count:,}<br>"
            f"Caras eliminadas: {eliminadas:,}<br><br>"
            f"¿Guardar este recorte?"
        )
        r = QMessageBox.question(
            self, "Confirmar recorte", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            try:
                saved = self._save_modified_mesh(cropped, idx, "recortado")
                self._status_main.setText(
                    f"✓ Recorte guardado — radio {self._fmt(radius)}, "
                    f"{cropped.face_count:,} caras — {saved}"
                )
            except Exception as save_err:
                self._status_main.setText(
                    f"✓ Recorte aplicado (no pudo guardarse: {save_err})"
                )
            self._mesh_cache[idx]     = cropped
            self._pristine_cache[idx] = cropped   # crop updates the alignment base
            self._show_scan_align_pts(idx)
            self._mark_dirty()
        else:
            # Discard: restore original
            self._gl.load_mesh(data)
            self._raw_radius = data.radius
            self._lbl_verts.setText(f"{data.vertex_count:,}")
            self._lbl_faces.setText(f"{data.face_count:,}")
            self._lbl_radius.setText(self._fmt(data.radius))
            self._show_scan_align_pts(idx)
            self._status_main.setText("Recorte descartado.")

    # ── measurements ─────────────────────────────────────────────────────────

    def _on_measurement(self, m):
        """Called when a new distance measurement is complete."""
        lines = self._meas_list.text().split("\n") if self._meas_list.text() != "—" else []
        n = len(lines) + 1
        lines.append(f"{n}:  {m.label}")
        self._meas_list.setText("\n".join(lines))
        self._status_main.setText(f"📏 Distancia #{n}: {m.label}")

    def _clear_measurements(self):
        self._gl.clear_measurements()
        self._meas_list.setText("—")

    # ── project save / load ──────────────────────────────────────────────────

    def _new_project(self):
        if self._dirty and self._panel.count() > 0:
            r = QMessageBox.question(
                self, "Nueva campaña",
                "Hay cambios sin guardar. ¿Descartarlos y empezar una nueva?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self._clear_all()

    def _clear_all(self):
        for i in range(self._panel.count() - 1, -1, -1):
            self._panel.remove_scan(i)
        self._mesh_cache.clear()
        self._pristine_cache.clear()
        self._active_idx    = None
        self._project_path  = None
        self._campaign_data = None
        self._dirty         = False
        self.setWindowTitle("Refractory Analyzer")
        self._status_main.setText("Nueva campaña")

    def _save_project(self):
        if self._panel.count() == 0:
            QMessageBox.information(self, "Guardar", "No hay escaneos cargados.")
            return
        if self._project_path is None:
            self._save_project_as()
        else:
            self._do_save(self._project_path)

    def _save_project_as(self):
        if self._panel.count() == 0:
            QMessageBox.information(self, "Guardar", "No hay escaneos cargados.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Guardar campaña", "", PROJECT_FILTER
        )
        if not path:
            return
        if not path.endswith(PROJECT_EXT):
            path += PROJECT_EXT
        self._do_save(path)

    def _do_save(self, path: str):
        from datetime import datetime
        from uuid import uuid4
        count     = self._panel.count()
        mesh_list = [self._mesh_cache[i] for i in range(count)
                     if i in self._mesh_cache]

        if len(mesh_list) != count:
            QMessageBox.warning(self, "Guardar",
                "Algunos escaneos todavía se están cargando. Esperá e intentá de nuevo.")
            return

        self._status_main.setText("Guardando campaña…")
        try:
            proj_name = pathlib.Path(path).stem
            now = datetime.now().isoformat()

            if self._campaign_data is None:
                scans = [
                    ScanMeta(id=str(uuid4()),
                             name=self._panel.get_scan(i).name,
                             source_path=mesh_list[i].source_path,
                             load_date=now)
                    for i in range(count)
                ]
                self._campaign_data = CampaignData(
                    id=str(uuid4()), name=proj_name, scans=scans,
                    meshes=mesh_list, calibration=self._align_ref_dist,
                    start_date=now if scans else None, end_date=None,
                )
            else:
                self._campaign_data.name = proj_name
                self._campaign_data.calibration = self._align_ref_dist
                for i in range(min(count, len(self._campaign_data.scans))):
                    s = self._panel.get_scan(i)
                    if s:
                        self._campaign_data.scans[i].name = s.name
                self._campaign_data.meshes = mesh_list

            save_project(path, self._campaign_data)
            self._project_path = path
            self._dirty        = False
            self.setWindowTitle(f"Refractory Analyzer — {proj_name}")
            if hasattr(self, '_flask_server'):
                self._flask_server.set_output_dir(str(pathlib.Path(path).parent))
            self._status_main.setText(f"✓ Guardado: {path}")
            self._refresh_recent_menu()
        except Exception as e:
            QMessageBox.critical(self, "Error al guardar", str(e))

    def _open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir campaña", "", PROJECT_FILTER
        )
        if path:
            self._open_project_path(path)

    def _open_project_path(self, path: str):
        if self._dirty and self._panel.count() > 0:
            r = QMessageBox.question(
                self, "Abrir campaña",
                "Hay cambios sin guardar. ¿Descartarlos y abrir la campaña?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        self._status_main.setText(f"Cargando {pathlib.Path(path).name}…")
        try:
            proj = load_project(path)
        except Exception as e:
            QMessageBox.critical(self, "Error al abrir", str(e))
            return

        # Clear current state
        for i in range(self._panel.count() - 1, -1, -1):
            self._panel.remove_scan(i)
        self._mesh_cache.clear()
        self._pristine_cache.clear()

        # Restore calibration
        if proj.calibration is not None:
            self._align_ref_dist = proj.calibration
            self._lbl_ref_dist.setText(f"{self._fmt(proj.calibration)}  ✓ calibrado")
            self._save_calibration(proj.calibration)

        # Restore scans
        for i, (scan, mesh) in enumerate(zip(proj.scans, proj.meshes)):
            self._panel.add_scan(scan.name, scan.source_path, load_date=scan.load_date)
            self._mesh_cache[i]     = mesh
            self._pristine_cache[i] = mesh

        self._campaign_data = proj
        self._project_path  = path
        self._dirty         = False
        self.setWindowTitle(f"Refractory Analyzer — {proj.name}")
        self._refresh_recent_menu()

        if proj.meshes:
            self._select_mesh(0)
            self._status_main.setText(
                f"✓ Campaña cargada: {len(proj.scans)} escaneo(s)"
            )

    def closeEvent(self, event):
        if self._dirty and self._panel.count() > 0:
            r = QMessageBox.question(
                self, "Salir",
                "Hay cambios sin guardar. ¿Guardar antes de salir?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel,
            )
            if r == QMessageBox.StandardButton.Save:
                self._save_project()
                event.accept()
            elif r == QMessageBox.StandardButton.Discard:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    def _mark_dirty(self):
        self._dirty = True
        title = self.windowTitle()
        if not title.startswith("*"):
            self.setWindowTitle("* " + title)

    # ── settings ─────────────────────────────────────────────────────────────

    def _open_settings(self):
        from ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        if dlg.exec():
            s = dlg.get_settings()
            if hasattr(self, '_flask_server'):
                self._flask_server.set_work_root(s["work_root"])
                self._flask_server.set_output_dir(s["output_dir"])
            self._status_main.setText("✓ Configuración guardada")

    # ── embedded Flask server ────────────────────────────────────────────────

    def _start_embedded_server(self):
        """Start the capture Flask server in a daemon thread."""
        import sys, threading
        pc_server_path = str(pathlib.Path(__file__).parent.parent / "pc_server")
        if pc_server_path not in sys.path:
            sys.path.insert(0, pc_server_path)

        try:
            import server as flask_server
            from ui.settings_dialog import load_settings
            s = load_settings()

            flask_server.set_work_root(s["work_root"])

            # Output: project dir if open, else saved setting
            out = (str(pathlib.Path(self._project_path).parent)
                   if self._project_path
                   else s["output_dir"])
            flask_server.set_output_dir(out)
            flask_server.set_mesh_ready_callback(self._on_server_mesh_ready)

            self._flask_server = flask_server
            t = threading.Thread(
                target=lambda: flask_server.app.run(
                    host="0.0.0.0", port=5005,
                    debug=False, use_reloader=False
                ),
                daemon=True,
                name="flask-capture-server",
            )
            t.start()
            self._server_thread = t
            self._update_server_status()

        except Exception as e:
            self._server_lbl.setText(f"📡  error: {e}")

    def _update_server_status(self):
        import socket
        try:
            # Get the LAN IP (not loopback)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "127.0.0.1"
        self._server_lbl.setText(f"📡  {ip}:5005")

    def _on_server_mesh_ready(self, path: str, name: str):
        """Called from Flask thread when reconstruction finishes — dispatch to Qt thread."""
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._import_server_mesh(path, name))

    def _import_server_mesh(self, path: str, name: str):
        """Load and add a newly reconstructed mesh to the current project."""
        self._status_main.setText(f"📥  Nuevo escaneo listo: {name} — cargando…")
        self._on_load_requested(path, name)

        # Check if alignment points from the phone are available
        import json
        align_file = pathlib.Path(path).parent / "align_pts.json"
        if align_file.exists():
            try:
                pts_raw = json.loads(align_file.read_text())
                pts = [np.array(p, dtype=np.float32) for p in pts_raw]
                if len(pts) >= 3 and self._align_ref_dist is not None:
                    QTimer.singleShot(800, lambda: self._offer_auto_align(pts, name))
            except Exception:
                pass

    def _offer_auto_align(self, pts: list, name: str):
        """Offer to auto-align the just-imported mesh using phone alignment points."""
        from PySide6.QtCore import QTimer
        r = QMessageBox.question(
            self, "Puntos de alineación detectados",
            f"El escaneo '{name}' incluye 3 puntos de referencia capturados con el celular.\n\n"
            "¿Aplicar alineación automática?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if r == QMessageBox.StandardButton.Yes:
            idx = self._panel.count() - 1   # just-added scan
            data = self._mesh_cache.get(idx)
            if data is not None:
                self._on_align_ready(pts)   # reuse existing alignment flow

    # ── public accessor for comparison dialog ────────────────────────────────

    def get_mesh_data(self, index: int) -> Optional[MeshData]:
        return self._mesh_cache.get(index)
