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


# ── sparse-only (preview) ─────────────────────────────────────────────────────

def reconstruct_sparse(
    image_folder: str,
    imu_file: str,
    output_dir: str,
    progress_cb: Callable[[int, str], None],
) -> str:
    """
    Run only SfM (feature extraction + matching + incremental mapping).
    Returns path to sparse point cloud PLY. Fast (~1 min on CPU).
    """
    import pycolmap

    img_path    = pathlib.Path(image_folder)
    out_path    = pathlib.Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    db_path     = out_path / "database.db"
    sparse_path = out_path / "sparse"
    sparse_path.mkdir(exist_ok=True)

    imu_data = _load_imu(imu_file)

    progress_cb(5, "Configurando cámara…")
    reader_opts = pycolmap.ImageReaderOptions()
    focal_px = _median_focal(imu_data)
    if focal_px:
        width  = imu_data.get("width",  1920)
        height = imu_data.get("height", 1080)
        reader_opts.default_focal_length_factor = focal_px / max(width, height)

    progress_cb(8, "Seleccionando mejores fotos…")
    all_jpgs  = sorted([f.name for f in img_path.glob("*.jpg")])
    jpg_names = _select_best_frames(img_path, all_jpgs, imu_data, max_frames=200)
    log.info(f"Frame selection: {len(all_jpgs)} → {len(jpg_names)} frames")

    progress_cb(12, "Extrayendo características SIFT…")
    extraction_opts = pycolmap.FeatureExtractionOptions()
    extraction_opts.sift.max_num_features = 16384
    extraction_opts.max_image_size        = 3200
    extraction_opts.num_threads           = 2
    pycolmap.extract_features(
        db_path, img_path,
        image_names=jpg_names,
        reader_options=reader_opts,
        extraction_options=extraction_opts,
    )

    # Matching exhaustivo para < 150 fotos (compara todas contra todas → más correspondencias)
    # Secuencial para más fotos (exhaustivo sería demasiado lento)
    progress_cb(30, "Buscando correspondencias…")
    if len(jpg_names) <= 150:
        progress_cb(30, "Buscando correspondencias (exhaustivo)…")
        pycolmap.match_exhaustive(db_path)
    else:
        progress_cb(30, "Buscando correspondencias (secuencial)…")
        seq_opts = pycolmap.SequentialPairingOptions()
        seq_opts.overlap        = 30
        seq_opts.loop_detection = False
        pycolmap.match_sequential(db_path, pairing_options=seq_opts)

    progress_cb(50, "Reconstruyendo estructura dispersa (SfM)…")
    maps = pycolmap.incremental_mapping(db_path, img_path, sparse_path)
    if not maps:
        raise RuntimeError("SfM no encontró suficientes correspondencias.")

    best_recon = max(maps.values(), key=lambda r: len(r.images))
    best_recon.write(str(sparse_path / "0"))
    log.info(f"SfM preview: {len(best_recon.images)} images, {len(best_recon.points3D)} pts")

    progress_cb(80, "Exportando nube de puntos…")
    ply_path = str(out_path / "sparse.ply")

    # Extraer colores en paralelo mientras se exporta el PLY
    import threading
    color_done = threading.Event()
    def _extract_colors():
        try:
            best_recon.extract_colors_for_all_images(str(img_path))
            _export_sparse_ply(best_recon, ply_path)   # re-exportar con colores reales
            log.info("Colors extracted and PLY updated")
        except Exception as e:
            log.warning(f"Color extraction failed: {e}")
        finally:
            color_done.set()

    # Exportar sin colores primero (rápido) para que el cliente pueda empezar a descargar
    _export_sparse_ply(best_recon, ply_path)
    threading.Thread(target=_extract_colors, daemon=True).start()

    progress_cb(100, f"✓ Nube lista — {len(best_recon.points3D):,} puntos")
    return ply_path


def reconstruct_dense(
    image_folder: str,
    output_dir: str,
    progress_cb: Callable[[int, str], None],
) -> str:
    """
    Run MVS + meshing from an existing SfM sparse reconstruction in output_dir.
    Returns path to mesh OBJ.
    """
    import pycolmap

    img_path    = pathlib.Path(image_folder)
    out_path    = pathlib.Path(output_dir)
    sparse_path = out_path / "sparse" / "0"
    dense_path  = out_path / "dense"
    dense_path.mkdir(exist_ok=True)

    # Load the already-computed reconstruction
    recon = pycolmap.Reconstruction(str(sparse_path))
    obj_path = str(out_path / "mesh.obj")

    try:
        progress_cb(5, "Densificando nube de puntos (MVS)…")
        _dense_pipeline(recon, img_path, dense_path, progress_cb)
        obj_path = _poisson_mesh(dense_path, out_path, progress_cb)
    except Exception as e:
        log.warning(f"Dense pipeline failed ({e}), using sparse fallback")
        progress_cb(50, "MVS falló — generando malla desde nube dispersa…")
        obj_path = _mesh_from_sparse(recon, out_path, progress_cb)

    progress_cb(100, "✓ Malla completa lista")
    return obj_path


