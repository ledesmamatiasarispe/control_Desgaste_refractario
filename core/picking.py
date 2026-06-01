import numpy as np
from dataclasses import dataclass
from typing import Optional
from core.loader import MeshData


@dataclass
class PickResult:
    hit_point:    np.ndarray   # (3,) world position
    face_index:   int
    vertex_index: int
    distance:     float


def ray_cast(origin: np.ndarray, direction: np.ndarray,
             mesh: MeshData) -> Optional[PickResult]:
    if mesh.is_point_cloud or mesh.faces is None:
        return _nearest_point(origin, direction, mesh)

    v0 = mesh.vertices[mesh.faces[:, 0]]
    v1 = mesh.vertices[mesh.faces[:, 1]]
    v2 = mesh.vertices[mesh.faces[:, 2]]

    edge1 = v1 - v0
    edge2 = v2 - v0

    h   = np.cross(direction, edge2)
    a   = np.einsum('ij,ij->i', edge1, h)

    # Use a scale-relative EPS so tiny meshes (mm/cm scale) are handled correctly.
    # |a| ~ edge_length^2; filtering by absolute 1e-7 fails for edges < 3e-4 m.
    EPS_PARALLEL = float(np.abs(a).max()) * 1e-8 if len(a) else 1e-30
    EPS_PARALLEL = max(EPS_PARALLEL, 1e-30)
    EPS_T        = mesh.radius * 1e-6   # forward-hit threshold

    valid = np.abs(a) > EPS_PARALLEL
    # safe 1/a — masked values never reach u/v/t checks
    f     = np.where(valid, 1.0 / np.where(valid, a, 1.0), 0.0)

    s = origin - v0
    u = f * np.einsum('ij,ij->i', s, h)
    valid &= (u >= 0.0) & (u <= 1.0)

    q = np.cross(s, edge1)
    v = f * (q @ direction)
    valid &= (v >= 0.0) & (u + v <= 1.0)

    t = f * np.einsum('ij,ij->i', edge2, q)
    valid &= t > EPS_T

    if not np.any(valid):
        return None

    t_vals    = np.where(valid, t, np.inf)
    best_face = int(np.argmin(t_vals))
    best_t    = float(t_vals[best_face])

    if np.isinf(best_t):
        return None

    hit_point  = (origin + direction * best_t).astype(np.float32)
    face_verts = mesh.faces[best_face]
    dists      = np.linalg.norm(mesh.vertices[face_verts] - hit_point, axis=1)
    best_vert  = int(face_verts[np.argmin(dists)])

    return PickResult(hit_point=hit_point, face_index=best_face,
                      vertex_index=best_vert, distance=best_t)


def _nearest_point(origin: np.ndarray, direction: np.ndarray,
                   mesh: MeshData) -> Optional[PickResult]:
    oc = mesh.vertices - origin
    t  = np.maximum(np.dot(oc, direction), 0.0)
    closest = origin + t[:, None] * direction
    dists   = np.linalg.norm(mesh.vertices - closest, axis=1)
    thresh  = mesh.radius * 0.02
    if dists.min() > thresh:
        return None
    best = int(np.argmin(dists))
    return PickResult(hit_point=mesh.vertices[best], face_index=-1,
                      vertex_index=best, distance=float(t[best]))
