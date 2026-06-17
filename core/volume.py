"""Working-volume computation for crucibles via cross-section integration.

Integrates horizontal cross-sectional areas from a user-defined fill-level
plane down to the deepest part of the mesh.  Works even for open (non-
watertight) scan meshes because it never relies on a closed-surface volume.
"""
import numpy as np

_LIQUID_IRON_DENSITY_KG_M3 = 7000.0   # kg / m³ at ~1 500 °C
_MM3_PER_M3                = 1e9


def compute_fill_volume(mesh_tm,
                        fill_origin: np.ndarray,
                        fill_normal: np.ndarray,
                        n_slices: int = 80) -> float:
    """Integrate cross-sections from fill_origin plane down to crucible bottom.

    Parameters
    ----------
    mesh_tm      : trimesh.Trimesh — the crucible inner-surface mesh
    fill_origin  : (3,) point on the fill-level plane (rim centroid - normal*height)
    fill_normal  : unit vector pointing OUTWARD (away from crucible interior / upward)
    n_slices     : number of horizontal slices used for the Riemann sum

    Returns
    -------
    Volume in the same linear units³ as the mesh coordinates (typically mm³).
    """
    normal = np.asarray(fill_normal, dtype=np.float64)
    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-12:
        return 0.0
    normal /= norm_len
    origin = np.asarray(fill_origin, dtype=np.float64)

    # Signed distance of every vertex from the fill plane (positive = above)
    dots = (mesh_tm.vertices.astype(np.float64) - origin) @ normal
    below = dots < 1e-6   # include vertices exactly at fill plane
    if not below.any():
        return 0.0

    max_depth = float(-dots[below].min())   # positive depth to deepest vertex
    if max_depth < 1e-12:
        return 0.0

    depths = np.linspace(0.0, max_depth, n_slices + 1)
    areas  = []

    for d in depths:
        slice_pt = origin - normal * d
        try:
            section = mesh_tm.section(plane_origin=slice_pt, plane_normal=normal)
            if section is None or len(section.entities) == 0:
                areas.append(0.0)
                continue
            path2d, _ = section.to_2D()
            areas.append(max(0.0, float(path2d.area)))
        except Exception:
            areas.append(0.0)

    return float(np.trapz(areas, depths))


# ── unit conversions ──────────────────────────────────────────────────────────

def volume_mm3_to_m3(vol_mm3: float) -> float:
    return vol_mm3 / _MM3_PER_M3


def mass_kg(vol_m3: float) -> float:
    return vol_m3 * _LIQUID_IRON_DENSITY_KG_M3


def mass_ton(vol_m3: float) -> float:
    return mass_kg(vol_m3) / 1000.0