def _select_best_frames(
    img_path: pathlib.Path,
    jpg_names: list,
    imu_data: dict,
    max_frames: int = 200,
) -> list:
    """
    Elige las mejores fotos de entre todas las capturadas:
    1. Filtra fotos movidas (gyro alto en IMU)
    2. Ordena por nitidez (varianza del Laplaciano)
    3. Selecciona hasta max_frames con máxima cobertura angular
    """
    if len(jpg_names) <= max_frames:
        return jpg_names   # ya son pocas, usar todas

    # Construir índice de datos IMU por frame_id
    frames_meta = {f.get("frame_id"): f for f in imu_data.get("frames", [])}
    GYRO_MAX = 0.8   # rad/s — descartar frames muy movidos

    # Calcular nitidez con PIL + numpy (sin cv2)
    def sharpness(img_file: pathlib.Path) -> float:
        try:
            from PIL import Image as PILImage
            img = PILImage.open(img_file).convert("L")
            # Reducir para velocidad
            img = img.resize((320, 240), PILImage.BILINEAR)
            arr = np.array(img, dtype=np.float32)
            # Laplaciano manual
            lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
                   - 4 * arr[1:-1, 1:-1])
            return float(lap.var())
        except Exception:
            return 0.0

    scored = []
    for name in jpg_names:
        # Frame ID desde nombre de archivo (e.g. "00042.jpg" → 42)
        try:
            fid = int(pathlib.Path(name).stem)
        except ValueError:
            fid = -1

        meta  = frames_meta.get(fid, {})
        imu   = meta.get("imu", {})
        gyro  = imu.get("gyro", [0, 0, 0])
        gyro_mag = float(np.sqrt(sum(g*g for g in gyro))) if gyro else 0.0

        if gyro_mag > GYRO_MAX:
            continue   # descartado por movimiento

        sharp = sharpness(img_path / name)
        orient = imu.get("orient", [0.0, 0.0, 0.0])
        scored.append((name, sharp, orient))

    if not scored:
        log.warning("All frames filtered by gyro — using all frames")
        return jpg_names

    # Si aún sobran, seleccionar con máxima cobertura angular
    if len(scored) <= max_frames:
        return [s[0] for s in scored]

    # Ordenar por nitidez descendente, tomar el top 2×max y luego filtrar por cobertura
    scored.sort(key=lambda x: x[1], reverse=True)
    candidates = scored[:max_frames * 2]

    # Greedy: elegir frames que maximicen la distancia angular al más cercano ya elegido
    selected = [candidates[0]]
    remaining = candidates[1:]
    while len(selected) < max_frames and remaining:
        best_idx, best_dist = 0, -1.0
        for i, (name, sharp, orient) in enumerate(remaining):
            # Distancia mínima al frame más cercano ya seleccionado
            min_d = min(
                abs(orient[0] - s[2][0]) + abs(orient[1] - s[2][1])
                for s in selected
            )
            score = min_d + sharp * 0.001   # ponderar levemente por nitidez
            if score > best_dist:
                best_dist, best_idx = score, i
        selected.append(remaining.pop(best_idx))

    result = sorted([s[0] for s in selected])
    log.info(f"Selected {len(result)} frames (gyro-filtered: {len(scored)}, sharpness+coverage)")
    return result


def _smooth_and_fill(mesh):
    """Suavizar la malla y rellenar huecos pequeños."""
    import trimesh.smoothing
    try:
        # Eliminar triángulos degenerados y componentes desconectados pequeños
        mesh.remove_degenerate_faces()
        mesh.remove_duplicate_faces()
        mesh.remove_unreferenced_vertices()

        # Quedarse solo con el componente conexo más grande
        components = mesh.split(only_watertight=False)
        if components:
            mesh = max(components, key=lambda m: len(m.faces))

        # Suavizado Laplaciano (reduce spikes sin destruir la forma)
        trimesh.smoothing.filter_laplacian(mesh, lamb=0.5, iterations=5)

        log.info(f"Smoothed mesh: {len(mesh.faces)} faces")
    except Exception as e:
        log.warning(f"Smooth/fill failed: {e}")
    return mesh


