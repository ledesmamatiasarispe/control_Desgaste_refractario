"""Working-volume computation for crucibles via cross-section integration.

Integrates horizontal cross-sectional areas from a user-defined fill-level
plane down to the deepest part of the mesh.  Works even for open (non-
watertight) scan meshes because it never relies on a closed-surface volume.
"""
import numpy as np

_LIQUID_IRON_DENSITY_KG_M3 = 7150.0   # kg / m³ at ~1 500 °C
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


def compute_section_profile(mesh_tm,
                            fill_origin: np.ndarray,
                            fill_normal: np.ndarray,
                            n_slices: int = 100):
    """Pre-compute cross-section areas along the full mesh height.

    Returns (depths, areas) arrays where depths[i] is the distance below
    fill_origin and areas[i] is the cross-sectional area at that depth.
    Used by the interactive fill-level slider for instant volume updates.
    """
    normal = np.asarray(fill_normal, dtype=np.float64)
    norm_len = np.linalg.norm(normal)
    if norm_len < 1e-12:
        return np.array([0.0]), np.array([0.0])
    normal /= norm_len
    origin = np.asarray(fill_origin, dtype=np.float64)

    dots = (mesh_tm.vertices.astype(np.float64) - origin) @ normal
    below = dots < 1e-6
    if not below.any():
        return np.array([0.0]), np.array([0.0])

    max_depth = float(-dots[below].min())
    if max_depth < 1e-12:
        return np.array([0.0]), np.array([0.0])

    depths = np.linspace(0.0, max_depth, n_slices + 1)
    areas  = []
    valid  = 0
    for d in depths:
        slice_pt = origin - normal * d
        try:
            section = mesh_tm.section(plane_origin=slice_pt, plane_normal=normal)
            if section is None or len(section.entities) == 0:
                areas.append(0.0)
                continue
            path2d, _ = section.to_2D()
            a = max(0.0, float(path2d.area))
            areas.append(a)
            if a > 0.0:
                valid += 1
        except Exception:
            areas.append(0.0)

    areas_np = np.array(areas)

    # Leading zeros: the plane clips the mesh edge and returns no section.
    # Extrapolate the first valid area upward so the top portion contributes
    # correctly to the integral instead of silently dropping to zero.
    nonzero = np.where(areas_np > 0)[0]
    if nonzero.size > 0:
        first = int(nonzero[0])
        if first > 0:
            areas_np[:first] = areas_np[first]

    return depths, areas_np, valid


def volume_from_profile(depths: np.ndarray, areas: np.ndarray,
                        target_depth: float) -> float:
    """Integrate a pre-computed section profile up to target_depth. Instant."""
    if target_depth <= 0.0 or len(depths) < 2:
        return 0.0
    target_depth = min(target_depth, float(depths[-1]))
    idx = int(np.searchsorted(depths, target_depth))
    idx = min(idx, len(depths) - 1)
    d_sub = np.append(depths[:idx], target_depth)
    a_sub = np.append(areas[:idx], float(np.interp(target_depth, depths, areas)))
    return float(np.trapz(a_sub, d_sub))


# ── unit conversions ──────────────────────────────────────────────────────────

def volume_mm3_to_m3(vol_mm3: float) -> float:
    return vol_mm3 / _MM3_PER_M3


def mass_kg(vol_m3: float) -> float:
    return vol_m3 * _LIQUID_IRON_DENSITY_KG_M3


def mass_ton(vol_m3: float) -> float:
    return mass_kg(vol_m3) / 1000.0
