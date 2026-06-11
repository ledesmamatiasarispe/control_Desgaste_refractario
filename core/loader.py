import os
import zipfile
import tempfile
import numpy as np
import trimesh
from dataclasses import dataclass
from typing import Optional
from PIL import Image


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

_TEX_DEFAULT_COLOR = [0.72, 0.78, 0.85, 1.0]
_TEX_MAX_DIM       = 2048   # downscale huge textures before sampling


def _bake_texture_colors(mesh: trimesh.Trimesh, tex_cache: dict) -> Optional[np.ndarray]:
    """Bake a mesh's texture/material into per-vertex RGBA colors (float32, 0-1).

    Returns None if the mesh carries no usable texture/material/vertex-color info,
    so the caller can fall back to the default flat color.
    """
    visual = mesh.visual
    n = len(mesh.vertices)
    try:
        if isinstance(visual, trimesh.visual.texture.TextureVisuals):
            uv       = visual.uv
            material = visual.material
            image    = getattr(material, 'baseColorTexture', None)

            if uv is not None and image is not None and len(uv) == n:
                key = id(image)
                arr = tex_cache.get(key)
                if arr is None:
                    img = image.convert('RGBA')
                    if max(img.size) > _TEX_MAX_DIM:
                        scale = _TEX_MAX_DIM / max(img.size)
                        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
                        img = img.resize(new_size, Image.BILINEAR)
                    arr = np.asarray(img, dtype=np.float32) / 255.0
                    tex_cache[key] = arr
                h, w = arr.shape[:2]
                u = np.mod(uv[:, 0], 1.0)
                v = np.mod(uv[:, 1], 1.0)
                px = np.clip((u * (w - 1)).round().astype(np.int64), 0, w - 1)
                py = np.clip(((1.0 - v) * (h - 1)).round().astype(np.int64), 0, h - 1)
                colors = arr[py, px].copy()
                colors[:, 3] = 1.0
                return colors.astype(np.float32)

            factor = getattr(material, 'baseColorFactor', None)
            if factor is not None:
                rgba = np.asarray(factor, dtype=np.float32)
                if rgba.max() > 1.0:
                    rgba = rgba / 255.0
                rgba = rgba.copy()
                rgba[3] = 1.0
                return np.tile(rgba, (n, 1)).astype(np.float32)

        vc = getattr(visual, 'vertex_colors', None)
        if vc is not None and len(vc) == n:
            colors = np.asarray(vc, dtype=np.float32) / 255.0
            colors[:, 3] = 1.0
            return colors.astype(np.float32)
    except Exception:
        pass
    return None


def _load_mesh(path: str) -> MeshData:
    loaded = trimesh.load(path, process=False)

    tex_cache: dict = {}
    baked_colors: Optional[np.ndarray] = None

    if isinstance(loaded, trimesh.Scene):
        geoms = [g for g in loaded.geometry.values()
                 if isinstance(g, trimesh.Trimesh)]
        if not geoms:
            raise ValueError(f"Scene has no triangle geometry: {path}")
        if len(geoms) == 1:
            mesh = geoms[0]
            baked_colors = _bake_texture_colors(mesh, tex_cache)
        else:
            # Concatenate manually so each part keeps its own baked
            # texture/material color — trimesh's concatenate can't merge
            # several distinct textures into a single TextureVisuals.
            all_v, all_f, all_c = [], [], []
            offset = 0
            for g in geoms:
                v = np.array(g.vertices, dtype=np.float64)
                f = np.array(g.faces, dtype=np.int64) + offset
                all_v.append(v)
                all_f.append(f)
                c = _bake_texture_colors(g, tex_cache)
                if c is None:
                    c = np.full((len(v), 4), _TEX_DEFAULT_COLOR, dtype=np.float32)
                all_c.append(c)
                offset += len(v)
            mesh = trimesh.Trimesh(
                vertices=np.concatenate(all_v, axis=0),
                faces=np.concatenate(all_f, axis=0),
                process=False,
            )
            baked_colors = np.concatenate(all_c, axis=0)

    elif isinstance(loaded, trimesh.PointCloud):
        return _pointcloud_data(np.array(loaded.vertices, dtype=np.float32), path)

    elif isinstance(loaded, trimesh.Trimesh):
        mesh = loaded
        baked_colors = _bake_texture_colors(mesh, tex_cache)

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

    if baked_colors is not None and len(baked_colors) == len(vertices):
        colors = baked_colors.astype(np.float32)
    else:
        colors = np.full((len(vertices), 4), _TEX_DEFAULT_COLOR, dtype=np.float32)

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
