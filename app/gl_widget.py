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
    NAVIGATE        = auto()
    ANNOTATE        = auto()
    ALIGN_3PT       = auto()
    CALIBRATE_3PT   = auto()
    MEASURE         = auto()
    CROP_CYLINDER   = auto()
    MEASURE_DIAM    = auto()
    ERASE           = auto()
    MEASURE_DEPTH   = auto()
    COMPARE_RADIAL  = auto()


@dataclass
class Measurement:
    p1:       np.ndarray
    p2:       np.ndarray
    distance: float
    label:    str          # formatted string shown in 3D


@dataclass
class DiameterMeasurement:
    height_point: np.ndarray   # clicked point on mesh
    center:       np.ndarray   # cross-section centroid (3D)
    spokes:       list         # [(p1_3d, p2_3d, diam_value, label_str)] × N_DIRS
    max_idx:      int          # index into spokes of the longest diameter
    label:        str          # formatted label of the longest diameter
    ring_pts:     np.ndarray   # (N, 3) fitted circle polygon for reference
    plane_verts:  np.ndarray   # (6, 3) two triangles forming the cut plane quad


@dataclass
class DepthMeasurement:
    rim_pts:   list          # [p1, p2, p3] — define the reference plane (top of crucible)
    bottom_pt: np.ndarray   # 4th clicked point (bottom of crucible)
    foot_pt:   np.ndarray   # perpendicular projection of bottom_pt onto rim plane
    normal:    np.ndarray   # unit normal of the rim plane
    depth:     float        # perpendicular distance (model units)
    label:     str          # formatted string


@dataclass
class RadialScan:
    """Radial cross-section comparison between two meshes at a given height."""
    center:     np.ndarray              # (3,) scan origin (crucible axis at scan height)
    angles_deg: List[float]             # 7 angles evenly distributed 0–360°
    hits_a:     List[Optional[np.ndarray]]  # (7,) hits on main mesh
    hits_b:     List[Optional[np.ndarray]]  # (7,) hits on ref mesh
    dists_a:    List[float]             # distances center→A per direction
    dists_b:    List[float]             # distances center→B per direction
    gaps:       List[float]             # |dist_a - dist_b| per direction


@dataclass
class ProfileScan:
    """Horizontal line measurements at N Y-heights within an X cut plane.

    Each line is parallel to the alignment plane (horizontal) and lies inside
    the x=x_cut cross-section plane.  Directions: ±Z.
    """
    center:       np.ndarray                    # (3,) centre (x_cut, cy, cz)
    x_cut:        float                         # world X position of the cut
    heights:      List[float]                   # Y positions of the N lines
    hits_a_start: List[Optional[np.ndarray]]   # −Z wall hits on mesh A
    hits_a_end:   List[Optional[np.ndarray]]   # +Z wall hits on mesh A
    hits_b_start: List[Optional[np.ndarray]]   # −Z wall hits on ref mesh (None if absent)
    hits_b_end:   List[Optional[np.ndarray]]   # +Z wall hits on ref mesh
    widths_a:     List[float]                  # Z-span of mesh A per height
    widths_b:     List[float]                  # Z-span of ref mesh per height (0 if absent)
    gaps_left:    List[float]                  # |z_start_a − z_start_b| (−Z / left wall)
    gaps_right:   List[float]                  # |z_end_a   − z_end_b|   (+Z / right wall)