def _export_sparse_ply(recon, ply_path: str):
    """Export sparse point cloud as ASCII PLY with XYZ + RGB."""
    pts = list(recon.points3D.values())
    lines = [
        "ply",
        "format ascii 1.0",
        f"element vertex {len(pts)}",
        "property float x",
        "property float y",
        "property float z",
        "property uchar red",
        "property uchar green",
        "property uchar blue",
        "end_header",
    ]
    for p in pts:
        x, y, z = p.xyz
        c = p.color[:3]
        # pycolmap puede devolver uint8 (0-255) o float (0-1)
        if hasattr(c, 'dtype') and np.issubdtype(c.dtype, np.floating):
            r, g, b = int(c[0]*255), int(c[1]*255), int(c[2]*255)
        else:
            r, g, b = int(c[0]), int(c[1]), int(c[2])
        lines.append(f"{x:.6f} {y:.6f} {z:.6f} {r} {g} {b}")
    pathlib.Path(ply_path).write_text("\n".join(lines))


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
    cb(8, "Seleccionando mejores fotos…")
    all_jpgs  = sorted([f.name for f in image_path.glob("*.jpg")])
    jpg_names = _select_best_frames(image_path, all_jpgs, imu_data, max_frames=200)
    log.info(f"Frame selection: {len(all_jpgs)} → {len(jpg_names)} frames")

    cb(12, "Extrayendo características SIFT…")
    extraction_opts = pycolmap.FeatureExtractionOptions()
    extraction_opts.sift.max_num_features = 16384
    extraction_opts.max_image_size        = 3200
    extraction_opts.num_threads           = 2
    pycolmap.extract_features(
        db_path, image_path,
        image_names=jpg_names,
        reader_options=reader_opts,
        extraction_options=extraction_opts,
    )

    # Exhaustivo para ≤150 fotos, secuencial para más
    if len(jpg_names) <= 150:
        cb(25, "Buscando correspondencias (exhaustivo)…")
        pycolmap.match_exhaustive(db_path)
    else:
        cb(25, "Buscando correspondencias (secuencial)…")
        seq_opts = pycolmap.SequentialPairingOptions()
        seq_opts.overlap        = 30
        seq_opts.loop_detection = False
        pycolmap.match_sequential(db_path, pairing_options=seq_opts)

    # ── 4. Sparse SfM ──
    cb(40, "Reconstruyendo estructura dispersa (SfM)…")
    maps = pycolmap.incremental_mapping(db_path, image_path, sparse_path)
    if not maps:
        raise RuntimeError("SfM no encontró suficientes correspondencias. "
                           "Intentá con más fotos o mejor iluminación.")

    # Use the reconstruction with most registered images
    best_recon = max(maps.values(), key=lambda r: len(r.images))
    try:
        best_recon.extract_colors_for_all_images(str(image_path))
    except Exception as e:
        log.warning(f"Color extraction failed: {e}")
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

    # ── 6. Back-project alignment points from phone ──
    try:
        align_pts = imu_data.get("align_pts", [])
        if len(align_pts) >= 3:
            cb(98, "Calculando puntos de alineación 3D…")
            pts_3d = _backproject_align_points(best_recon, align_pts, obj_path)
            if pts_3d:
                import json
                align_json = out_path / "align_pts.json"
                align_json.write_text(json.dumps([p.tolist() for p in pts_3d]))
                log.info(f"Align points saved: {align_json}")
    except Exception as e:
        log.warning(f"Could not compute align points: {e}")

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
    """Convert sparse point cloud → surface mesh using alpha shapes (CPU, no GPU needed)."""
    import trimesh
    from scipy.spatial import Delaunay

    cb(80, "Generando malla desde nube dispersa (alpha shape)…")

    pts = np.array([p.xyz for p in recon.points3D.values()], dtype=np.float64)
    if len(pts) < 10:
        raise RuntimeError("Muy pocos puntos en la nube dispersa.")

    log.info(f"Sparse cloud: {len(pts)} points")
    obj_path = str(out_path / "mesh.obj")

    # Compute adaptive alpha from average nearest-neighbor distance
    try:
        from scipy.spatial import KDTree
        tree = KDTree(pts)
        dists, _ = tree.query(pts, k=2)
        avg_nn = float(np.median(dists[:, 1]))
        alpha  = avg_nn * 8.0   # generous radius to connect sparse points
        log.info(f"Alpha shape: avg_nn={avg_nn:.4f}, alpha={alpha:.4f}")

        # 3D Delaunay → filter tetrahedra by circumradius < alpha → extract surface
        tri = Delaunay(pts)
        tetra = pts[tri.simplices]   # (N, 4, 3)

        # Circumradius of each tetrahedron
        def _circumradius(t):
            a, b, c, d = t
            A = np.array([b-a, c-a, d-a])
            b_ = 0.5 * np.array([np.dot(b-a, b-a),
                                  np.dot(c-a, c-a),
                                  np.dot(d-a, d-a)])
            try:
                x = np.linalg.solve(A, b_)
                return np.linalg.norm(x)
            except np.linalg.LinAlgError:
                return np.inf

        radii  = np.array([_circumradius(t) for t in tetra])
        keep   = tri.simplices[radii < alpha]

        if len(keep) > 0:
            # Extract boundary faces (appear exactly once across all tetrahedra)
            from collections import Counter
            face_count = Counter()
            for tet in keep:
                for face in [(tet[0],tet[1],tet[2]),
                             (tet[0],tet[1],tet[3]),
                             (tet[0],tet[2],tet[3]),
                             (tet[1],tet[2],tet[3])]:
                    face_count[tuple(sorted(face))] += 1
            boundary = [f for f, c in face_count.items() if c == 1]

            if len(boundary) > 0:
                faces = np.array(boundary, dtype=np.int64)
                mesh  = trimesh.Trimesh(vertices=pts, faces=faces, process=True)
                if len(mesh.faces) > 0:
                    # Suavizar y rellenar huecos
                    mesh = _smooth_and_fill(mesh)
                    cb(92, f"Exportando malla ({len(mesh.faces):,} caras)…")
                    mesh.export(obj_path)
                    return obj_path

    except Exception as e:
        log.warning(f"Alpha shape failed: {e} — using convex hull")

    # Last resort: convex hull
    cb(90, "Usando convex hull como último recurso…")
    mesh = trimesh.PointCloud(pts.astype(np.float32)).convex_hull
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


