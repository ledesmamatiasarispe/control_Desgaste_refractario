"""
Refractory Capture Server
Run:  python server.py  (default port 5005)

Android app connects to this server, uploads frames, and gets a reconstructed mesh.
"""

import argparse
import json
import logging
import pathlib
import socket
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Set

from flask import Flask, jsonify, request, send_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── job storage ───────────────────────────────────────────────────────────────

# Carpeta persistente para fotos y trabajos (sobrevive reinicios)
WORK_ROOT = pathlib.Path.home() / ".refractory_capture" / "jobs"
WORK_ROOT.mkdir(parents=True, exist_ok=True)

# Where finished meshes are copied for the desktop app
OUTPUT_DIR = pathlib.Path(r"D:\stl hornos\reconstructions")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── embedded-server hooks (set by desktop app when running embedded) ──────────
_on_mesh_ready_cb = None   # callback(path: str, name: str) called on job done


def set_mesh_ready_callback(cb):
    """Register a callback to be called when a mesh is ready. Thread-safe."""
    global _on_mesh_ready_cb
    _on_mesh_ready_cb = cb


def set_output_dir(path: str):
    """Change where finished meshes are saved (call before jobs start)."""
    global OUTPUT_DIR
    OUTPUT_DIR = pathlib.Path(path)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"OUTPUT_DIR → {OUTPUT_DIR}")


def set_work_root(path: str):
    """Change where temp job folders (photos, SIFT db) are created."""
    global WORK_ROOT
    WORK_ROOT = pathlib.Path(path)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    log.info(f"WORK_ROOT → {WORK_ROOT}")


MAX_JOBS = 5

@dataclass
class Job:
    job_id:          str
    status:          str          = "waiting"   # waiting/uploading/running/preview_done/done/error
    progress:        int          = 0
    message:         str          = ""
    received_frames: Set[int]     = field(default_factory=set)
    total_frames:    int          = 0
    output_path:     Optional[str] = None
    sparse_ply:      Optional[str] = None   # path to sparse point cloud PLY
    error:           Optional[str] = None
    created_at:      float        = field(default_factory=lambda: __import__('time').time())

    def to_dict(self):
        d = asdict(self)
        d["received_frames"] = sorted(self.received_frames)
        return d


_jobs: Dict[str, Job] = {}
_lock = threading.Lock()


def _load_existing_jobs():  # noqa — called at end of module setup
    """Escanea WORK_ROOT al arrancar y reconstruye los jobs que haya en disco."""
    for job_dir in WORK_ROOT.iterdir():
        if not job_dir.is_dir():
            continue
        jid = job_dir.name
        images = list((job_dir / "images").glob("*.jpg")) if (job_dir / "images").exists() else []
        if not images:
            continue
        # Determinar estado
        mesh = job_dir / "output" / "mesh.obj"
        ply  = job_dir / "output" / "sparse.ply"
        imu  = job_dir / "imu_summary.json"
        if mesh.exists():
            status = "done"
            out_path = str(mesh)
        elif ply.exists():
            status = "preview_done"
            out_path = None
        elif imu.exists():
            status = "uploading"
            out_path = None
        else:
            status = "waiting"
            out_path = None

        job = Job(
            job_id         = jid,
            status         = status,
            progress       = 100 if status in ("done", "preview_done") else 0,
            message        = f"Cargado desde disco ({len(images)} fotos)",
            received_frames= set(range(len(images))),
            total_frames   = len(images),
            output_path    = out_path,
            sparse_ply     = str(ply) if ply.exists() else None,
            created_at     = job_dir.stat().st_mtime,
        )
        with _lock:
            _jobs[jid] = job
        log.info(f"Loaded existing job {jid} ({status}, {len(images)} frames)")


def _job(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(job_id)


_load_existing_jobs()   # poblar _jobs con lo que haya en disco al arrancar


def _new_job() -> Job:
    import shutil
    jid = str(uuid.uuid4())[:8]
    job = Job(job_id=jid)
    with _lock:
        # Si ya hay MAX_JOBS, borrar el más viejo
        if len(_jobs) >= MAX_JOBS:
            oldest = min(_jobs.values(), key=lambda j: j.created_at)
            del _jobs[oldest.job_id]
            old_dir = WORK_ROOT / oldest.job_id
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
            log.info(f"Job limit reached — evicted oldest job {oldest.job_id}")
        _jobs[jid] = job
    work_dir = WORK_ROOT / jid
    (work_dir / "images").mkdir(parents=True, exist_ok=True)
    return job


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/ping")
def ping():
    return jsonify({"ok": True, "server": "Refractory Capture Server 1.0",
                    "hostname": socket.gethostname()})


@app.post("/new_job")
def new_job():
    job = _new_job()
    log.info(f"New job: {job.job_id}")
    return jsonify({"job_id": job.job_id})


@app.post("/upload_frame/<job_id>/<int:frame_id>")
def upload_frame(job_id: str, frame_id: int):
    job = _job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    if job.status not in ("waiting", "uploading", "preview_done"):
        return jsonify({"error": f"job is {job.status}, not accepting frames"}), 400

    # Save image
    image_file = request.files.get("image")
    if image_file is None:
        return jsonify({"error": "no image field"}), 400

    img_path = WORK_ROOT / job_id / "images" / f"{frame_id:05d}.jpg"
    image_file.save(str(img_path))

    # Save IMU + camera metadata
    meta_str = request.form.get("meta", "{}")
    meta_path = WORK_ROOT / job_id / "images" / f"{frame_id:05d}.json"
    meta_path.write_text(meta_str)

    with _lock:
        job.status = "uploading"   # reset preview_done if user adds more photos
        job.received_frames.add(frame_id)

    log.debug(f"Job {job_id}: received frame {frame_id}")
    return jsonify({"ok": True, "received": len(job.received_frames)})


@app.get("/received_frames/<job_id>")
def received_frames(job_id: str):
    job = _job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"frames": sorted(job.received_frames)})


