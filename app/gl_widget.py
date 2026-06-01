from dataclasses import dataclass
from enum import Enum, auto
from typing import List, Optional

import numpy as np
from PySide6.QtCore    import Qt, Signal
from PySide6.QtGui     import QColor, QFont, QPainter, QPen
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from app.camera   import OrbitCamera
from app.renderer import Renderer
from core.loader  import MeshData
from core.picking import ray_cast


class Mode(Enum):
    NAVIGATE      = auto()
    ANNOTATE      = auto()
    ALIGN_3PT     = auto()
    CALIBRATE_3PT = auto()
    MEASURE       = auto()


@dataclass
class Measurement:
    p1:       np.ndarray
    p2:       np.ndarray
    distance: float
    label:    str          # formatted string shown in 3D


class GLWidget(QOpenGLWidget):
    point_picked      = Signal(np.ndarray)
    align_ready       = Signal(list)
    calibrate_ready   = Signal(list)
    measure_done      = Signal(object)   # Measurement
    status_message    = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._camera    = OrbitCamera()
        self._renderer  = Renderer()
        self._mesh_data: Optional[MeshData] = None
        self._ref_data:  Optional[MeshData] = None

        self._mode      = Mode.NAVIGATE
        self._heatmap   = False

        self._last_mouse = None
        self._mouse_btn  = None
        self._align_pts: List[np.ndarray] = []

        # Measurement state
        self._meas_pending: Optional[np.ndarray] = None  # first point waiting
        self._measurements: List[Measurement]    = []

        self.setMinimumSize(400, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ── public API ──────────────────────────────────────────────────────────

    def load_mesh(self, mesh_data: MeshData):
        self._mesh_data  = mesh_data
        self._heatmap    = False
        self._align_pts  = []
        self.makeCurrent()
        self._renderer.load_mesh(mesh_data)
        self._renderer.update_markers(np.empty((0, 3), np.float32),
                                      np.empty((0, 4), np.float32))
        self.doneCurrent()
        # Fit camera to the combined bounding sphere
        self._fit_combined()
        self.update()
        self.status_message.emit(
            f"{mesh_data.vertex_count:,} vértices  |  "
            f"{mesh_data.face_count:,} caras  |  "
            f"radio {mesh_data.radius:.2f}"
        )

    def set_reference_mesh(self, ref_data: MeshData | None):
        self._ref_data = ref_data
        self.makeCurrent()
        if ref_data is not None:
            self._renderer.load_reference(ref_data)
        else:
            self._renderer.clear_reference()
        self.doneCurrent()
        self._fit_combined()
        self.update()

    def set_clip_planes(self, h_frac: float, v_frac: float):
        """h_frac / v_frac in [0,1]. 1.0 = no cut, 0.0 = cut everything."""
        meshes = [m for m in (self._mesh_data, self._ref_data) if m is not None]
        if not meshes:
            return

        all_v = np.concatenate([m.vertices for m in meshes], axis=0)
        ymin, ymax = float(all_v[:, 1].min()), float(all_v[:, 1].max())
        xmin, xmax = float(all_v[:, 0].min()), float(all_v[:, 0].max())
        pad_y = (ymax - ymin) * 0.01 + 1e-6
        pad_x = (xmax - xmin) * 0.01 + 1e-6

        y_cut = (ymin - pad_y) + h_frac * (ymax - ymin + 2 * pad_y)
        x_cut = (xmin - pad_x) + v_frac * (xmax - xmin + 2 * pad_x)

        # Plane (a,b,c,d): dot >= 0 → keep
        # Horizontal: keep y <= y_cut  → clip dist = y_cut - y = (0,-1,0,y_cut)·pos
        # Vertical:   keep x <= x_cut  → clip dist = x_cut - x = (-1,0,0,x_cut)·pos
        clip_h = np.array([0.0, -1.0, 0.0,  y_cut], dtype=np.float32)
        clip_v = np.array([-1.0, 0.0, 0.0,  x_cut], dtype=np.float32)

        self.makeCurrent()
        self._renderer.set_clip_planes(clip_h, clip_v)
        self.doneCurrent()
        self.update()

    def _fit_combined(self):
        meshes = [m for m in (self._mesh_data, self._ref_data) if m is not None]
        if not meshes:
            return
        if len(meshes) == 1:
            self._camera.fit(meshes[0].centroid, meshes[0].radius)
            return
        all_v    = np.concatenate([m.vertices for m in meshes], axis=0)
        centroid = all_v.mean(axis=0)
        radius   = float(np.max(np.linalg.norm(all_v - centroid, axis=1)))
        self._camera.fit(centroid, max(radius, 1e-6))

    def clear_measurements(self):
        self._measurements.clear()
        self._meas_pending = None
        self._refresh_measure_render()

    def apply_heatmap(self, colors: np.ndarray):
        self._heatmap = True
        self.makeCurrent()
        self._renderer.update_colors(colors)
        self.doneCurrent()
        self.update()

    def clear_heatmap(self):
        self._heatmap = False
        self.makeCurrent()
        self._renderer.reset_colors()
        self.doneCurrent()
        self.update()

    def set_mode(self, mode: Mode):
        self._mode       = mode
        self._align_pts  = []
        self._meas_pending = None
        self._update_markers()
        self._refresh_measure_render()
        msgs = {
            Mode.NAVIGATE:      "Modo navegación",
            Mode.ANNOTATE:      "Modo anotación — clic en la malla para marcar",
            Mode.ALIGN_3PT:     "Alinear 3 puntos — seleccioná punto 1/3",
            Mode.CALIBRATE_3PT: "CALIBRAR — seleccioná punto de referencia 1/3",
            Mode.MEASURE:       "Medir distancia — clic en el primer punto",
        }
        self.setCursor(
            Qt.CursorShape.ArrowCursor if mode == Mode.NAVIGATE
            else Qt.CursorShape.CrossCursor
        )
        self.status_message.emit(msgs.get(mode, ""))

    def toggle_wireframe(self, enabled: bool):
        self._renderer.set_wireframe(enabled)
        self.update()

    def fit_view(self):
        if self._mesh_data:
            self._camera.fit(self._mesh_data.centroid, self._mesh_data.radius)
            self.update()

    def reset_alignment(self):
        """Reload original mesh vertices."""
        if self._mesh_data:
            self.load_mesh(self._mesh_data)

    # ── OpenGL ──────────────────────────────────────────────────────────────

    def initializeGL(self):
        fmt = self.context().format()
        if fmt.majorVersion() < 3:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "OpenGL requerido",
                f"Se necesita OpenGL 3.3, versión detectada: "
                f"{fmt.majorVersion()}.{fmt.minorVersion()}")
        self._renderer.initialize()

    def resizeGL(self, w: int, h: int):
        from OpenGL import GL
        GL.glViewport(0, 0, w, h)

    def paintGL(self):
        aspect = self.width() / max(self.height(), 1)
        mvp    = self._camera.get_mvp(aspect)
        cam_p  = self._camera.position.astype(np.float32)
        self._renderer.draw(mvp, cam_p, use_vcolor=self._heatmap)

    def paintEvent(self, event):
        """OpenGL first (via super), then QPainter overlay for measurement labels."""
        super().paintEvent(event)
        if not self._measurements and self._meas_pending is None:
            return

        aspect = self.width() / max(self.height(), 1)
        mvp    = self._camera.get_mvp(aspect)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont("Arial", 10, QFont.Weight.Bold)
        painter.setFont(font)

        # Label for each completed measurement at its midpoint
        for m in self._measurements:
            sx, sy, vis = self._world_to_screen((m.p1 + m.p2) / 2, mvp)
            if vis:
                tw = 72
                painter.fillRect(sx - tw//2, sy - 16, tw, 18,
                                 QColor(0, 0, 0, 180))
                painter.setPen(QColor(255, 230, 0))
                painter.drawText(sx - tw//2 + 3, sy - 2, m.label)

        # Label for the pending first point
        if self._meas_pending is not None:
            sx, sy, vis = self._world_to_screen(self._meas_pending, mvp)
            if vis:
                painter.fillRect(sx + 8, sy - 16, 80, 18, QColor(0, 0, 0, 160))
                painter.setPen(QColor(0, 220, 255))
                painter.drawText(sx + 10, sy - 2, "Pto. 1 — esperando Pto. 2")

        painter.end()

    # ── mouse ────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        self._last_mouse = event.position()
        self._mouse_btn  = event.button()

        if event.button() == Qt.MouseButton.LeftButton:
            if self._mode in (Mode.ANNOTATE, Mode.ALIGN_3PT,
                              Mode.CALIBRATE_3PT, Mode.MEASURE):
                self._handle_pick(event.position())

    def mouseMoveEvent(self, event):
        if self._last_mouse is None:
            return
        dx = event.position().x() - self._last_mouse.x()
        dy = event.position().y() - self._last_mouse.y()
        self._last_mouse = event.position()

        btn = event.buttons()
        if btn & Qt.MouseButton.LeftButton and self._mode == Mode.NAVIGATE:
            self._camera.orbit(dx, dy)
            self.update()
        elif btn & Qt.MouseButton.RightButton or btn & Qt.MouseButton.MiddleButton:
            self._camera.pan(dx, dy, self.height())
            self.update()

    def mouseReleaseEvent(self, event):
        self._last_mouse = None
        self._mouse_btn  = None

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        self._camera.zoom(delta)
        self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.fit_view()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_F:
            self.fit_view()
        elif key == Qt.Key.Key_Escape:
            self.set_mode(Mode.NAVIGATE)

    # ── picking ───────────────────────────────────────────────────────────────

    def _pick_nearest(self, origin, direction) -> Optional[np.ndarray]:
        """Ray cast against both meshes; return nearest hit point or None."""
        best_dist = np.inf
        best_pt   = None
        for mesh in (self._mesh_data, self._ref_data):
            if mesh is None:
                continue
            r = ray_cast(origin, direction, mesh)
            if r is not None and r.distance < best_dist:
                best_dist = r.distance
                best_pt   = r.hit_point
        return best_pt

    def _handle_pick(self, pos):
        if self._mesh_data is None:
            return
        w, h = self.width(), self.height()
        origin, direction = self._camera.get_ray(pos.x(), pos.y(), w, h)

        hit = self._pick_nearest(origin, direction)
        if hit is None:
            return

        if self._mode == Mode.ANNOTATE:
            self.point_picked.emit(hit)

        elif self._mode == Mode.MEASURE:
            if self._meas_pending is None:
                # First point selected
                self._meas_pending = hit
                self._refresh_measure_render()
                self.status_message.emit("Pto. 1 seleccionado — clic en el segundo punto")
            else:
                # Second point — compute distance
                p1   = self._meas_pending
                p2   = hit
                dist = float(np.linalg.norm(p2 - p1))
                label = f"{dist:.4f}"
                m = Measurement(p1=p1, p2=p2, distance=dist, label=label)
                self._measurements.append(m)
                self._meas_pending = None
                self._refresh_measure_render()
                self.measure_done.emit(m)
                self.status_message.emit(
                    f"Distancia: {label}  — clic para otra medición, Esc para salir")

        elif self._mode in (Mode.ALIGN_3PT, Mode.CALIBRATE_3PT):
            is_calib = (self._mode == Mode.CALIBRATE_3PT)
            self._align_pts.append(hit)
            self._update_markers()
            n = len(self._align_pts)
            prefix = "CALIBRAR" if is_calib else "Alinear"
            if n < 3:
                self.status_message.emit(
                    f"{prefix} — seleccioná punto {n+1}/3")
            else:
                self.status_message.emit(
                    f"3 puntos — {'guardando calibración' if is_calib else 'aplicando alineación'}…")
                pts = self._align_pts.copy()
                self._align_pts = []
                self._update_markers()
                self.set_mode(Mode.NAVIGATE)
                if is_calib:
                    self.calibrate_ready.emit(pts)
                else:
                    self.align_ready.emit(pts)

    def _update_markers(self):
        pts = self._align_pts
        if not pts:
            self.makeCurrent()
            self._renderer.update_markers(
                np.empty((0, 3), np.float32),
                np.empty((0, 4), np.float32))
            self.doneCurrent()
            self.update()
            return

        colors_map = [
            [1.0, 0.2, 0.2, 1.0],  # red   – pt 1
            [0.2, 1.0, 0.2, 1.0],  # green – pt 2
            [0.2, 0.5, 1.0, 1.0],  # blue  – pt 3
        ]
        positions = np.array(pts, dtype=np.float32)
        colors    = np.array([colors_map[i] for i in range(len(pts))],
                              dtype=np.float32)
        self.makeCurrent()
        self._renderer.update_markers(positions, colors)
        self.doneCurrent()
        self.update()

    # ── measurement rendering ──────────────────────────────────────────────

    def _refresh_measure_render(self):
        """Rebuild GPU lines + markers from self._measurements and pending point."""
        _YELLOW = [1.0, 0.85, 0.0, 1.0]
        _CYAN   = [0.0, 0.85, 1.0, 1.0]

        seg_pts, seg_col = [], []
        mk_pts,  mk_col  = [], []

        for m in self._measurements:
            seg_pts.extend([m.p1, m.p2])
            seg_col.extend([_YELLOW, _YELLOW])
            mk_pts.extend([m.p1, m.p2])
            mk_col.extend([_YELLOW, _YELLOW])

        if self._meas_pending is not None:
            mk_pts.append(self._meas_pending)
            mk_col.append(_CYAN)

        def _arr(lst): return np.array(lst, dtype=np.float32) if lst \
                               else np.empty((0, 3 if any(len(x)==3 for x in (lst or [[0,0,0]])) else 4), dtype=np.float32)

        segs = np.array(seg_pts, dtype=np.float32) if seg_pts else np.empty((0, 3), np.float32)
        scol = np.array(seg_col, dtype=np.float32) if seg_col else np.empty((0, 4), np.float32)
        mpts = np.array(mk_pts,  dtype=np.float32) if mk_pts  else np.empty((0, 3), np.float32)
        mcol = np.array(mk_col,  dtype=np.float32) if mk_col  else np.empty((0, 4), np.float32)

        self.makeCurrent()
        self._renderer.update_measurements(segs, scol, mpts, mcol)
        self.doneCurrent()
        self.update()

    # ── world → screen projection ──────────────────────────────────────────

    def _world_to_screen(self, world_pt: np.ndarray, mvp: np.ndarray):
        """Return (sx, sy, visible) for a world-space point."""
        v = mvp.astype(np.float64) @ np.array(
            [world_pt[0], world_pt[1], world_pt[2], 1.0], dtype=np.float64)
        if v[3] <= 0:
            return 0, 0, False
        ndc = v[:3] / v[3]
        if abs(ndc[0]) > 1.05 or abs(ndc[1]) > 1.05 or ndc[2] < -1 or ndc[2] > 1:
            return 0, 0, False
        sx = int((ndc[0] + 1) / 2 * self.width())
        sy = int((1 - ndc[1]) / 2 * self.height())
        return sx, sy, True
