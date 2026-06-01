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
                             PROJECT_FILTER, PROJECT_EXT)


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
            self.done.emit(data, self.name)
        except Exception as e:
            self.error.emit(str(e))


# ── main window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Refractory Analyzer")
        self.resize(1300, 800)

        self._mesh_cache: Dict[int, MeshData] = {}
        self._active_idx: Optional[int]        = None
        self._load_thread: Optional[QThread]   = None
        self._align_ref_dist: Optional[float]  = None
        self._project_path: Optional[str]       = None   # current .refproj path
        self._dirty: bool                       = False  # unsaved changes

        self._calib_file = pathlib.Path.home() / ".refractory_calibration.json"

        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._build_status()
        self._load_calibration()

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

        # Right panel
        right = self._build_right_panel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._gl)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([900, 260])
        root.addWidget(splitter)

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(260)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(6, 6, 6, 6)

        title = QLabel("Propiedades")
        title.setFont(QFont("", 10, QFont.Weight.Bold))
        layout.addWidget(title)

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

        layout.addStretch()
        return w

    def _build_menu(self):
        mb = self.menuBar()

        # ── File ──
        file_menu = mb.addMenu("Archivo")

        act_new = QAction("Nuevo proyecto", self)
        act_new.setShortcut("Ctrl+N")
        act_new.triggered.connect(self._new_project)
        file_menu.addAction(act_new)

        act_open = QAction("Abrir proyecto…", self)
        act_open.setShortcut("Ctrl+O")
        act_open.triggered.connect(self._open_project)
        file_menu.addAction(act_open)

        # Recent submenu
        self._recent_menu = file_menu.addMenu("Recientes")
        self._refresh_recent_menu()

        file_menu.addSeparator()

        act_save = QAction("Guardar proyecto", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(self._save_project)
        file_menu.addAction(act_save)

        act_saveas = QAction("Guardar proyecto como…", self)
        act_saveas.setShortcut("Ctrl+Shift+S")
        act_saveas.triggered.connect(self._save_project_as)
        file_menu.addAction(act_saveas)

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

        self._act_fit   = act("⊡ Encuadrar",    "F",  tip="Centrar vista en la malla")
        self._act_wire  = act("⬡ Contorno",      "W",  checkable=True, tip="Vista de contorno")
        self._act_nav   = act("↖ Navegar",       None, checkable=True, tip="Modo navegación")
        self._act_calib = act("📐 Calibrar 3pt", None, checkable=True,
                              tip="Seleccioná 3 puntos de referencia — se guardan como calibración")
        self._act_align = act("△ Alinear 3pt",   None, checkable=True,
                              tip="Alinear esta malla usando los 3 puntos calibrados")
        self._act_hmap  = act("⬛ Quitar mapa",   None, tip="Volver a color base")

        self._act_nav.setChecked(True)

        tb.addAction(self._act_fit)
        tb.addSeparator()
        tb.addAction(self._act_wire)
        tb.addSeparator()
        tb.addAction(self._act_nav)
        tb.addAction(self._act_calib)
        tb.addAction(self._act_align)
        tb.addSeparator()
        tb.addAction(self._act_hmap)

        self._act_fit.triggered.connect(self._gl.fit_view)
        self._act_wire.toggled.connect(self._gl.toggle_wireframe)
        self._act_nav.triggered.connect(lambda: self._set_mode(Mode.NAVIGATE))
        self._act_calib.triggered.connect(lambda: self._set_mode(Mode.CALIBRATE_3PT))
        self._act_align.triggered.connect(lambda: self._set_mode(Mode.ALIGN_3PT))
        self._act_hmap.triggered.connect(self._clear_heatmap)

    def _build_status(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status_main = QLabel("Sin malla cargada")
        sb.addWidget(self._status_main, 1)
        self._gl.status_message.connect(self._status_main.setText)

    # ── mode helpers ─────────────────────────────────────────────────────────

    def _set_mode(self, mode: Mode):
        self._act_nav.setChecked(mode == Mode.NAVIGATE)
        self._act_calib.setChecked(mode == Mode.CALIBRATE_3PT)
        self._act_align.setChecked(mode == Mode.ALIGN_3PT)
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
        idx = self._panel.count()  # index before adding
        self._panel.add_campaign(name, data.source_path)
        self._mesh_cache[idx] = data
        self._select_mesh(idx)
        self._mark_dirty()

    def _on_load_error(self, msg: str):
        QMessageBox.critical(self, "Error al cargar", msg)
        self._status_main.setText("Error al cargar malla.")

    def _on_select(self, index: int):
        self._select_mesh(index)

    def _on_remove(self, index: int):
        self._mesh_cache.pop(index, None)
        # Re-index cache keys above the removed index
        new_cache = {}
        for k, v in self._mesh_cache.items():
            if k < index:
                new_cache[k] = v
            elif k > index:
                new_cache[k - 1] = v
        self._mesh_cache = new_cache
        self._panel.remove_campaign(index)

        if self._active_idx == index:
            self._active_idx = None
            if self._panel.count() > 0:
                self._select_mesh(min(index, self._panel.count() - 1))

    def _on_compare(self, ref: int, cur: int):
        pass  # comparison is handled directly by ComparisonDialog

    # ── mesh selection ───────────────────────────────────────────────────────

    def _select_mesh(self, index: int):
        self._active_idx = index
        data = self._mesh_cache.get(index)
        if data is None:
            return
        self._gl.load_mesh(data)
        self._gl.set_reference_mesh(None)   # clear reference when selecting alone
        self._lbl_verts.setText(f"{data.vertex_count:,}")
        self._lbl_faces.setText(f"{data.face_count:,}")
        self._lbl_radius.setText(f"{data.radius:.3f}")
        self._lbl_max.setText("—")
        self._lbl_mean.setText("—")
        self._lbl_p95.setText("—")

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

        self._lbl_verts.setText(f"{cur_data.vertex_count:,}")
        self._lbl_faces.setText(f"{cur_data.face_count:,}")
        self._lbl_radius.setText(f"{cur_data.radius:.3f}")
        self._lbl_max.setText(f"{result.max_wear:.3f}")
        self._lbl_mean.setText(f"{result.mean_wear:.3f}")
        self._lbl_p95.setText(f"{result.p95_wear:.3f}")

        self._current_wear_result   = result
        self._current_wear_cmap     = self._cbo_cmap.currentText()
        self._current_wear_mesh_idx = cur_idx
        self._update_colorbar()

        # Re-apply current slider positions
        self._update_clips()

    def show_heatmap(self, mesh_idx: int, colors: np.ndarray, result):
        self._select_mesh(mesh_idx)
        self._gl.apply_heatmap(colors)
        self._lbl_max.setText(f"{result.max_wear:.3f}")
        self._lbl_mean.setText(f"{result.mean_wear:.3f}")
        self._lbl_p95.setText(f"{result.p95_wear:.3f}")
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
            colors = distances_to_colors(
                self._current_wear_result.distances,
                colormap=cmap_name,
            )
            self._current_wear_cmap = cmap_name
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
                        f"{dist:.6f}  ✓ calibrado"
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
        """3 pts picked in CALIBRATE mode — save as reference, align this mesh."""
        from core.alignment import three_point_align
        idx  = self._active_idx
        data = self._mesh_cache.get(idx) if idx is not None else None
        if data is None:
            return
        try:
            p1, p2 = pts[0], pts[1]
            ref_dist = float(np.linalg.norm((p2 - p1).astype(np.float64)))

            self._align_ref_dist = ref_dist
            self._save_calibration(ref_dist)
            self._lbl_ref_dist.setText(f"{ref_dist:.6f}  ✓ calibrado")

            aligned = three_point_align(data, pts[0], pts[1], pts[2])
            self._mesh_cache[idx] = aligned
            self._gl.load_mesh(aligned)
            self._mark_dirty()
            self._status_main.setText(
                f"✓ Calibración guardada — dist P1→P2: {ref_dist:.6f}"
            )
        except ValueError as e:
            QMessageBox.warning(self, "Calibración", str(e))

    def _on_align_ready(self, pts: list):
        """3 pts picked in ALIGN mode — apply alignment + scale to calibration."""
        from core.alignment import three_point_align
        idx  = self._active_idx
        data = self._mesh_cache.get(idx) if idx is not None else None
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
            p1, p2 = pts[0], pts[1]
            current_dist = float(np.linalg.norm((p2 - p1).astype(np.float64)))
            scale_factor = self._align_ref_dist / current_dist

            aligned = three_point_align(
                data, pts[0], pts[1], pts[2],
                target_dist=self._align_ref_dist
            )
            self._mesh_cache[idx] = aligned
            self._gl.load_mesh(aligned)
            self._mark_dirty()
            self._status_main.setText(
                f"✓ Alineado y escalado ×{scale_factor:.4f}"
            )
        except ValueError as e:
            QMessageBox.warning(self, "Alineación", str(e))

    # ── project save / load ──────────────────────────────────────────────────

    def _new_project(self):
        if self._dirty and self._panel.count() > 0:
            r = QMessageBox.question(
                self, "Nuevo proyecto",
                "Hay cambios sin guardar. ¿Descartarlos y empezar uno nuevo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self._clear_all()

    def _clear_all(self):
        for i in range(self._panel.count() - 1, -1, -1):
            self._panel.remove_campaign(i)
        self._mesh_cache.clear()
        self._active_idx   = None
        self._project_path = None
        self._dirty        = False
        self.setWindowTitle("Refractory Analyzer")
        self._status_main.setText("Proyecto nuevo")

    def _save_project(self):
        if self._panel.count() == 0:
            QMessageBox.information(self, "Guardar", "No hay campañas cargadas.")
            return
        if self._project_path is None:
            self._save_project_as()
        else:
            self._do_save(self._project_path)

    def _save_project_as(self):
        if self._panel.count() == 0:
            QMessageBox.information(self, "Guardar", "No hay campañas cargadas.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Guardar proyecto", "", PROJECT_FILTER
        )
        if not path:
            return
        if not path.endswith(PROJECT_EXT):
            path += PROJECT_EXT
        self._do_save(path)

    def _do_save(self, path: str):
        names     = [self._panel.get_campaign(i).name
                     for i in range(self._panel.count())]
        mesh_list = [self._mesh_cache[i]
                     for i in range(self._panel.count())
                     if i in self._mesh_cache]

        if len(mesh_list) != len(names):
            QMessageBox.warning(self, "Guardar",
                "Algunas mallas todavía se están cargando. Esperá e intentá de nuevo.")
            return

        self._status_main.setText("Guardando proyecto…")
        try:
            proj_name = pathlib.Path(path).stem
            save_project(
                path, names, mesh_list,
                calibration   = self._align_ref_dist,
                project_name  = proj_name,
            )
            self._project_path = path
            self._dirty        = False
            self.setWindowTitle(f"Refractory Analyzer — {proj_name}")
            self._status_main.setText(f"✓ Guardado: {path}")
            self._refresh_recent_menu()
        except Exception as e:
            QMessageBox.critical(self, "Error al guardar", str(e))

    def _open_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir proyecto", "", PROJECT_FILTER
        )
        if path:
            self._open_project_path(path)

    def _open_project_path(self, path: str):
        if self._dirty and self._panel.count() > 0:
            r = QMessageBox.question(
                self, "Abrir proyecto",
                "Hay cambios sin guardar. ¿Descartarlos y abrir el proyecto?",
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
            self._panel.remove_campaign(i)
        self._mesh_cache.clear()

        # Restore calibration
        if proj.calibration is not None:
            self._align_ref_dist = proj.calibration
            self._lbl_ref_dist.setText(f"{proj.calibration:.6f}  ✓ calibrado")
            self._save_calibration(proj.calibration)

        # Restore campaigns
        for i, (meta, mesh) in enumerate(zip(proj.campaigns, proj.meshes)):
            self._panel.add_campaign(meta.name, meta.source_path)
            self._mesh_cache[i] = mesh

        self._project_path = path
        self._dirty        = False
        self.setWindowTitle(f"Refractory Analyzer — {proj.name}")
        self._refresh_recent_menu()

        if proj.meshes:
            self._select_mesh(0)
            self._status_main.setText(
                f"✓ Proyecto cargado: {len(proj.campaigns)} campaña(s)"
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

    # ── public accessor for comparison dialog ────────────────────────────────

    def get_mesh_data(self, index: int) -> Optional[MeshData]:
        return self._mesh_cache.get(index)