@app.post("/start_reconstruct/<job_id>")
def start_reconstruct(job_id: str):
    job = _job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    if job.status == "running":
        return jsonify({"ok": True, "message": "already running"})

    data = request.get_json(silent=True) or {}
    job.total_frames = data.get("total_frames", len(job.received_frames))
    mode = data.get("mode", "full")   # preview_done jobs can be re-run with more frames   # "preview" → solo SfM | "full" → SfM + MVS + malla

    if len(job.received_frames) < 5:
        return jsonify({"error": "se necesitan al menos 5 fotogramas"}), 400

    align_pts = data.get("align_pts", [])
    _build_imu_summary(job_id, align_pts=align_pts)

    with _lock:
        job.status = "running"
        job.progress = 0
        job.message = "Iniciando…"

    target = _run_preview if mode == "preview" else _run_reconstruction
    t = threading.Thread(target=target, args=(job,), daemon=True)
    t.start()

    log.info(f"Job {job_id}: {mode} started with {len(job.received_frames)} frames")
    return jsonify({"ok": True, "mode": mode})


@app.get("/pointcloud/<job_id>")
def pointcloud(job_id: str):
    """Download the sparse point cloud PLY generated during preview."""
    job = _job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    if job.status not in ("preview_done", "done") or not job.sparse_ply:
        return jsonify({"error": "point cloud not ready"}), 400
    return send_file(job.sparse_ply, as_attachment=True,
                     download_name="pointcloud.ply", mimetype="application/octet-stream")


@app.post("/continue_reconstruct/<job_id>")
def continue_reconstruct(job_id: str):
    """Trigger full MVS + mesh generation from an already-completed preview SfM."""
    job = _job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    if job.status == "running":
        return jsonify({"ok": True, "message": "already running"})
    if job.status != "preview_done":
        return jsonify({"error": f"job must be in preview_done state, is {job.status}"}), 400

    with _lock:
        job.status   = "running"
        job.progress = 50
        job.message  = "Continuando con reconstrucción densa…"

    t = threading.Thread(target=_run_dense_from_sparse, args=(job,), daemon=True)
    t.start()

    log.info(f"Job {job_id}: continuing to full reconstruction")
    return jsonify({"ok": True})


@app.get("/status/<job_id>")
def status(job_id: str):
    job = _job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job.to_dict())


@app.get("/download/<job_id>")
def download(job_id: str):
    job = _job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    if job.status != "done" or not job.output_path:
        return jsonify({"error": "mesh not ready"}), 400
    return send_file(job.output_path, as_attachment=True,
                     download_name=pathlib.Path(job.output_path).name)


@app.get("/jobs")
def list_jobs():
    with _lock:
        return jsonify([j.to_dict() for j in _jobs.values()])


@app.post("/import_folder")
def import_folder():
    """Importar una carpeta existente con imágenes como nuevo job."""
    data   = request.get_json(silent=True) or {}
    folder = data.get("folder", "")
    if not folder:
        return jsonify({"error": "folder requerido"}), 400

    img_dir = pathlib.Path(folder)
    # Aceptar si la carpeta tiene JPEGs o es una carpeta images/ dentro de un job
    if (img_dir / "images").exists():
        img_dir = img_dir / "images"

    images = sorted(img_dir.glob("*.jpg"))
    if not images:
        return jsonify({"error": f"No se encontraron imágenes en {folder}"}), 400

    import shutil
    job = _new_job()
    jid = job.job_id
    dst = WORK_ROOT / jid / "images"
    dst.mkdir(parents=True, exist_ok=True)

    # Copiar o enlazar imágenes al directorio del job
    for i, img in enumerate(images):
        shutil.copy2(str(img), str(dst / f"{i:05d}.jpg"))

    with _lock:
        job.received_frames = set(range(len(images)))
        job.total_frames    = len(images)
        job.status          = "uploading"
        job.message         = f"Importado desde {pathlib.Path(folder).name} ({len(images)} fotos)"

    log.info(f"Imported {len(images)} images from {folder} → job {jid}")
    return jsonify({"job_id": jid, "frames": len(images)})


# ── reconstruction workers ────────────────────────────────────────────────────

