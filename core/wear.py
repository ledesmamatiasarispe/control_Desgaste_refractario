import numpy as np
from scipy.spatial import KDTree
from dataclasses import dataclass
from core.loader import MeshData


@dataclass
class WearResult:
    distances: np.ndarray  # (N,) float32, per-vertex on current mesh
    max_wear:  float
    mean_wear: float
    p95_wear:  float


def compute_wear(reference: MeshData, current: MeshData) -> WearResult:
    """
    Per-vertex distance from each vertex of current to the nearest point
    on the reference mesh (or nearest vertex if rtree is not available).
    """
    try:
        import rtree  # noqa: F401
        distances = _proximity(reference, current)
    except ImportError:
        distances = _kdtree(reference, current)

    d = distances.astype(np.float32)
    return WearResult(
        distances=d,
        max_wear=float(d.max()),
        mean_wear=float(d.mean()),
        p95_wear=float(np.percentile(d, 95)),
    )


def _kdtree(ref: MeshData, cur: MeshData) -> np.ndarray:
    tree = KDTree(ref.vertices)
    dists, _ = tree.query(cur.vertices, workers=-1)
    return dists.astype(np.float32)


def _proximity(ref: MeshData, cur: MeshData) -> np.ndarray:
    import trimesh
    ref_mesh = trimesh.Trimesh(vertices=ref.vertices, faces=ref.faces, process=False)
    _, dists, _ = trimesh.proximity.closest_point(ref_mesh, cur.vertices)
    return np.array(dists, dtype=np.float32)