# ── alignment point back-projection ──────────────────────────────────────────

def _backproject_align_points(reconstruction, align_pts: list, mesh_obj_path: str):
    """
    For each alignment point {frame_id, px, py} from the phone:
    - Find the COLMAP image with that frame name
    - Get camera pose (R, t) and intrinsics
    - Cast ray from camera center through pixel (px, py)
    - Intersect ray with the reconstructed mesh
    Returns list of 3 np.ndarray 3D points, or None if fewer than 3 found.
    """
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    import trimesh
    from core.picking import ray_cast
    from core.loader import MeshData

    # Load reconstructed mesh for ray casting
    try:
        mesh = trimesh.load(mesh_obj_path, force='mesh', process=False)
        if isinstance(mesh, trimesh.Scene):
            mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
        verts  = np.array(mesh.vertices, dtype=np.float32)
        faces  = np.array(mesh.faces,    dtype=np.uint32)
    except Exception as e:
        log.warning(f"Could not load mesh for backprojection: {e}")
        return None

    # Build minimal MeshData for ray_cast
    centroid = verts.mean(axis=0)
    radius   = float(np.max(np.linalg.norm(verts - centroid, axis=1)))
    mesh_data = MeshData(
        vertices=verts, faces=faces,
        normals=np.zeros_like(verts), colors=np.zeros((len(verts),4), np.float32),
        centroid=centroid, radius=radius,
        is_point_cloud=False, source_path=mesh_obj_path,
        vertex_count=len(verts), face_count=len(faces)
    )

    # Build lookup: frame name → COLMAP image
    name_to_img = {img.name: img for img in reconstruction.images.values()}

    pts_3d = []
    for ap in sorted(align_pts, key=lambda x: x.get("index", 0)):
        frame_id = ap.get("frame_id")
        px = ap.get("px", 0)
        py = ap.get("py", 0)
        frame_name = f"{frame_id:05d}.jpg"

        img = name_to_img.get(frame_name)
        if img is None:
            log.warning(f"Align point frame {frame_name} not in reconstruction")
            continue

        cam = reconstruction.cameras[img.camera_id]
        # Get intrinsics (SIMPLE_RADIAL: [f, cx, cy, k])
        params = cam.params
        f  = float(params[0])
        cx = float(params[1]) if len(params) > 1 else cam.width  / 2.0
        cy = float(params[2]) if len(params) > 2 else cam.height / 2.0

        # Direction in camera space (normalized)
        d_cam = np.array([(px - cx) / f, (py - cy) / f, 1.0], dtype=np.float64)
        d_cam /= np.linalg.norm(d_cam)

        # Camera pose: cam_from_world → world_from_cam
        R = np.array(img.cam_from_world.rotation.matrix(), dtype=np.float64)
        t = np.array(img.cam_from_world.translation, dtype=np.float64)
        R_inv   = R.T
        origin  = (-R_inv @ t).astype(np.float32)
        direction = (R_inv @ d_cam).astype(np.float32)
        direction /= np.linalg.norm(direction)

        result = ray_cast(origin, direction, mesh_data)
        if result is not None:
            pts_3d.append(result.hit_point)
            log.info(f"Align pt {ap.get('index')}: {result.hit_point}")
        else:
            log.warning(f"No mesh intersection for align pt {ap.get('index')}")

    return pts_3d if len(pts_3d) >= 3 else None