def _run_preview(job: Job):
    """Run only SfM → export sparse PLY. Fast (~1 min on CPU)."""
    from reconstructor import reconstruct_sparse

    work_dir = WORK_ROOT / job.job_id
    image_dir = work_dir / "images"
    out_dir   = work_dir / "output"
    imu_file  = work_dir / "imu_summary.json"

    def cb(pct, msg):
        with _lock:
            job.progress = pct
            job.message  = msg
        log.info(f"Job {job.job_id} preview [{pct:3d}%] {msg}")

    try:
        ply_path = reconstruct_sparse(
            image_folder=str(image_dir),
            imu_file=str(imu_file),
            output_dir=str(out_dir),
            progress_cb=cb,
        )
        with _lock:
            job.status     = "preview_done"
            job.progress   = 100
            job.message    = "✓ Nube de puntos lista"
            job.sparse_ply = ply_path
        log.info(f"Job {job.job_id} preview done → {ply_path}")
    except Exception as e:
        with _lock:
            job.status  = "error"
            job.error   = str(e)
            job.message = f"Error en preview: {e}"
        log.error(f"Job {job.job_id} preview failed: {e}", exc_info=True)


def _run_dense_from_sparse(job: Job):
    """Run MVS + meshing from the SfM already stored in the job's output dir."""
    from reconstructor import reconstruct_dense

    work_dir  = WORK_ROOT / job.job_id
    image_dir = work_dir / "images"
    out_dir   = work_dir / "output"
    imu_file  = work_dir / "imu_summary.json"

    def cb(pct, msg):
        with _lock:
            job.progress = 50 + pct // 2   # 50-100 range
            job.message  = msg
        log.info(f"Job {job.job_id} dense [{pct:3d}%] {msg}")

    try:
        obj_path = reconstruct_dense(
            image_folder=str(image_dir),
            output_dir=str(out_dir),
            progress_cb=cb,
        )
        _finalize_job(job, obj_path)
    except Exception as e:
        with _lock:
            job.status  = "error"
            job.error   = str(e)
            job.message = f"Error en reconstrucción densa: {e}"
        log.error(f"Job {job.job_id} dense failed: {e}", exc_info=True)


def _finalize_job(job: Job, obj_path: str):
    import shutil
    from datetime import datetime
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = OUTPUT_DIR / f"scan_{ts}_{job.job_id}.obj"
    shutil.copy2(obj_path, dest)
    # Las fotos se conservan en WORK_ROOT/job_id/ — se borran solo cuando
    # se evicta el job al superar MAX_JOBS
    with _lock:
        job.status      = "done"
        job.progress    = 100
        job.message     = f"✓ Listo — {dest.name}"
        job.output_path = str(dest)
    log.info(f"Job {job.job_id} done → {dest}")
    if _on_mesh_ready_cb:
        try:
            from datetime import datetime as dt
            scan_name = f"Escaneo {dt.now().strftime('%d/%m/%Y %H:%M')}"
            _on_mesh_ready_cb(str(dest), scan_name)
        except Exception as e:
            log.warning(f"mesh_ready_cb error: {e}")


def _run_reconstruction(job: Job):
    from reconstructor import reconstruct

    work_dir  = WORK_ROOT / job.job_id
    image_dir = work_dir / "images"
    out_dir   = work_dir / "output"
    imu_file  = work_dir / "imu_summary.json"

    def cb(pct: int, msg: str):
        with _lock:
            job.progress = pct
            job.message  = msg
        log.info(f"Job {job.job_id} [{pct:3d}%] {msg}")

    try:
        obj_path = reconstruct(
            image_folder=str(image_dir),
            imu_file=str(imu_file),
            output_dir=str(out_dir),
            progress_cb=cb,
        )
        _finalize_job(job, obj_path)
    except Exception as e:
        with _lock:
            job.status  = "error"
            job.error   = str(e)
            job.message = f"Error: {e}"
        log.error(f"Job {job.job_id} failed: {e}", exc_info=True)


def _build_imu_summary(job_id: str, align_pts: list = None):
    """Aggregate per-frame JSON metadata + alignment points into imu_summary.json."""
    img_dir  = WORK_ROOT / job_id / "images"
    frames   = []
    width = height = 0

    for meta_file in sorted(img_dir.glob("*.json")):
        try:
            data = json.loads(meta_file.read_text())
            frames.append(data)
            cam = data.get("camera", {})
            width  = cam.get("width",  width)  or width
            height = cam.get("height", height) or height
        except Exception:
            pass

    summary = {
        "frames":     frames,
        "width":      width,
        "height":     height,
        "align_pts":  align_pts or [],   # [{index, frame_id, px, py, imu}, ...]
    }
    (WORK_ROOT / job_id / "imu_summary.json").write_text(json.dumps(summary))


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5005)
    args = parser.parse_args()

    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"\n  Refractory Capture Server")
    print(f"  Escuchando en: http://{local_ip}:{args.port}")
    print(f"  Meshes guardados en: {OUTPUT_DIR}")
    print(f"  Ctrl+C para detener\n")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)