class GLWidget(QOpenGLWidget):
    point_picked      = Signal(np.ndarray)
    align_ready       = Signal(list)
    calibrate_ready   = Signal(list)
    measure_done      = Signal(object)   # Measurement
    diameter_done     = Signal(object)   # DiameterMeasurement
    crop_ready        = Signal(list)     # 3 points for cylinder crop
    faces_erased      = Signal(object)   # MeshData after face deletion
    erase_updated     = Signal(int)      # count of pending faces to delete
    depth_done        = Signal(object)   # DepthMeasurement
    radial_scan_done  = Signal(object)   # RadialScan
    profile_scan_done = Signal(object)   # ProfileScan
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

        self._unit_factor   = 1.0
        self._unit_suffix   = "mm"
        self._unit_decimals = 3
        self._align_pts: List[np.ndarray] = []

        # Measurement state
        self._meas_pending: Optional[np.ndarray] = None  # first point waiting
        self._measurements: List[Measurement]          = []
        self._diam_measurements: List[DiameterMeasurement] = []
        self._slider_diams:  List[DiameterMeasurement] = []  # live diameters from sliders
        self._depth_pending: List[np.ndarray]          = []  # pts accumulating for depth mode
        self._depth_measurements: List[DepthMeasurement] = []

        # Radial comparison state (Y slider)
        self._radial_scan:      Optional[RadialScan]  = None
        self._radial_show_wear: bool                  = False  # False=from center, True=between

        # Profile scan state (X slider)
        self._profile_scan: Optional[ProfileScan] = None

        # Erase state
        self._erase_pending: set           = set()    # face indices to delete
        self._erase_brush_frac: float      = 0.05    # brush radius as fraction of mesh radius
        self._face_centroids: Optional[np.ndarray] = None
        self._face_neighbors: Optional[list]       = None  # list[list[int]] edge-adjacency

        self.setMinimumSize(400, 300)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ── public API ──────────────────────────────────────────────────────────

    def load_mesh(self, mesh_data: MeshData):
        self._mesh_data  = mesh_data
        self._heatmap    = False
        self._align_pts  = []
        self._erase_pending.clear()
        # Precompute face centroids and edge-adjacency for erase mode
        if mesh_data.faces is not None and len(mesh_data.faces):
            vf = mesh_data.vertices[mesh_data.faces]   # (F, 3, 3)
            self._face_centroids = vf.mean(axis=1)     # (F, 3) float32
            self._face_neighbors = self._compute_face_adjacency(mesh_data.faces)
        else:
            self._face_centroids = None
            self._face_neighbors = None
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

    def set_slider_diameter(self, h_frac: Optional[float], v_frac: Optional[float]):
        """Compute live diameter measurements at the current clip-plane heights.

        Pass None for either axis to disable measurement on that slider.
        Mirrors the same world-coordinate math as set_clip_planes.
        """
        self._slider_diams.clear()

        mesh = self._mesh_data
        if mesh is None or mesh.faces is None:
            self._refresh_measure_render()
            return

        all_v = mesh.vertices
        ymin, ymax = float(all_v[:, 1].min()), float(all_v[:, 1].max())
        xmin, xmax = float(all_v[:, 0].min()), float(all_v[:, 0].max())
        cx = float(mesh.centroid[0])
        cy = float(mesh.centroid[1])
        cz = float(mesh.centroid[2])

        if h_frac is not None:
            pad_y = (ymax - ymin) * 0.01 + 1e-6
            y_cut = (ymin - pad_y) + h_frac * (ymax - ymin + 2 * pad_y)
            if ymin < y_cut < ymax:
                pt = np.array([cx, y_cut, cz], dtype=np.float32)
                dm = self._compute_diameter_at(pt)
                if dm is not None:
                    self._slider_diams.append(dm)

        if v_frac is not None:
            pad_x = (xmax - xmin) * 0.01 + 1e-6
            x_cut = (xmin - pad_x) + v_frac * (xmax - xmin + 2 * pad_x)
            if xmin < x_cut < xmax:
                pt = np.array([x_cut, cy, cz], dtype=np.float32)
                dm = self._compute_diameter_at(pt)
                if dm is not None:
                    self._slider_diams.append(dm)

        self._refresh_measure_render()

    # ── radial comparison ────────────────────────────────────────────────────

    def update_radial_from_slider(self, h_frac: float):
        """Recompute radial scan at the Y height of the horizontal clip slider.

        Fires whenever two meshes are loaded, regardless of mode.
        Uses the same coordinate math as set_clip_planes so the scan plane
        aligns exactly with the visible cut.
        """
        if self._mesh_data is None or self._ref_data is None:
            return
        all_v = np.concatenate([self._mesh_data.vertices, self._ref_data.vertices], axis=0)
        ymin, ymax = float(all_v[:, 1].min()), float(all_v[:, 1].max())
        pad_y = (ymax - ymin) * 0.01 + 1e-6
        y_cut = float((ymin - pad_y) + h_frac * (ymax - ymin + 2 * pad_y))
        # Clamp so horizontal rays can actually hit the mesh
        margin = (ymax - ymin) * 0.03
        y_cut  = float(np.clip(y_cut, ymin + margin, ymax - margin))
        if self._radial_scan is not None:
            center = self._radial_scan.center.copy()
        else:
            ca = self._mesh_data.centroid.astype(np.float64)
            cb = self._ref_data.centroid.astype(np.float64)
            center = ((ca + cb) / 2).astype(np.float32)
        center[1] = y_cut
        self._run_radial_scan(center)

    def update_profile_from_slider(self, v_frac: float):
        """Compute horizontal profile scan at the X cut position of the vertical slider.

        Measures horizontal lines (parallel to the alignment plane, i.e. in Z direction)
        at N evenly-spaced heights inside the x=x_cut cross-section plane.
        Works with a single mesh; shows gap data when a reference is also loaded.
        """
        if self._mesh_data is None or self._mesh_data.faces is None:
            return
        meshes = [m for m in (self._mesh_data, self._ref_data) if m is not None]
        all_v = np.concatenate([m.vertices for m in meshes], axis=0)
        xmin, xmax = float(all_v[:, 0].min()), float(all_v[:, 0].max())
        pad_x = (xmax - xmin) * 0.01 + 1e-6
        x_cut = float((xmin - pad_x) + v_frac * (xmax - xmin + 2 * pad_x))
        margin = (xmax - xmin) * 0.03
        x_cut = float(np.clip(x_cut, xmin + margin, xmax - margin))
        scan = self._compute_profile_scan(x_cut)
        if scan is None:
            return
        self._profile_scan = scan
        self._refresh_measure_render()
        self.profile_scan_done.emit(scan)

    def _compute_profile_scan(self, x_cut: float) -> 'Optional[ProfileScan]':
        """Slice both meshes at x=x_cut and measure Z-direction width at N Y-heights.

        Uses trimesh's cross-section and a scanline intersection to find the
        horizontal (Z-direction) extents of each mesh profile at N evenly-spaced
        Y heights inside the cut plane.
        """
        import trimesh as _trimesh

        meshes = [m for m in (self._mesh_data, self._ref_data) if m is not None]
        if not meshes:
            return None

        all_v = np.concatenate([m.vertices for m in meshes], axis=0)
        ymin, ymax = float(all_v[:, 1].min()), float(all_v[:, 1].max())
        center_y = float((ymin + ymax) / 2)
        center_z = float(all_v[:, 2].mean())
        center = np.array([x_cut, center_y, center_z], dtype=np.float32)

        N = 7
        margin_y = (ymax - ymin) * 0.08
        y_range = ymax - ymin - 2 * margin_y
        heights = [ymin + margin_y + i * y_range / max(N - 1, 1) for i in range(N)]

        def _z_extents_from_section(mesh, heights_list):
            """Return (starts, ends): lists of min/max Z per height, or None if missing."""
            tm = _trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=False)
            try:
                sec = tm.section(plane_origin=[x_cut, 0.0, 0.0], plane_normal=[1.0, 0.0, 0.0])
            except Exception:
                return [None] * len(heights_list), [None] * len(heights_list)
            if sec is None:
                return [None] * len(heights_list), [None] * len(heights_list)

            v3 = sec.vertices  # (M, 3)
            # Build 2D segments in the YZ plane: (y0,z0)→(y1,z1)
            segs = []
            for entity in sec.entities:
                idx = entity.points
                for k in range(len(idx) - 1):
                    y0, z0 = float(v3[idx[k],     1]), float(v3[idx[k],     2])
                    y1, z1 = float(v3[idx[k + 1], 1]), float(v3[idx[k + 1], 2])
                    segs.append((y0, z0, y1, z1))

            starts, ends = [], []
            for yi in heights_list:
                z_cross = []
                for (y0, z0, y1, z1) in segs:
                    dy = y1 - y0
                    if abs(dy) < 1e-9:
                        continue
                    t = (yi - y0) / dy
                    if 0.0 <= t <= 1.0:
                        z_cross.append(z0 + t * (z1 - z0))
                if len(z_cross) >= 2:
                    z_cross.sort()
                    starts.append(z_cross[0])
                    ends.append(z_cross[-1])
                else:
                    starts.append(None)
                    ends.append(None)
            return starts, ends

        def _make_pt(yi, z_val):
            if z_val is None:
                return None
            return np.array([x_cut, yi, z_val], dtype=np.float32)

        starts_a, ends_a = _z_extents_from_section(self._mesh_data, heights)
        starts_b = [None] * N
        ends_b   = [None] * N
        if self._ref_data is not None:
            starts_b, ends_b = _z_extents_from_section(self._ref_data, heights)

        hits_a_start = [_make_pt(h, z) for h, z in zip(heights, starts_a)]
        hits_a_end   = [_make_pt(h, z) for h, z in zip(heights, ends_a)]
        hits_b_start = [_make_pt(h, z) for h, z in zip(heights, starts_b)]
        hits_b_end   = [_make_pt(h, z) for h, z in zip(heights, ends_b)]

        widths_a = [float(e - s) if s is not None and e is not None else 0.0
                    for s, e in zip(starts_a, ends_a)]
        widths_b = [float(e - s) if s is not None and e is not None else 0.0
                    for s, e in zip(starts_b, ends_b)]
        # Separate gap per wall side (left = −Z, right = +Z)
        gaps_left  = [abs(float(sa) - float(sb)) if sa is not None and sb is not None else 0.0
                      for sa, sb in zip(starts_a, starts_b)]
        gaps_right = [abs(float(ea) - float(eb)) if ea is not None and eb is not None else 0.0
                      for ea, eb in zip(ends_a, ends_b)]

        return ProfileScan(
            center=center, x_cut=x_cut, heights=heights,
            hits_a_start=hits_a_start, hits_a_end=hits_a_end,
            hits_b_start=hits_b_start, hits_b_end=hits_b_end,
            widths_a=widths_a, widths_b=widths_b,
            gaps_left=gaps_left, gaps_right=gaps_right,
        )

    def set_radial_wear_mode(self, show_wear: bool):
        """Toggle between 'from center' (False) and 'between crucibles' (True) display.
        Applies to both the radial scan and the profile scan.
        """
        self._radial_show_wear = show_wear
        self._refresh_measure_render()
        if show_wear:
            self.status_message.emit("📊 Entre crisoles: líneas de desgaste Δ")
        else:
            self.status_message.emit("📊 Desde centro: radio de cada crisol")
        self.repaint()

    def _run_radial_scan(self, center: Optional[np.ndarray] = None):
        """Compute radial scan; uses combined centroid of both meshes if center is None."""
        if self._mesh_data is None or self._ref_data is None:
            self.status_message.emit("Necesitás cargar dos crisoles para comparar radialmente")
            return
        if center is None:
            # Average of both centroids = better axis estimate than a single mesh centroid
            ca = self._mesh_data.centroid.astype(np.float64)
            cb = self._ref_data.centroid.astype(np.float64)
            center = ((ca + cb) / 2).astype(np.float32)
        self.status_message.emit("Calculando escaneo radial…")
        scan = self._compute_radial_scan(center)
        if scan is None:
            self.status_message.emit("Error calculando escaneo radial")
            return
        self._radial_scan = scan
        self._refresh_measure_render()
        self.radial_scan_done.emit(scan)
        n_ok = sum(1 for a, b in zip(scan.hits_a, scan.hits_b) if a is not None and b is not None)
        self.status_message.emit(
            f"Radial: {n_ok}/7 direcciones — clic en la malla para cambiar la altura del corte")

    def _compute_radial_scan(self, center: np.ndarray) -> Optional[RadialScan]:
        """Ray-cast in 7 horizontal directions against both meshes.

        Shoots from far OUTSIDE inward to guarantee front-face hits regardless of
        how mesh normals are oriented. Both hits are then sorted by distance from
        center so hits_a = smaller (closer/less worn) and hits_b = larger (farther/more worn).
        """
        if self._mesh_data is None or self._ref_data is None:
            return None
        N = 7
        angles_deg = [i * 360.0 / N for i in range(N)]
        hits_a, hits_b, dists_a, dists_b, gaps = [], [], [], [], []
        orig = center.astype(np.float64)

        # Far enough to start outside both meshes in every direction
        far = float(max(self._mesh_data.radius, self._ref_data.radius)) * 3.0

        for angle in angles_deg:
            rad = np.deg2rad(angle)
            direction = np.array([np.sin(rad), 0.0, np.cos(rad)], dtype=np.float64)
            # Shoot from outside toward center: guarantees hitting front faces
            origin_out = orig + direction * far
            r_m = ray_cast(origin_out, -direction, self._mesh_data)
            r_r = ray_cast(origin_out, -direction, self._ref_data)

            hm = r_m.hit_point.astype(np.float32) if r_m is not None else None
            hr = r_r.hit_point.astype(np.float32) if r_r is not None else None

            dm = float(np.linalg.norm(hm - center)) if hm is not None else None
            dr = float(np.linalg.norm(hr - center)) if hr is not None else None

            # Sort so hits_a = smaller radius (less worn), hits_b = larger (more worn)
            if dm is not None and dr is not None:
                if dm <= dr:
                    ha, hb, da, db = hm, hr, dm, dr
                else:
                    ha, hb, da, db = hr, hm, dr, dm
            elif dm is not None:
                ha, hb, da, db = hm, None, dm, 0.0
            elif dr is not None:
                ha, hb, da, db = hr, None, dr, 0.0
            else:
                ha, hb, da, db = None, None, 0.0, 0.0

            gap = abs(da - db) if (ha is not None and hb is not None) else 0.0
            hits_a.append(ha); hits_b.append(hb)
            dists_a.append(da); dists_b.append(db)
            gaps.append(gap)

        return RadialScan(
            center=center.astype(np.float32),
            angles_deg=angles_deg,
            hits_a=hits_a, hits_b=hits_b,
            dists_a=dists_a, dists_b=dists_b,
            gaps=gaps,
        )

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
        self._diam_measurements.clear()
        self._slider_diams.clear()
        self._depth_measurements.clear()
        self._depth_pending.clear()
        self._radial_scan  = None
        self._profile_scan = None
        self._meas_pending = None
        self._refresh_measure_render()

    def set_ref_mode(self, mode: str):
        self._renderer.set_ref_mode(mode)
        self.update()

    def set_unit(self, factor: float, suffix: str, decimals: int):
        self._unit_factor   = factor
        self._unit_suffix   = suffix
        self._unit_decimals = decimals
        for m in self._measurements:
            v = m.distance * factor
            m.label = f"{v:.{decimals}f} {suffix}"
        self.update()

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
        if self._erase_pending:
            self.cancel_erase()
        self._mode         = mode
        self._align_pts    = []
        self._meas_pending = None
        self._depth_pending.clear()
        self._update_markers()
        self._refresh_measure_render()
        msgs = {
            Mode.NAVIGATE:      "Modo navegación",
            Mode.ANNOTATE:      "Modo anotación — clic en la malla para marcar",
            Mode.ALIGN_3PT:     "Alinear 3 puntos — seleccioná punto 1/3",
            Mode.CALIBRATE_3PT: "CALIBRAR — seleccioná punto de referencia 1/3",
            Mode.MEASURE:       "Medir distancia — clic en el primer punto",
            Mode.CROP_CYLINDER: "Recortar cilindro — seleccioná punto 1/3 sobre el borde del crisol",
            Mode.MEASURE_DIAM:  "Medir diámetro — clic en un punto del horno para medir el diámetro a esa altura",
            Mode.ERASE:          "Borrar caras — arrastrá para pintar (rueda = tamaño), Enter para confirmar, Esc para cancelar",
            Mode.MEASURE_DEPTH:  "Medir profundidad — seleccioná punto del borde 1/4",
            Mode.COMPARE_RADIAL: "Comparación radial — clic en la malla para cambiar la altura del corte",
        }
        if mode == Mode.COMPARE_RADIAL:
            self._run_radial_scan()
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
        self._renderer.draw(mvp, cam_p, use_vcolor=self._heatmap or bool(self._erase_pending))

    def paintEvent(self, event):
        """OpenGL first (via super), then QPainter overlay for gizmo and measurement labels."""
        super().paintEvent(event)

        aspect = self.width() / max(self.height(), 1)
        mvp    = self._camera.get_mvp(aspect)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont("Arial", 10, QFont.Weight.Bold)
        painter.setFont(font)

        # ── axes gizmo (always visible) ────────────────────────────────────
        self._draw_axes_gizmo(painter)

        # ── measurement labels (only when there's something to show) ────────
        has_anything = (self._measurements or self._meas_pending is not None
                        or self._diam_measurements or self._slider_diams
                        or self._depth_measurements or self._radial_scan is not None
                        or self._profile_scan is not None)
        if not has_anything:
            painter.end()
            return

        # Label for each completed distance measurement at its midpoint
        for m in self._measurements:
            sx, sy, vis = self._world_to_screen((m.p1 + m.p2) / 2, mvp)
            if vis:
                tw = 72
                painter.fillRect(sx - tw//2, sy - 16, tw, 18,
                                 QColor(0, 0, 0, 180))
                painter.setPen(QColor(255, 230, 0))
                painter.drawText(sx - tw//2 + 3, sy - 2, m.label)

        # Labels for each diameter spoke at 3/4 of the line
        # user-clicked = red/orange  |  slider live = yellow-green/green
        small_font = QFont("Arial", 8)
        tagged_diams = ([(dm, False) for dm in self._diam_measurements] +
                        [(dm, True)  for dm in self._slider_diams])
        for dm, is_slider in tagged_diams:
            c_max   = QColor(230, 255, 50)  if is_slider else QColor(255, 50,  50)
            c_other = QColor(80,  230, 100) if is_slider else QColor(255, 160, 0)
            for k, (p1, p2, _val, lbl) in enumerate(dm.spokes):
                pos = p1 + 0.75 * (p2 - p1)
                sx, sy, vis = self._world_to_screen(pos, mvp)
                if not vis:
                    continue
                if k == dm.max_idx:
                    painter.setFont(font)
                    painter.setPen(c_max)
                    tw = 120
                else:
                    painter.setFont(small_font)
                    painter.setPen(c_other)
                    tw = 90
                painter.fillRect(sx - tw//2, sy - 16, tw, 18, QColor(0, 0, 0, 170))
                painter.drawText(sx - tw//2 + 3, sy - 2, lbl)

        # Label for the pending first point
        if self._meas_pending is not None:
            sx, sy, vis = self._world_to_screen(self._meas_pending, mvp)
            if vis:
                painter.fillRect(sx + 8, sy - 16, 80, 18, QColor(0, 0, 0, 160))
                painter.setPen(QColor(0, 220, 255))
                painter.drawText(sx + 10, sy - 2, "Pto. 1 — esperando Pto. 2")

        # Depth measurement labels — at midpoint of depth line
        for dm in self._depth_measurements:
            mid = (dm.bottom_pt.astype(np.float32) + dm.foot_pt) / 2
            sx, sy, vis = self._world_to_screen(mid, mvp)
            if vis:
                tw = 180
                painter.setFont(font)
                painter.fillRect(sx - tw//2, sy - 16, tw, 18, QColor(0, 0, 0, 180))
                painter.setPen(QColor(160, 80, 255))
                painter.drawText(sx - tw//2 + 3, sy - 2, dm.label)

        # Radial scan labels
        if self._radial_scan is not None:
            sc = self._radial_scan
            small_font = QFont("Arial", 8)
            for i in range(7):
                if not self._radial_show_wear:
                    # Labels at each hit point for both meshes
                    if sc.hits_a[i] is not None and sc.dists_a[i] > 0:
                        sx, sy, vis = self._world_to_screen(sc.hits_a[i], mvp)
                        if vis:
                            v = sc.dists_a[i] * self._unit_factor
                            lbl = f"{v:.{self._unit_decimals}f}"
                            painter.setFont(small_font)
                            painter.setPen(QColor(255, 160, 60))
                            painter.fillRect(sx + 4, sy - 14, 55, 16, QColor(0, 0, 0, 160))
                            painter.drawText(sx + 5, sy - 2, lbl)
                    if sc.hits_b[i] is not None and sc.dists_b[i] > 0:
                        sx, sy, vis = self._world_to_screen(sc.hits_b[i], mvp)
                        if vis:
                            v = sc.dists_b[i] * self._unit_factor
                            lbl = f"{v:.{self._unit_decimals}f}"
                            painter.setFont(small_font)
                            painter.setPen(QColor(80, 210, 255))
                            painter.fillRect(sx + 4, sy - 14, 55, 16, QColor(0, 0, 0, 160))
                            painter.drawText(sx + 5, sy - 2, lbl)
                else:
                    # Labels at midpoint of gap line
                    ha, hb = sc.hits_a[i], sc.hits_b[i]
                    if ha is not None and hb is not None and sc.gaps[i] > 0:
                        mid = (ha + hb) / 2
                        sx, sy, vis = self._world_to_screen(mid, mvp)
                        if vis:
                            v = sc.gaps[i] * self._unit_factor
                            lbl = f"Δ {v:.{self._unit_decimals}f} {self._unit_suffix}"
                            tw = 110
                            painter.setFont(small_font)
                            painter.setPen(QColor(255, 100, 80))
                            painter.fillRect(sx - tw//2, sy - 14, tw, 16, QColor(0, 0, 0, 160))
                            painter.drawText(sx - tw//2 + 3, sy - 2, lbl)

        # Profile scan labels (X slider horizontal lines)
        if self._profile_scan is not None:
            ps = self._profile_scan
            small_font = QFont("Arial", 8)
            for i in range(len(ps.heights)):
                if not self._radial_show_wear:
                    # Label mesh A line at its midpoint (width value)
                    if (ps.hits_a_start[i] is not None and ps.hits_a_end[i] is not None
                            and ps.widths_a[i] > 0):
                        mid = (ps.hits_a_start[i] + ps.hits_a_end[i]) / 2
                        sx, sy, vis = self._world_to_screen(mid, mvp)
                        if vis:
                            v = ps.widths_a[i] * self._unit_factor
                            lbl = f"{v:.{self._unit_decimals}f}"
                            painter.setFont(small_font)
                            painter.setPen(QColor(230, 230, 50))
                            painter.fillRect(sx + 4, sy - 14, 58, 16, QColor(0, 0, 0, 160))
                            painter.drawText(sx + 5, sy - 2, lbl)
                    if (ps.hits_b_start[i] is not None and ps.hits_b_end[i] is not None
                            and ps.widths_b[i] > 0):
                        mid = (ps.hits_b_start[i] + ps.hits_b_end[i]) / 2
                        sx, sy, vis = self._world_to_screen(mid, mvp)
                        if vis:
                            v = ps.widths_b[i] * self._unit_factor
                            lbl = f"{v:.{self._unit_decimals}f}"
                            painter.setFont(small_font)
                            painter.setPen(QColor(200, 100, 255))
                            painter.fillRect(sx + 4, sy - 14, 58, 16, QColor(0, 0, 0, 160))
                            painter.drawText(sx + 5, sy - 2, lbl)
                else:
                    # Left wall gap label (−Z side)
                    ha_s, hb_s = ps.hits_a_start[i], ps.hits_b_start[i]
                    if ha_s is not None and hb_s is not None and ps.gaps_left[i] > 0:
                        mid = (ha_s + hb_s) / 2
                        sx, sy, vis = self._world_to_screen(mid, mvp)
                        if vis:
                            v = ps.gaps_left[i] * self._unit_factor
                            lbl = f"ΔL {v:.{self._unit_decimals}f}"
                            tw = 90
                            painter.setFont(small_font)
                            painter.setPen(QColor(255, 150, 50))
                            painter.fillRect(sx - tw//2, sy - 14, tw, 16, QColor(0, 0, 0, 160))
                            painter.drawText(sx - tw//2 + 3, sy - 2, lbl)
                    # Right wall gap label (+Z side)
                    ha_e, hb_e = ps.hits_a_end[i], ps.hits_b_end[i]
                    if ha_e is not None and hb_e is not None and ps.gaps_right[i] > 0:
                        mid = (ha_e + hb_e) / 2
                        sx, sy, vis = self._world_to_screen(mid, mvp)
                        if vis:
                            v = ps.gaps_right[i] * self._unit_factor
                            lbl = f"ΔR {v:.{self._unit_decimals}f}"
                            tw = 90
                            painter.setFont(small_font)
                            painter.setPen(QColor(255, 200, 80))
                            painter.fillRect(sx - tw//2, sy - 14, tw, 16, QColor(0, 0, 0, 160))
                            painter.drawText(sx - tw//2 + 3, sy - 2, lbl)

        painter.end()

    # ── mouse ────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        self._last_mouse = event.position()
        self._mouse_btn  = event.button()

        if event.button() == Qt.MouseButton.LeftButton:
            if self._mode in (Mode.ANNOTATE, Mode.ALIGN_3PT,
                              Mode.CALIBRATE_3PT, Mode.MEASURE,
                              Mode.CROP_CYLINDER, Mode.MEASURE_DIAM,
                              Mode.MEASURE_DEPTH, Mode.COMPARE_RADIAL):
                self._handle_pick(event.position())
            elif self._mode == Mode.ERASE:
                self._handle_erase(event.position())

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
        elif btn & Qt.MouseButton.LeftButton and self._mode == Mode.ERASE:
            self._handle_erase(event.position())
        elif btn & Qt.MouseButton.RightButton or btn & Qt.MouseButton.MiddleButton:
            self._camera.pan(dx, dy, self.height())
            self.update()

    def mouseReleaseEvent(self, event):
        self._last_mouse = None
        self._mouse_btn  = None

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if self._mode == Mode.ERASE:
            factor = 1.15 if delta > 0 else 1.0 / 1.15
            self._erase_brush_frac = max(0.005, min(0.5, self._erase_brush_frac * factor))
            if self._mesh_data:
                r = self._mesh_data.radius * self._erase_brush_frac
                self.status_message.emit(
                    f"Radio del pincel: {r:.3f}  — arrastrá para pintar, Enter para confirmar"
                )
        else:
            self._camera.zoom(delta)
            self.update()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._mode == Mode.ERASE:
                self._handle_erase_flood_fill(event.position())
            else:
                self.fit_view()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_F:
            self.fit_view()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._mode == Mode.ERASE and self._erase_pending:
                self.commit_erase()
        elif key == Qt.Key.Key_Escape:
            if self._mode == Mode.ERASE and self._erase_pending:
                self.cancel_erase()
                self.status_message.emit("Borrado cancelado — arrastrá para volver a pintar")
            else:
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
                v = dist * self._unit_factor
                label = f"{v:.{self._unit_decimals}f} {self._unit_suffix}"
                m = Measurement(p1=p1, p2=p2, distance=dist, label=label)
                self._measurements.append(m)
                self._meas_pending = None
                self._refresh_measure_render()
                self.measure_done.emit(m)
                self.status_message.emit(
                    f"Distancia: {label}  — clic para otra medición, Esc para salir")

        elif self._mode == Mode.MEASURE_DIAM:
            self.status_message.emit("Calculando diámetro…")
            dm = self._compute_diameter_at(hit)
            if dm is None:
                self.status_message.emit("No se pudo calcular el diámetro en esa altura — intentá en otro punto")
                return
            self._diam_measurements.append(dm)
            self._refresh_measure_render()
            self.diameter_done.emit(dm)
            self.status_message.emit(f"{dm.label}  — clic para otra medición, Esc para salir")

        elif self._mode == Mode.MEASURE_DEPTH:
            self._depth_pending.append(hit)
            self._update_markers()
            n = len(self._depth_pending)
            if n < 3:
                self.status_message.emit(f"Profundidad — seleccioná punto del borde {n+1}/4")
            elif n == 3:
                self.status_message.emit("Profundidad — seleccioná el punto del fondo (4/4)")
            else:
                p1, p2, p3 = self._depth_pending[:3]
                p4 = hit
                n_vec    = np.cross(p2 - p1, p3 - p1)
                norm_len = np.linalg.norm(n_vec)
                if norm_len < 1e-10:
                    self.status_message.emit("Los 3 puntos del borde son colineales — intentá con otros puntos")
                    self._depth_pending.pop()
                    return
                normal = (n_vec / norm_len).astype(np.float32)
                depth  = abs(float(np.dot(p4 - p1, normal)))
                foot   = (p4 - np.dot(p4 - p1, normal) * normal).astype(np.float32)
                v      = depth * self._unit_factor
                label  = f"Profundidad: {v:.{self._unit_decimals}f} {self._unit_suffix}"
                dm = DepthMeasurement(
                    rim_pts=[p1, p2, p3], bottom_pt=p4.astype(np.float32),
                    foot_pt=foot, normal=normal, depth=depth, label=label)
                self._depth_pending.clear()
                self._depth_measurements.append(dm)
                self._update_markers()
                self._refresh_measure_render()
                self.depth_done.emit(dm)
                self.status_message.emit(f"{label}  — clic para otra medición, Esc para salir")

        elif self._mode == Mode.COMPARE_RADIAL:
            # Click changes the Y height of the scan plane; keep XZ from existing center
            if self._radial_scan is not None:
                center = self._radial_scan.center.copy()
            elif self._mesh_data is not None and self._ref_data is not None:
                ca = self._mesh_data.centroid.astype(np.float64)
                cb = self._ref_data.centroid.astype(np.float64)
                center = ((ca + cb) / 2).astype(np.float32)
            else:
                return
            center[1] = float(hit[1])
            self._run_radial_scan(center)

        elif self._mode in (Mode.ALIGN_3PT, Mode.CALIBRATE_3PT, Mode.CROP_CYLINDER):
            is_calib = (self._mode == Mode.CALIBRATE_3PT)
            is_crop  = (self._mode == Mode.CROP_CYLINDER)
            self._align_pts.append(hit)
            self._update_markers()
            n = len(self._align_pts)
            if is_crop:
                prefix = "Recortar cilindro"
            elif is_calib:
                prefix = "CALIBRAR"
            else:
                prefix = "Alinear"
            if n < 3:
                self.status_message.emit(
                    f"{prefix} — seleccioná punto {n+1}/3")
            else:
                if is_crop:
                    self.status_message.emit("3 puntos seleccionados — calculando recorte…")
                else:
                    self.status_message.emit(
                        f"3 puntos — {'guardando calibración' if is_calib else 'aplicando alineación'}…")
                pts = self._align_pts.copy()
                self._align_pts = []
                self._update_markers()
                self.set_mode(Mode.NAVIGATE)
                if is_calib:
                    self.calibrate_ready.emit(pts)
                elif is_crop:
                    self.crop_ready.emit(pts)
                else:
                    self.align_ready.emit(pts)

    def _update_markers(self):
        if self._mode == Mode.MEASURE_DEPTH and self._depth_pending:
            depth_colors = [
                [1.0, 0.2, 0.2, 1.0],  # red    – rim pt 1
                [0.2, 1.0, 0.2, 1.0],  # green  – rim pt 2
                [0.2, 0.5, 1.0, 1.0],  # blue   – rim pt 3
                [0.8, 0.2, 1.0, 1.0],  # purple – bottom pt
            ]
            pts = self._depth_pending
            positions = np.array(pts, dtype=np.float32)
            colors    = np.array([depth_colors[i] for i in range(len(pts))],
                                  dtype=np.float32)
            self.makeCurrent()
            self._renderer.update_markers(positions, colors)
            self.doneCurrent()
            self.update()
            return

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

    def show_align_pts(self, pts):
        """Show persistent alignment markers for the active scan (or clear if None/empty)."""
        colors_map = [
            [1.0, 0.2, 0.2, 1.0],
            [0.2, 1.0, 0.2, 1.0],
            [0.2, 0.5, 1.0, 1.0],
        ]
        if pts:
            positions = np.array(pts, dtype=np.float32)
            colors    = np.array(colors_map[:len(pts)], dtype=np.float32)
        else:
            positions = np.empty((0, 3), np.float32)
            colors    = np.empty((0, 4), np.float32)
        self.makeCurrent()
        self._renderer.update_markers(positions, colors)
        self.doneCurrent()
        self.update()

    def show_refinement_preview(self, original_pts, refined_pts):
        """Show original clicks (yellow) + refined centroids (colored) + connecting lines.

        Called just before alignment so the user can see how much each bolt
        center was corrected before the mesh transforms.
        """
        colors_refined = [
            [1.0, 0.2, 0.2, 1.0],
            [0.2, 1.0, 0.2, 1.0],
            [0.2, 0.5, 1.0, 1.0],
        ]
        _YELLOW = [1.0, 0.85, 0.1, 1.0]

        all_pts  = list(refined_pts) + list(original_pts)
        all_cols = colors_refined[:len(refined_pts)] + [_YELLOW] * len(original_pts)

        # Lines connecting original → refined for each pair
        seg_pts, seg_col = [], []
        for orig, ref in zip(original_pts, refined_pts):
            seg_pts.extend([orig, ref])
            seg_col.extend([_YELLOW, _YELLOW])

        mk_pos = np.array(all_pts,  dtype=np.float32) if all_pts  else np.empty((0,3), np.float32)
        mk_col = np.array(all_cols, dtype=np.float32) if all_cols else np.empty((0,4), np.float32)
        sg_pos = np.array(seg_pts,  dtype=np.float32) if seg_pts  else np.empty((0,3), np.float32)
        sg_col = np.array(seg_col,  dtype=np.float32) if seg_col  else np.empty((0,4), np.float32)

        self.makeCurrent()
        self._renderer.update_markers(mk_pos, mk_col)
        self._renderer.update_measurements(sg_pos, sg_col,
                                           np.empty((0,3), np.float32),
                                           np.empty((0,4), np.float32))
        self.doneCurrent()
        self.update()

    # ── measurement rendering ──────────────────────────────────────────────

    def _refresh_measure_render(self):
        """Rebuild GPU lines + markers from measurements, diameter rings, and pending point."""
        _YELLOW  = [1.0, 0.85, 0.0, 1.0]
        _CYAN    = [0.0, 0.85, 1.0, 1.0]
        _MAGENTA = [1.0, 0.2,  1.0, 1.0]

        seg_pts, seg_col = [], []
        mk_pts,  mk_col  = [], []

        for m in self._measurements:
            seg_pts.extend([m.p1, m.p2])
            seg_col.extend([_YELLOW, _YELLOW])
            mk_pts.extend([m.p1, m.p2])
            mk_col.extend([_YELLOW, _YELLOW])

        # Color schemes: (ring, spoke, max_spoke, center_marker)
        _SCHEME_USER   = ([1.0, 0.2,  1.0, 1.0],   # magenta ring
                          [1.0, 0.55, 0.0, 1.0],   # orange spokes
                          [1.0, 0.15, 0.15, 1.0],  # red max spoke
                          [1.0, 0.15, 0.15, 1.0])  # red center
        _SCHEME_SLIDER = ([0.2, 0.9,  0.9, 1.0],   # cyan ring
                          [0.3, 0.9,  0.4, 1.0],   # green spokes
                          [0.9, 1.0,  0.2, 1.0],   # yellow-green max spoke
                          [0.9, 1.0,  0.2, 1.0])   # yellow-green center

        tagged = ([(dm, _SCHEME_USER)   for dm in self._diam_measurements] +
                  [(dm, _SCHEME_SLIDER) for dm in self._slider_diams])

        for dm, scheme in tagged:
            c_ring, c_spoke, c_max, c_center = scheme

            # Fitted reference ring
            ring = dm.ring_pts
            N = len(ring)
            for i in range(N):
                seg_pts.extend([ring[i], ring[(i + 1) % N]])
                seg_col.extend([c_ring, c_ring])

            # Spoke diameter lines
            colors_by_k = [c_max if k == dm.max_idx else c_spoke
                           for k in range(len(dm.spokes))]
            for k, (p1, p2, _val, _lbl) in enumerate(dm.spokes):
                color = colors_by_k[k]
                seg_pts.extend([p1, p2])
                seg_col.extend([color, color])
                mk_pts.extend([p1, p2])
                mk_col.extend([color, color])

            # Polygon connecting adjacent rim points in angular order
            rim = ([(dm.spokes[k][0], colors_by_k[k]) for k in range(len(dm.spokes))] +
                   [(dm.spokes[k][1], colors_by_k[k]) for k in range(len(dm.spokes))])
            for i in range(len(rim)):
                a_pt, a_col = rim[i]
                b_pt, b_col = rim[(i + 1) % len(rim)]
                seg_pts.extend([a_pt, b_pt])
                seg_col.extend([a_col, b_col])

            mk_pts.append(dm.center)
            mk_col.append(c_center)
            mk_pts.append(dm.height_point)
            mk_col.append(c_spoke)

        # Depth measurements
        _PURPLE = [0.7, 0.2, 1.0, 1.0]
        _GREEN  = [0.2, 1.0, 0.4, 1.0]
        for dm in self._depth_measurements:
            r1, r2, r3 = dm.rim_pts
            # Triangle connecting 3 rim points
            for a, b in ((r1, r2), (r2, r3), (r3, r1)):
                seg_pts.extend([a, b])
                seg_col.extend([_PURPLE, _PURPLE])
            # Depth line: bottom_pt → foot_pt
            seg_pts.extend([dm.bottom_pt, dm.foot_pt])
            seg_col.extend([_GREEN, _GREEN])
            # Markers: rim pts + bottom pt + foot pt
            for rp in dm.rim_pts:
                mk_pts.append(rp); mk_col.append(_PURPLE)
            mk_pts.append(dm.bottom_pt); mk_col.append(_GREEN)
            mk_pts.append(dm.foot_pt);   mk_col.append(_GREEN)

        if self._meas_pending is not None:
            mk_pts.append(self._meas_pending)
            mk_col.append(_CYAN)

        # Radial comparison
        if self._radial_scan is not None:
            sc = self._radial_scan
            _COL_A   = [0.2, 0.85, 1.0, 1.0]   # cyan   — smaller (less worn)
            _COL_B   = [1.0, 0.55, 0.0, 1.0]   # orange — larger  (more worn)
            _COL_GAP = [1.0, 0.25, 0.1, 1.0]   # red    — gap line

            if not self._radial_show_wear:
                # Mode 1: spokes from center to each surface + rings
                mk_pts.append(sc.center); mk_col.append([1.0, 1.0, 1.0, 1.0])  # white center dot
                for i in range(7):
                    if sc.hits_a[i] is not None:
                        seg_pts.extend([sc.center, sc.hits_a[i]])
                        seg_col.extend([_COL_A, _COL_A])
                        mk_pts.append(sc.hits_a[i]); mk_col.append(_COL_A)
                    if sc.hits_b[i] is not None:
                        seg_pts.extend([sc.center, sc.hits_b[i]])
                        seg_col.extend([_COL_B, _COL_B])
                        mk_pts.append(sc.hits_b[i]); mk_col.append(_COL_B)
                # Rings
                for hits, col in ((sc.hits_a, _COL_A), (sc.hits_b, _COL_B)):
                    valid = [h for h in hits if h is not None]
                    for k in range(len(valid)):
                        seg_pts.extend([valid[k], valid[(k + 1) % len(valid)]])
                        seg_col.extend([col, col])
            else:
                # Mode 2: ONLY gap lines + outer ring of larger crucible
                # (no center spokes — makes the difference visually clear)
                for i in range(7):
                    ha, hb = sc.hits_a[i], sc.hits_b[i]
                    if ha is not None:
                        mk_pts.append(ha); mk_col.append(_COL_A)
                    if hb is not None:
                        mk_pts.append(hb); mk_col.append(_COL_B)
                    if ha is not None and hb is not None:
                        seg_pts.extend([ha, hb]); seg_col.extend([_COL_GAP, _COL_GAP])
                # Only the OUTER (larger) ring so context is visible but clearly different
                valid_b = [h for h in sc.hits_b if h is not None]
                for k in range(len(valid_b)):
                    seg_pts.extend([valid_b[k], valid_b[(k + 1) % len(valid_b)]])
                    seg_col.extend([_COL_B, _COL_B])

        # Profile scan (X slider — horizontal lines at N heights in the YZ cut plane)
        if self._profile_scan is not None:
            ps = self._profile_scan
            _COL_PA   = [0.9, 0.9, 0.0, 1.0]   # yellow  — mesh A lines
            _COL_PB   = [0.8, 0.2, 0.9, 1.0]   # purple  — ref mesh B lines
            _COL_PGAP = [1.0, 0.55, 0.1, 1.0]  # orange  — gap lines

            if not self._radial_show_wear:
                # Mode 1 — show lines from center (left wall → center mark → right wall)
                # Draw each A line and B line; also show a center marker at x_cut
                center_mk = np.array([ps.x_cut,
                                      float(np.mean(ps.heights)),
                                      float(ps.center[2])], dtype=np.float32)
                mk_pts.append(center_mk); mk_col.append([1.0, 1.0, 1.0, 1.0])
                for i in range(len(ps.heights)):
                    if ps.hits_a_start[i] is not None and ps.hits_a_end[i] is not None:
                        seg_pts.extend([ps.hits_a_start[i], ps.hits_a_end[i]])
                        seg_col.extend([_COL_PA, _COL_PA])
                        mk_pts.extend([ps.hits_a_start[i], ps.hits_a_end[i]])
                        mk_col.extend([_COL_PA, _COL_PA])
                    if ps.hits_b_start[i] is not None and ps.hits_b_end[i] is not None:
                        seg_pts.extend([ps.hits_b_start[i], ps.hits_b_end[i]])
                        seg_col.extend([_COL_PB, _COL_PB])
                        mk_pts.extend([ps.hits_b_start[i], ps.hits_b_end[i]])
                        mk_col.extend([_COL_PB, _COL_PB])
            else:
                # Mode 2 — show gap lines between A and B at each height
                for i in range(len(ps.heights)):
                    ha_s, ha_e = ps.hits_a_start[i], ps.hits_a_end[i]
                    hb_s, hb_e = ps.hits_b_start[i], ps.hits_b_end[i]
                    if ha_s is not None: mk_pts.append(ha_s); mk_col.append(_COL_PA)
                    if ha_e is not None: mk_pts.append(ha_e); mk_col.append(_COL_PA)
                    if hb_s is not None: mk_pts.append(hb_s); mk_col.append(_COL_PB)
                    if hb_e is not None: mk_pts.append(hb_e); mk_col.append(_COL_PB)
                    if all(h is not None for h in (ha_s, hb_s)):
                        seg_pts.extend([ha_s, hb_s]); seg_col.extend([_COL_PGAP, _COL_PGAP])
                    if all(h is not None for h in (ha_e, hb_e)):
                        seg_pts.extend([ha_e, hb_e]); seg_col.extend([_COL_PGAP, _COL_PGAP])
        segs = np.array(seg_pts, dtype=np.float32) if seg_pts else np.empty((0, 3), np.float32)
        scol = np.array(seg_col, dtype=np.float32) if seg_col else np.empty((0, 4), np.float32)
        mpts = np.array(mk_pts,  dtype=np.float32) if mk_pts  else np.empty((0, 3), np.float32)
        mcol = np.array(mk_col,  dtype=np.float32) if mk_col  else np.empty((0, 4), np.float32)

        # Translucent planes: diameter cut planes + radial wear area
        _PLANE_USER   = [0.35, 0.65, 1.0, 0.18]
        _PLANE_SLIDER = [0.20, 0.90, 0.55, 0.14]
        _PLANE_WEAR   = [0.90, 0.25, 0.10, 0.38]
        plane_parts, color_parts = [], []
        for dm in self._diam_measurements:
            plane_parts.append(dm.plane_verts)
            color_parts.append(np.tile(_PLANE_USER,   (len(dm.plane_verts), 1)).astype(np.float32))
        for dm in self._slider_diams:
            plane_parts.append(dm.plane_verts)
            color_parts.append(np.tile(_PLANE_SLIDER, (len(dm.plane_verts), 1)).astype(np.float32))
        # Wear area filled polygon (only in wear-display mode)
        if self._radial_scan is not None and self._radial_show_wear:
            sc = self._radial_scan
            for i in range(7):
                j = (i + 1) % 7
                ha_i, hb_i = sc.hits_a[i], sc.hits_b[i]
                ha_j, hb_j = sc.hits_a[j], sc.hits_b[j]
                if all(h is not None for h in (ha_i, hb_i, ha_j, hb_j)):
                    tri = np.array([ha_i, hb_i, ha_j, hb_i, hb_j, ha_j], dtype=np.float32)
                    plane_parts.append(tri)
                    color_parts.append(np.tile(_PLANE_WEAR, (6, 1)).astype(np.float32))

        # Profile scan gap strips (filled between consecutive height rows in wear mode)
        if self._profile_scan is not None and self._radial_show_wear:
            ps = self._profile_scan
            for i in range(len(ps.heights) - 1):
                ha_s_i, ha_e_i = ps.hits_a_start[i],     ps.hits_a_end[i]
                hb_s_i, hb_e_i = ps.hits_b_start[i],     ps.hits_b_end[i]
                ha_s_j, ha_e_j = ps.hits_a_start[i + 1], ps.hits_a_end[i + 1]
                hb_s_j, hb_e_j = ps.hits_b_start[i + 1], ps.hits_b_end[i + 1]
                # Left-side (−Z) gap strip
                if all(h is not None for h in (ha_s_i, hb_s_i, ha_s_j, hb_s_j)):
                    tri = np.array([ha_s_i, hb_s_i, ha_s_j,
                                    hb_s_i, hb_s_j, ha_s_j], dtype=np.float32)
                    plane_parts.append(tri)
                    color_parts.append(np.tile(_PLANE_WEAR, (6, 1)).astype(np.float32))
                # Right-side (+Z) gap strip
                if all(h is not None for h in (ha_e_i, hb_e_i, ha_e_j, hb_e_j)):
                    tri = np.array([ha_e_i, hb_e_i, ha_e_j,
                                    hb_e_i, hb_e_j, ha_e_j], dtype=np.float32)
                    plane_parts.append(tri)
                    color_parts.append(np.tile(_PLANE_WEAR, (6, 1)).astype(np.float32))

        if plane_parts:
            all_pv = np.concatenate(plane_parts)
            pcol   = np.concatenate(color_parts)
        else:
            all_pv = np.empty((0, 3), np.float32)
            pcol   = np.empty((0, 4), np.float32)

        self.makeCurrent()
        self._renderer.update_measurements(segs, scol, mpts, mcol)
        self._renderer.update_cut_plane(all_pv, pcol)
        self.doneCurrent()
        self.update()

    # ── axes gizmo ────────────────────────────────────────────────────────

    def _draw_axes_gizmo(self, painter: QPainter):
        """Draw an XYZ orientation indicator in the bottom-left corner.

        Uses the camera view matrix's rotation rows to project each world axis
        onto screen space — no perspective, just rotation — so the gizmo tracks
        the orbit correctly without being affected by zoom or translation.
        """
        V  = self._camera.get_view_matrix()
        R  = V[0, :3]   # camera right  → world X maps to screen X via dot(R, axis)
        U  = V[1, :3]   # camera up     → world Y maps to screen Y (inverted) via dot(U, axis)

        GX = 65                      # gizmo origin, pixels from left
        GY = self.height() - 65      # gizmo origin, pixels from bottom
        L  = 46                      # arm length in pixels

        # Semi-transparent dark background circle
        painter.setBrush(QColor(0, 0, 0, 110))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(GX - 58, GY - 58, 116, 116)

        axes = [
            (np.array([1.0, 0.0, 0.0]), QColor(220, 60,  60),  "X"),
            (np.array([0.0, 1.0, 0.0]), QColor(60,  200, 60),  "Y"),
            (np.array([0.0, 0.0, 1.0]), QColor(80,  140, 255), "Z"),
        ]

        # Sort: axes pointing away from viewer drawn first (behind)
        # depth in camera space = -dot(V[2,:3], d); more positive = closer
        axes.sort(key=lambda a: float(np.dot(V[2, :3], a[0])))

        gizmo_font = QFont("Arial", 9, QFont.Weight.Bold)
        painter.setFont(gizmo_font)

        for (d, color, label) in axes:
            sx =  float(np.dot(R, d))
            sy = -float(np.dot(U, d))   # invert Y: screen y grows downward
            n  = np.hypot(sx, sy)
            if n < 1e-8:
                continue
            sx /= n;  sy /= n

            ex = int(GX + sx * L)
            ey = int(GY + sy * L)

            painter.setPen(QPen(color, 2))
            painter.drawLine(GX, GY, ex, ey)

            lx = int(GX + sx * (L + 13))
            ly = int(GY + sy * (L + 13))
            painter.setPen(color)
            painter.drawText(lx - 5, ly + 4, label)

        # White dot at origin
        painter.setBrush(QColor(255, 255, 255, 210))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(GX - 3, GY - 3, 6, 6)

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

    # ── erase mode ────────────────────────────────────────────────────────

    def _handle_erase(self, pos):
        """Ray-cast the brush position and accumulate nearby faces into the erase set."""
        if self._mesh_data is None or self._face_centroids is None:
            return
        w, h = self.width(), self.height()
        origin, direction = self._camera.get_ray(pos.x(), pos.y(), w, h)
        result = ray_cast(origin, direction, self._mesh_data)
        if result is None or result.face_index < 0:
            return

        brush_r = self._mesh_data.radius * self._erase_brush_frac
        hit_c   = self._face_centroids[result.face_index]
        dists   = np.linalg.norm(self._face_centroids - hit_c, axis=1)
        new_ids = set(int(i) for i in np.where(dists <= brush_r)[0])

        if new_ids - self._erase_pending:
            self._erase_pending |= new_ids
            self._update_erase_preview()
            n = len(self._erase_pending)
            self.erase_updated.emit(n)
            self.status_message.emit(
                f"Borrar: {n:,} caras seleccionadas  "
                f"(radio {brush_r:.2f}) — Enter para confirmar, Esc para cancelar"
            )

    def _update_erase_preview(self):
        """Color selected-to-delete faces red on the GPU without modifying MeshData."""
        if self._mesh_data is None or self._mesh_data.faces is None:
            return
        colors = self._mesh_data.colors.copy()
        if self._erase_pending:
            face_idx = np.array(list(self._erase_pending), dtype=np.int64)
            face_idx = face_idx[face_idx < len(self._mesh_data.faces)]
            if len(face_idx):
                vert_idx = self._mesh_data.faces[face_idx].flatten()
                colors[vert_idx] = [1.0, 0.18, 0.08, 1.0]
        self.makeCurrent()
        self._renderer.update_colors(colors)
        self.doneCurrent()
        self.update()

    def commit_erase(self):
        """Remove all pending faces, rebuild the mesh, and emit faces_erased."""
        if not self._erase_pending or self._mesh_data is None:
            return

        import trimesh as _trimesh
        from core.loader import MeshData

        mesh = self._mesh_data
        keep = np.ones(len(mesh.faces), dtype=bool)
        for fi in self._erase_pending:
            if 0 <= fi < len(mesh.faces):
                keep[fi] = False

        new_faces_raw = mesh.faces[keep]
        if len(new_faces_raw) == 0:
            self.status_message.emit("Error: no quedarían caras — borrado cancelado")
            return

        tm = _trimesh.Trimesh(vertices=mesh.vertices, faces=new_faces_raw, process=True)
        tm.remove_unreferenced_vertices()

        verts  = np.asarray(tm.vertices,       dtype=np.float32)
        norms  = np.asarray(tm.vertex_normals, dtype=np.float32)
        faces  = np.asarray(tm.faces,          dtype=np.int32)
        colors = np.tile([0.72, 0.78, 0.85, 1.0], (len(verts), 1)).astype(np.float32)
        c      = verts.mean(axis=0).astype(np.float32)
        r      = float(np.max(np.linalg.norm(verts - c, axis=1))) if len(verts) else 1.0

        new_data = MeshData(
            vertices=verts, faces=faces, normals=norms, colors=colors,
            centroid=c, radius=r, is_point_cloud=False,
            source_path=mesh.source_path,
            vertex_count=len(verts), face_count=len(faces),
        )

        self._erase_pending.clear()
        self.erase_updated.emit(0)
        self.load_mesh(new_data)
        self.faces_erased.emit(new_data)

    def cancel_erase(self):
        """Deselect all pending faces and restore original mesh colors."""
        if not self._erase_pending:
            return
        self._erase_pending.clear()
        self.erase_updated.emit(0)
        if self._mesh_data is not None:
            self.makeCurrent()
            self._renderer.update_colors(self._mesh_data.colors)
            self.doneCurrent()
            self.update()

    def _handle_erase_flood_fill(self, pos):
        """BFS from the hit face through edge-adjacent neighbors, expanding the selection."""
        if self._mesh_data is None or self._face_neighbors is None:
            return
        w, h = self.width(), self.height()
        origin, direction = self._camera.get_ray(pos.x(), pos.y(), w, h)
        result = ray_cast(origin, direction, self._mesh_data)
        if result is None or result.face_index < 0:
            return

        start = result.face_index
        neighbors = self._face_neighbors

        # BFS: collect the entire connected component reachable from start
        visited = {start}
        queue   = [start]
        while queue:
            fi = queue.pop()   # DFS order — faster stack pop, same result
            for nb in neighbors[fi]:
                if nb not in visited:
                    visited.add(nb)
                    queue.append(nb)

        new_faces = visited - self._erase_pending
        if new_faces:
            self._erase_pending |= visited
            self._update_erase_preview()
            n = len(self._erase_pending)
            self.erase_updated.emit(n)
            self.status_message.emit(
                f"Selección expandida: {n:,} caras — Enter para confirmar, Esc para cancelar"
            )

    @staticmethod
    def _compute_face_adjacency(faces: np.ndarray) -> list:
        """Return edge-adjacent neighbor list for every face. O(F log F) via numpy sort."""
        nf = len(faces)
        f  = np.asarray(faces, dtype=np.int64)

        # Build (3F, 3) array: [min_v, max_v, face_idx] for each of 3 edges per face
        parts = []
        for j in range(3):
            mn = np.minimum(f[:, j], f[:, (j + 1) % 3])
            mx = np.maximum(f[:, j], f[:, (j + 1) % 3])
            fi = np.arange(nf, dtype=np.int64)
            parts.append(np.stack([mn, mx, fi], axis=1))
        edges = np.concatenate(parts, axis=0)   # (3F, 3)

        # Sort by (min_v, max_v) so shared edges become consecutive rows
        order = np.lexsort((edges[:, 1], edges[:, 0]))
        se    = edges[order]

        # Find consecutive pairs that share the same edge
        same = (se[:-1, 0] == se[1:, 0]) & (se[:-1, 1] == se[1:, 1])
        adj  = [[] for _ in range(nf)]
        for i in np.where(same)[0]:
            a, b = int(se[i, 2]), int(se[i + 1, 2])
            adj[a].append(b)
            adj[b].append(a)
        return adj

    # ── diameter calculation ───────────────────────────────────────────────

    def _compute_diameter_at(self, point: np.ndarray) -> 'Optional[DiameterMeasurement]':
        """Slice the mesh at the height of `point` and compute 7 evenly-spaced diameter lines.

        Evaluates cross-sections on the Y axis (XZ plane) and Z axis (XY plane) and picks
        the one whose boundary is most circular (min/max radius ratio closest to 1).
        This auto-detects whether the furnace vertical axis is Y or Z.
        """
        mesh = self._mesh_data
        if mesh is None or mesh.faces is None or len(mesh.faces) == 0:
            return None

        import trimesh as _trimesh
        tm = _trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces, process=False)

        # ── pass 1: collect valid candidates and score by circularity ──────
        candidates = []   # (circularity, axis, ax0, ax1, z, verts_2d, center_2d, segments)

        for axis, ax0, ax1 in ((1, 0, 2), (2, 0, 1), (0, 1, 2)):   # Y, Z, X axes
            z = float(point[axis])
            origin_arr = [0.0, 0.0, 0.0]; origin_arr[axis] = z
            normal_arr = [0.0, 0.0, 0.0]; normal_arr[axis] = 1.0
            try:
                section = tm.section(plane_origin=origin_arr, plane_normal=normal_arr)
            except Exception:
                continue
            if section is None:
                continue
            verts_3d = section.vertices
            if len(verts_3d) < 6:
                continue

            verts_2d  = verts_3d[:, [ax0, ax1]]
            center_2d = verts_2d.mean(axis=0)
            radii_all = np.linalg.norm(verts_2d - center_2d, axis=1)
            r_max     = float(radii_all.max())
            if r_max < 1e-8:
                continue
            circularity = float(radii_all.min()) / r_max   # 1 = perfect circle

            # Build boundary segments
            segments = []
            for entity in section.entities:
                idx = entity.points
                for i in range(len(idx) - 1):
                    segments.append((verts_2d[idx[i]], verts_2d[idx[i + 1]]))
                if len(idx) >= 2:
                    a, b = verts_2d[idx[-1]], verts_2d[idx[0]]
                    if not np.allclose(a, b, atol=1e-8):
                        segments.append((a, b))
            if not segments:
                continue

            candidates.append((circularity, axis, ax0, ax1, z, verts_2d, center_2d, segments))

        if not candidates:
            return None

        # ── pass 2: use the most circular section ──────────────────────────
        candidates.sort(key=lambda c: c[0], reverse=True)
        _circ, axis, ax0, ax1, z, verts_2d, center_2d, segments = candidates[0]

        cx, cy = center_2d

        def _rim(d2):
            dx, dy = d2
            best_t, best_pt = -1.0, None
            for (a, b) in segments:
                vx, vy = b[0] - a[0], b[1] - a[1]
                denom = dx * vy - dy * vx
                if abs(denom) < 1e-12:
                    continue
                tx, ty = a[0] - cx, a[1] - cy
                t = (tx * vy - ty * vx) / denom
                s = (tx * dy - ty * dx) / denom
                if t > 1e-6 and -1e-6 <= s <= 1.0 + 1e-6 and t > best_t:
                    best_t = t
                    best_pt = center_2d + t * d2
            if best_pt is None:
                best_pt = verts_2d[int(np.argmax(verts_2d @ d2))]
            return best_pt

        def _to3d(p2):
            pt = np.zeros(3, dtype=np.float32)
            pt[ax0], pt[ax1], pt[axis] = p2[0], p2[1], z
            return pt

        # Starting angle aligned with the clicked point
        delta = np.array([float(point[ax0]) - cx, float(point[ax1]) - cy])
        theta = float(np.arctan2(delta[1], delta[0])) if np.linalg.norm(delta) > 1e-8 else 0.0

        N_DIRS = 7
        spokes = []
        for k in range(N_DIRS):
            angle = theta + k * np.pi / N_DIRS
            d    = np.array([np.cos(angle), np.sin(angle)])
            p1   = _to3d(_rim( d))
            p2   = _to3d(_rim(-d))
            other = [a for a in (0, 1, 2) if a != axis]
            dval  = float(np.linalg.norm(p2[other] - p1[other]))
            v   = dval * self._unit_factor
            lbl = f"⌀{v:.{self._unit_decimals}f} {self._unit_suffix}"
            spokes.append((p1, p2, dval, lbl))

        max_idx  = int(np.argmax([s[2] for s in spokes]))
        v_max    = spokes[max_idx][2] * self._unit_factor
        main_lbl = f"⌀ {v_max:.{self._unit_decimals}f} {self._unit_suffix} (máx)"

        radii  = np.linalg.norm(verts_2d - center_2d, axis=1)
        radius = float(radii.mean())
        N_ring = 64
        ang    = np.linspace(0.0, 2.0 * np.pi, N_ring, endpoint=False)
        ring   = np.zeros((N_ring, 3), dtype=np.float32)
        ring[:, ax0]  = cx + radius * np.cos(ang)
        ring[:, ax1]  = cy + radius * np.sin(ang)
        ring[:, axis] = z

        center_3d = np.zeros(3, dtype=np.float32)
        center_3d[ax0], center_3d[ax1], center_3d[axis] = cx, cy, z

        # Cut-plane quad: square centred on the section, 110% of max-radius half-side
        half = float(max(s[2] for s in spokes)) / 2.0 * 1.1
        def _corner(u, v):
            c = np.zeros(3, dtype=np.float32)
            c[ax0], c[ax1], c[axis] = cx + u * half, cy + v * half, z
            return c
        c0, c1, c2, c3 = _corner(-1,-1), _corner(1,-1), _corner(1,1), _corner(-1,1)
        plane_verts = np.array([c0, c1, c2, c0, c2, c3], dtype=np.float32)

        return DiameterMeasurement(
            height_point = point.astype(np.float32),
            center       = center_3d,
            spokes       = spokes,
            max_idx      = max_idx,
            label        = main_lbl,
            ring_pts     = ring,
            plane_verts  = plane_verts,
        )
