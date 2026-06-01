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

    return _apply(mesh, transform, scale=scale)


def icp_align(source: MeshData, target: MeshData) -> MeshData:
    """Align source to target using centroid pre-align + ICP."""
    import trimesh

    offset    = (target.centroid - source.centroid).astype(np.float64)
    pre       = np.eye(4)
    pre[:3, 3] = offset
    src_verts = (source.vertices + offset).astype(np.float32)

    matrix, _, _ = trimesh.registration.icp(
        src_verts, target.vertices,
        max_iterations=100, threshold=1e-6,
    )
    transform = matrix @ pre
    return _apply(source, transform)


# ── internals ────────────────────────────────────────────────────────────────

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
