import copy
import numpy as np
from core.loader import MeshData


def three_point_align(mesh: MeshData,
                      p1: np.ndarray,
                      p2: np.ndarray,
                      p3: np.ndarray,
                      target_dist: float = None) -> MeshData:
    """
    Align mesh so the plane defined by (p1,p2,p3) is Y-up, centroid at origin.

    target_dist: if given, uniformly scale the mesh so that |p2-p1| == target_dist.
                 Pass the distance from a reference mesh to normalise scale across
                 campaigns.  Normals are NOT scaled (direction is preserved).
    """
    v1 = (p2 - p1).astype(np.float64)
    v2 = (p3 - p1).astype(np.float64)
    normal = np.cross(v1, v2)
    n_len  = np.linalg.norm(normal)
    if n_len < 1e-10:
        raise ValueError("Los 3 puntos son colineales — elegí puntos más separados.")
    normal /= n_len

    if normal[1] < 0:
        normal = -normal

    R = _rot_from_vecs(normal, np.array([0.0, 1.0, 0.0]))

    centroid = (p1 + p2 + p3).astype(np.float64) / 3.0

    T4        = np.eye(4)
    T4[:3, 3] = -centroid
    R4        = np.eye(4)
    R4[:3, :3] = R

    transform = R4 @ T4

    # Uniform scale: ratio of reference distance to current p1-p2 distance
    scale = 1.0
    if target_dist is not None:
        current_dist = float(np.linalg.norm(v1))
        if current_dist > 1e-12:
            scale = target_dist / current_dist

    return _apply(mesh, transform, scale=scale), transform, scale


def umeyama_align(mesh: MeshData,
                  src_pts: list,
                  tgt_pts: list) -> tuple:
    """Find optimal scale + rotation + translation mapping src_pts → tgt_pts.

    Uses the Umeyama algorithm (SVD on cross-covariance of the 3 point pairs).
    Returns (aligned_MeshData, T4, scale) where T4 is the 4x4 transform and
    scale is the uniform scale factor found.  More robust than single-distance
    ratio when reference points are imprecisely picked.
    """
    src = np.array(src_pts, dtype=np.float64)   # (3, 3)
    dst = np.array(tgt_pts, dtype=np.float64)   # (3, 3)
    n   = len(src)

    mu_src = src.mean(axis=0)
    mu_dst = dst.mean(axis=0)
    src_c  = src - mu_src
    dst_c  = dst - mu_dst

    var_src = (src_c ** 2).sum() / n
    K       = (dst_c.T @ src_c) / n
    U, sigma, Vt = np.linalg.svd(K)

    S = np.eye(3)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        S[2, 2] = -1.0

    R     = U @ S @ Vt
    scale = float((sigma @ S).sum() / var_src) if var_src > 1e-12 else 1.0
    t     = mu_dst - scale * R @ mu_src

    # _apply computes: (R @ v + T4[:3,3]) * scale
    # We want:         scale * R @ v + t
    # So T4[:3,3] must be t/scale so that (T4[:3,3]) * scale == t
    T4 = np.eye(4)
    T4[:3, :3] = R
    T4[:3,  3] = t / scale if scale > 1e-12 else t

    return _apply(mesh, T4, scale=scale), T4, scale


def icp_align(source: MeshData, target: MeshData,
              pre_align: bool = True) -> MeshData:
    """Align source to target using ICP.

    pre_align: if True, translate source centroid to target centroid before
               running ICP (useful when meshes are far apart).  Set False
               when source is already coarsely aligned (e.g. after
               three_point_align) to avoid the centroid step moving it away.
    """
    if pre_align:
        offset     = (target.centroid - source.centroid).astype(np.float64)
        pre        = np.eye(4)
        pre[:3, 3] = offset
        src_verts  = (source.vertices + offset).astype(np.float32)
    else:
        pre       = np.eye(4)
        src_verts = source.vertices.copy()

    src_sub = _subsample(src_verts, _ICP_MAX_PTS).astype(np.float64)
    tgt_sub = _subsample(target.vertices, _ICP_MAX_PTS).astype(np.float64)

    err_before = _mean_nn_dist(src_sub, tgt_sub)
    matrix     = _icp_kdtree(src_sub, tgt_sub, max_iter=50, tol=1e-6)
    src_after  = (matrix[:3, :3] @ src_sub.T).T + matrix[:3, 3]
    err_after  = _mean_nn_dist(src_after, tgt_sub)

    if err_after >= err_before:
        # ICP diverged — keep only the pre-alignment step, discard ICP result
        transform = pre
    else:
        transform = matrix @ pre
    return _apply(source, transform)


_ICP_MAX_PTS = 1000   # subsample above this — brute-force NN scales as N²


def icp_align_near_pts(source: MeshData, target: MeshData,
                       src_pts: list, tgt_pts: list,
                       patch_radius: float) -> MeshData:
    """ICP using only the mesh regions near the 3 reference points (bolts)."""
    src_patch = _extract_patch_verts(source.vertices, src_pts, patch_radius)
    tgt_patch = _extract_patch_verts(target.vertices, tgt_pts, patch_radius)

    if len(src_patch) < 20 or len(tgt_patch) < 20:
        return icp_align(source, target, pre_align=False)

    src_patch = _subsample(src_patch, _ICP_MAX_PTS)
    tgt_patch = _subsample(tgt_patch, _ICP_MAX_PTS)

    sp = src_patch.astype(np.float64)
    tp = tgt_patch.astype(np.float64)
    err_before = _mean_nn_dist(sp, tp)
    T          = _icp_kdtree(sp, tp, max_iter=50, tol=1e-6)
    sp_after   = (T[:3, :3] @ sp.T).T + T[:3, 3]
    err_after  = _mean_nn_dist(sp_after, tp)
    if err_after >= err_before:
        return source   # ICP diverged — return mesh unchanged
    return _apply(source, T)


