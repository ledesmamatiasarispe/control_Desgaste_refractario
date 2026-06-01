import os
import zipfile
import tempfile
import numpy as np
import trimesh
from dataclasses import dataclass
from typing import Optional


@dataclass
class MeshData:
    vertices:      np.ndarray        # (N, 3) float32
    faces:         Optional[np.ndarray]  # (M, 3) uint32, None for point clouds
    normals:       np.ndarray        # (N, 3) float32
    colors:        np.ndarray        # (N, 4) float32
    centroid:      np.ndarray        # (3,) float32
    radius:        float
    is_point_cloud: bool
    source_path:   str
    vertex_count:  int
    face_count:    int


_MESH_EXTS  = {'.stl', '.glb', '.gltf', '.obj', '.ply', '.off', '.3mf'}
_CLOUD_EXTS = {'.xyz', '.pts', '.asc'}
_ZIP_EXTS   = {'.zip'}

SUPPORTED_FILTER = (
    "3D Files (*.stl *.glb *.gltf *.obj *.ply *.off *.3mf *.zip *.xyz *.pts);;"
    "STL (*.stl);;GLB/GLTF (*.glb *.gltf);;OBJ (*.obj);;"
    "PLY (*.ply);;ZIP (*.zip);;Point Cloud (*.xyz *.pts);;All Files (*)"
)


def load_file(path: str) -> MeshData:
    ext = os.path.splitext(path)[1].lower()
    if ext in _ZIP_EXTS:
        return _from_zip(path)
    if ext in _CLOUD_EXTS:
        return _load_cloud_txt(path)
    return _load_mesh(path)


# ── ZIP handling ─────────────────────────────────────────────────────────────

def _from_zip(zip_path: str) -> MeshData:
    with zipfile.ZipFile(zip_path, 'r') as z:
        names = z.namelist()

        # Inner ZIP?
        inner_zips = [n for n in names if n.lower().endswith('.zip')]
        mesh_files = [n for n in names
                      if os.path.splitext(n)[1].lower() in _MESH_EXTS]

        with tempfile.TemporaryDirectory() as tmp:
            z.extractall(tmp)

            if mesh_files:
                return _load_mesh(os.path.join(tmp, mesh_files[0]))

            if inner_zips:
                inner_path = os.path.join(tmp, inner_zips[0])
                return _from_zip(inner_path)

    raise ValueError(f"No supported mesh found in {zip_path}")


# ── mesh loading ─────────────────────────────────────────────────────────────

def _load_mesh(path: str) -> MeshData:
    loaded = trimesh.load(path, force='mesh', process=False)

    if isinstance(loaded, trimesh.Scene):
        geoms = [g for g in loaded.geometry.values()
                 if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise ValueError(f"Scene has no triangle geometry: {path}")
        mesh = trimesh.util.concatenate(geoms) if len(geoms) > 1 else geoms[0]

    elif isinstance(loaded, trimesh.PointCloud):
        return _pointcloud_data(np.array(loaded.vertices, dtype=np.float32), path)

    elif isinstance(loaded, trimesh.Trimesh):
        mesh = loaded

    else:
        raise ValueError(f"Unsupported trimesh type {type(loaded)} for {path}")

    if len(mesh.faces) == 0:
        return _pointcloud_data(np.array(mesh.vertices, dtype=np.float32), path)

    mesh.fix_normals()

    vertices = np.array(mesh.vertices,       dtype=np.float32)
    faces    = np.array(mesh.faces,          dtype=np.uint32)
    normals  = np.array(mesh.vertex_normals, dtype=np.float32)

    centroid = vertices.mean(axis=0)
    radius   = float(np.max(np.linalg.norm(vertices - centroid, axis=1)))

    colors = np.full((len(vertices), 4), [0.72, 0.78, 0.85, 1.0], dtype=np.float32)

    return MeshData(
        vertices=vertices, faces=faces, normals=normals, colors=colors,
        centroid=centroid, radius=max(radius, 1e-6),
        is_point_cloud=False,
        source_path=path,
        vertex_count=len(vertices),
        face_count=len(faces),
    )


# ── point cloud ──────────────────────────────────────────────────────────────

def _load_cloud_txt(path: str) -> MeshData:
    data = np.loadtxt(path, dtype=np.float32, comments=['#', '//'])
    if data.ndim == 1:
        data = data.reshape(1, -1)
    return _pointcloud_data(data[:, :3], path)


def _pointcloud_data(vertices: np.ndarray, path: str) -> MeshData:
    centroid = vertices.mean(axis=0)
    dists    = np.linalg.norm(vertices - centroid, axis=1)
    radius   = float(np.max(dists)) if len(dists) else 1.0

    normals = np.zeros((len(vertices), 3), dtype=np.float32)
    normals[:, 1] = 1.0
    colors  = np.full((len(vertices), 4), [0.35, 0.82, 0.60, 1.0], dtype=np.float32)

    return MeshData(
        vertices=vertices, faces=None, normals=normals, colors=colors,
        centroid=centroid, radius=max(radius, 1e-6),
        is_point_cloud=True,
        source_path=path,
        vertex_count=len(vertices),
        face_count=0,
    )
