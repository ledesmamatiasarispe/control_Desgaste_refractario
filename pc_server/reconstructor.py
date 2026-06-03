"""
3D reconstruction backend.

Priority order:
  1. pycolmap (COLMAP, installed)
  2. Meshroom CLI (if found on PATH or common locations)
  3. Raises RuntimeError with clear message
"""

import json
import logging
import pathlib
import shutil
import subprocess
import tempfile
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)


# ── public entry point ───────────────────────────────────────────────────────

def reconstruct(
    image_folder: str,
    imu_file: str,
    output_dir: str,
    progress_cb: Callable[[int, str], None],
) -> str:
    """
    Run 3D reconstruction from images + IMU data.

    Returns path to the output OBJ file.
    Raises RuntimeError on failure.
    """
    img_path = pathlib.Path(image_folder)
    out_path = pathlib.Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    imu_data = _load_imu(imu_file)

    # Try pycolmap first
    try:
        return _reconstruct_colmap(img_path, imu_data, out_path, progress_cb)
    except Exception as e:
        log.warning(f"pycolmap failed: {e} — trying Meshroom")

    # Try Meshroom
    meshroom_exe = _find_meshroom()
    if meshroom_exe:
        try:
            return _reconstruct_meshroom(meshroom_exe, img_path, out_path, progress_cb)
        except Exception as e:
            log.warning(f"Meshroom failed: {e}")

    raise RuntimeError(
        "No se pudo reconstruir. Verificá que pycolmap esté instalado "
        "o que Meshroom esté disponible en el PATH."
    )


# ── pycolmap pipeline ─────────────────────────────────────────────────────────

def _reconstruct_colmap(
    image_path: pathlib.Path,
    imu_data: dict,
    out_path: pathlib.Path,
    cb: Callable,
) -> str:
    import pycolmap

    db_path     = out_path / "database.db"
    sparse_path = out_path / "sparse"
    dense_path  = out_path / "dense"
    sparse_path.mkdir(exist_ok=True)
    dense_path.mkdir(exist_ok=True)

    # ── 1. Camera priors from IMU ──
    cb(5, "Configurando intrínsecos de cámara…")
    reader_opts = pycolmap.ImageReaderOptions()
    focal_px = _median_focal(imu_data)
    if focal_px:
        # Express focal as fraction of max dimension for COLMAP
        width  = imu_data.get("width",  1920)
        height = imu_data.get("height", 1080)
        max_dim = max(width, height)
        reader_opts.default_focal_length_factor = focal_px / max_dim
        log.info(f"Using focal prior: {focal_px:.1f} px")

    # ── 2. Feature extraction ──
    cb(10, "Extrayendo características SIFT…")
    extraction_opts = pycolmap.FeatureExtractionOptions()
    extraction_opts.sift.max_num_features = 8192  # was 2048 — more features for low-texture
    extraction_opts.max_image_size = 2400          # was 1600 — better quality, 2 threads is safe
    extraction_opts.num_threads   = 2              # keep low to avoid OOM crash
    # Pass only JPG filenames — the images/ folder also has .json metadata files
    jpg_names = sorted([f.name for f in image_path.glob("*.jpg")])
    pycolmap.extract_features(
        db_path, image_path,
        image_names=jpg_names,
        reader_options=reader_opts,
        extraction_options=extraction_opts,
    )

    # ── 3. Sequential matching (better than exhaustive for cylindrical/symmetric objects) ──
    # Exhaustive matching causes wrong correspondences in symmetric furnaces
    # (frame 5 matches frame 45 pointing at the same side → wrong camera poses)
    # Sequential matching only compares each frame with its N temporal neighbors
    cb(25, "Buscando correspondencias secuenciales…")
    seq_opts = pycolmap.SequentialPairingOptions()
    seq_opts.overlap         = 20    # compare each frame with its 20 temporal neighbors
    seq_opts.loop_detection  = False # vocab tree not available; disable loop closure
    pycolmap.match_sequential(db_path, options=seq_opts)

    # ── 4. Sparse SfM ──
    cb(40, "Reconstruyendo estructura dispersa (SfM)…")
    maps = pycolmap.incremental_mapping(db_path, image_path, sparse_path)
    if not maps:
        raise RuntimeError("SfM no encontró suficientes correspondencias. "
                           "Intentá con más fotos o mejor iluminación.")

    # Use the reconstruction with most registered images
    best_recon = max(maps.values(), key=lambda r: len(r.images))
    best_recon.write(str(sparse_path / "0"))
    log.info(f"SfM: {len(best_recon.images)} images, "
             f"{len(best_recon.points3D)} points")

    # ── 5. Dense MVS ──
    obj_path = str(out_path / "mesh.obj")
    try:
        cb(55, "Densificando nube de puntos (MVS)…")
        _dense_pipeline(best_recon, image_path, dense_path, cb)
        obj_path = _poisson_mesh(dense_path, out_path, cb)
    except Exception as e:
        log.warning(f"Dense pipeline failed ({e}), using sparse cloud")
        cb(75, "MVS falló — generando malla desde nube dispersa…")
        obj_path = _mesh_from_sparse(best_recon, out_path, cb)

    cb(100, "✓ Reconstrucción completada")
    return obj_path