def _subsample(verts: np.ndarray, max_pts: int) -> np.ndarray:
    if len(verts) <= max_pts:
        return verts
    idx = np.random.choice(len(verts), max_pts, replace=False)
    return verts[idx]


def _extract_patch_verts(verts: np.ndarray,
                         centers: list,
                         radius: float) -> np.ndarray:
    """Return all vertices within `radius` of any center point."""
    mask = np.zeros(len(verts), dtype=bool)
    for c in centers:
        c = np.array(c, dtype=np.float64)
        dists = np.linalg.norm(verts.astype(np.float64) - c, axis=1)
        mask |= (dists <= radius)
    return verts[mask]


# ── internals ────────────────────────────────────────────────────────────────

def refine_pts_to_local_centroid(pts: list,
                                 verts: np.ndarray,
                                 radius_fraction: float = 0.06) -> list:
    """Snap each point to the centroid of nearby vertices.

    Replaces a rough click on a noisy bolt surface with the stable geometric
    center of all mesh points within radius_fraction * mesh_radius of the click.
    Falls back to the original point if too few vertices are nearby.
    """
    vf  = verts.astype(np.float64)
    r   = float(np.max(np.linalg.norm(vf - vf.mean(0), axis=1))) * radius_fraction
    out = []
    for pt in pts:
        pa    = np.array(pt, dtype=np.float64)
        dists = np.linalg.norm(vf - pa, axis=1)
        near  = vf[dists <= r]
        out.append(near.mean(axis=0).tolist() if len(near) >= 5 else list(pa))
    return out


def _mean_nn_dist(src: np.ndarray, tgt: np.ndarray) -> float:
    """Mean distance from each src point to its nearest tgt point."""
    diff  = src[:, np.newaxis, :] - tgt[np.newaxis, :, :]
    dists = (diff * diff).sum(axis=2)
    return float(dists.min(axis=1).mean())


def _nearest_neighbors(src: np.ndarray, tgt: np.ndarray) -> np.ndarray:
    """Brute-force nearest neighbors — pure numpy, safe from any thread."""
    # src (N,3), tgt (M,3) → indices (N,) into tgt
    diff  = src[:, np.newaxis, :] - tgt[np.newaxis, :, :]  # (N, M, 3)
    dists = (diff * diff).sum(axis=2)                        # (N, M)
    return dists.argmin(axis=1)


def _icp_kdtree(src: np.ndarray, tgt: np.ndarray,
                max_iter: int = 50, tol: float = 1e-6) -> np.ndarray:
    """Pure-numpy ICP — rigid (rotation + translation, no scale).
    Returns a 4x4 homogeneous transform that maps src → tgt."""
    accum = np.eye(4)
    pts   = src.copy()

    for _ in range(max_iter):
        idx         = _nearest_neighbors(pts, tgt)
        matched_tgt = tgt[idx]

        mu_s = pts.mean(0);  mu_t = matched_tgt.mean(0)
        sc   = pts - mu_s;   tc   = matched_tgt - mu_t
        K    = tc.T @ sc / len(pts)
        U, _, Vt = np.linalg.svd(K)
        S = np.eye(3)
        if np.linalg.det(U) * np.linalg.det(Vt) < 0:
            S[2, 2] = -1
        R = U @ S @ Vt
        t = mu_t - R @ mu_s

        pts   = (R @ pts.T).T + t
        step  = np.eye(4)
        step[:3, :3] = R
        step[:3,  3] = t
        accum = step @ accum

        if np.linalg.norm(t) < tol and np.allclose(R, np.eye(3), atol=tol):
            break

    return accum

def _apply(mesh: MeshData, T: np.ndarray, scale: float = 1.0) -> MeshData:
    R = T[:3, :3].astype(np.float32)
    t = T[:3,  3].astype(np.float32)

    new_verts   = (R @ mesh.vertices.T).T + t
    new_normals = (R @ mesh.normals.T).T   # direction only, never scaled

    if scale != 1.0:
        new_verts = new_verts * np.float32(scale)
        # normals stay unit-length — no scaling

    centroid = new_verts.mean(axis=0)
    radius   = float(np.max(np.linalg.norm(new_verts - centroid, axis=1)))

    m             = copy.copy(mesh)
    m.vertices    = new_verts.astype(np.float32)
    m.normals     = new_normals.astype(np.float32)
    m.centroid    = centroid.astype(np.float32)
    m.radius      = max(radius, 1e-6)
    return m


def _rot_from_vecs(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Rotation matrix that rotates unit vector a onto unit vector b."""
    a = a / (np.linalg.norm(a) + 1e-12)
    b = b / (np.linalg.norm(b) + 1e-12)
    v = np.cross(a, b)
    c = float(np.dot(a, b))

    if c < -1.0 + 1e-8:
        perp = np.array([1.0, 0.0, 0.0])
        if abs(np.dot(a, perp)) > 0.9:
            perp = np.array([0.0, 1.0, 0.0])
        axis = np.cross(a, perp)
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)

    s  = np.linalg.norm(v)
    vx = np.array([[ 0,    -v[2],  v[1]],
                   [ v[2],  0,    -v[0]],
                   [-v[1],  v[0],  0   ]])
    return np.eye(3) + vx + vx @ vx * ((1 - c) / (s * s + 1e-12))