def _dense_pipeline(
    recon,
    image_path: pathlib.Path,
    dense_path: pathlib.Path,
    cb: Callable,
):
    import pycolmap
    undist_path = dense_path / "undistorted"
    undist_path.mkdir(exist_ok=True)

    pycolmap.undistort_images(
        output_path=str(undist_path),
        input_path=str(dense_path.parent / "sparse" / "0"),
        image_path=str(image_path),
    )

    cb(62, "PatchMatch stereo…")
    pycolmap.patch_match_stereo(str(undist_path))

    cb(72, "Fusionando profundidades…")
    pycolmap.stereo_fusion(
        output_path=str(dense_path / "fused.ply"),
        workspace_path=str(undist_path),
    )


def _poisson_mesh(dense_path: pathlib.Path, out_path: pathlib.Path,
                  cb: Callable) -> str:
    import pycolmap
    fused_ply = dense_path / "fused.ply"
    mesh_ply  = out_path / "mesh.ply"
    obj_path  = str(out_path / "mesh.obj")

    cb(80, "Reconstrucción de superficie Poisson…")
    pycolmap.poisson_meshing(
        input_path=str(fused_ply),
        output_path=str(mesh_ply),
    )

    cb(92, "Convirtiendo a OBJ…")
    _ply_to_obj(str(mesh_ply), obj_path)
    return obj_path


def _mesh_from_sparse(recon, out_path: pathlib.Path, cb: Callable) -> str:
    """Convert sparse point cloud → surface mesh (alpha shape, better than convex hull)."""
    import trimesh

    cb(80, "Generando malla desde nube dispersa…")

    pts = np.array([p.xyz for p in recon.points3D.values()], dtype=np.float32)
    if len(pts) < 10:
        raise RuntimeError("Muy pocos puntos en la nube dispersa.")

    log.info(f"Sparse cloud: {len(pts)} points")
    obj_path = str(out_path / "mesh.obj")

    # Try Open3D Ball-Pivoting (best quality for sparse clouds)
    try:
        import open3d as o3d
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(pts.astype(np.float64))
        pcd.estimate_normals(search_param=o3d.geometry.KDTreeSearchParamHybrid(
            radius=0.05, max_nn=30))
        # Adaptive radii based on point cloud density
        dists = pcd.compute_nearest_neighbor_distance()
        avg_d = float(np.mean(dists))
        radii = [avg_d * r for r in (1.5, 3.0, 6.0)]
        mesh_o3d = o3d.geometry.TriangleMesh.create_from_point_cloud_ball_pivoting(
            pcd, o3d.utility.DoubleVector(radii))
        if len(mesh_o3d.triangles) > 0:
            cb(92, f"Exportando malla BPA ({len(mesh_o3d.triangles):,} caras)…")
            o3d.io.write_triangle_mesh(obj_path, mesh_o3d)
            return obj_path
    except ImportError:
        pass   # Open3D not installed — fall through to trimesh

    # Fallback: Poisson reconstruction via trimesh (better than convex hull)
    cb(85, "Reconstrucción Poisson desde nube dispersa…")
    pc = trimesh.PointCloud(pts)
    # Estimate normals pointing outward (assume centroid-centered furnace)
    centroid = pts.mean(axis=0)
    normals  = pts - centroid
    norms    = np.linalg.norm(normals, axis=1, keepdims=True)
    normals  = normals / (norms + 1e-8)

    try:
        mesh = trimesh.voxel.ops.points_to_marching_cubes(pts, pitch=0.02)
    except Exception:
        mesh = trimesh.PointCloud(pts).convex_hull   # last resort

    cb(92, "Exportando OBJ…")
    mesh.export(obj_path)
    return obj_path


# ── Meshroom fallback ─────────────────────────────────────────────────────────

def _find_meshroom() -> Optional[str]:
    for candidate in [
        "meshroom_photogrammetry",
        r"C:\Program Files\Meshroom\meshroom_photogrammetry.exe",
        r"C:\Meshroom\meshroom_photogrammetry.exe",
    ]:
        if shutil.which(candidate):
            return candidate
    return None


def _reconstruct_meshroom(
    exe: str,
    image_path: pathlib.Path,
    out_path: pathlib.Path,
    cb: Callable,
) -> str:
    cb(10, "Iniciando Meshroom…")
    result = subprocess.run(
        [exe, "--input", str(image_path), "--output", str(out_path)],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])

    # Find output mesh
    for ext in ["*.obj", "*.ply", "*.stl"]:
        matches = list(out_path.rglob(ext))
        if matches:
            cb(100, "✓ Meshroom completado")
            return str(matches[0])
    raise RuntimeError("Meshroom terminó pero no se encontró mesh de salida.")


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_imu(imu_file: str) -> dict:
    try:
        return json.loads(pathlib.Path(imu_file).read_text())
    except Exception:
        return {}


def _median_focal(imu_data: dict) -> Optional[float]:
    frames = imu_data.get("frames", [])
    focals = [f["camera"]["focal_px"] for f in frames
              if f.get("camera", {}).get("focal_px", 0) > 0]
    return float(np.median(focals)) if focals else None


def _ply_to_obj(ply_path: str, obj_path: str):
    import trimesh
    mesh = trimesh.load(ply_path, process=False)
    mesh.export(obj_path)
