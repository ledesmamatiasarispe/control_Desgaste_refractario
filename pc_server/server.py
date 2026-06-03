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

# Fotos e imágenes temporales de cada job — carpeta fija y visible para el usuario
WORK_ROOT = pathlib.Path(tempfile.gettempdir()) / "refractory_capture"
WORK_ROOT.mkdir(exist_ok=True)

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


@dataclass
class Job:
    job_id:          str
    status:          str          = "waiting"   # waiting/uploading/running/done/error
    progress:        int          = 0
    message:         str          = ""
    received_frames: Set[int]     = field(default_factory=set)
    total_frames:    int          = 0
    output_path:     Optional[str] = None
    error:           Optional[str] = None

    def to_dict(self):
        d = asdict(self)
        d["received_frames"] = sorted(self.received_frames)
        return d


_jobs: Dict[str, Job] = {}
_lock = threading.Lock()


def _job(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(job_id)


def _new_job() -> Job:
    jid = str(uuid.uuid4())[:8]
    job = Job(job_id=jid)
    with _lock:
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
    if job.status not in ("waiting", "uploading"):
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
        job.status = "uploading"
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

    if len(job.received_frames) < 5:
        return jsonify({"error": "se necesitan al menos 5 fotogramas"}), 400

    # Build IMU summary file from per-frame JSONs
    _build_imu_summary(job_id)

    # Start reconstruction in background thread
    with _lock:
        job.status = "running"
        job.progress = 0
        job.message = "Iniciando…"

    t = threading.Thread(target=_run_reconstruction, args=(job,), daemon=True)
    t.start()

    log.info(f"Job {job_id}: reconstruction started with {len(job.received_frames)} frames")
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


# ── reconstruction worker ─────────────────────────────────────────────────────

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
            image_folder = str(image_dir),
            imu_file     = str(imu_file),
            output_dir   = str(out_dir),
            progress_cb  = cb,
        )

        # Copy to output folder for the desktop app
        from datetime import datetime
        import shutil
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = OUTPUT_DIR / f"scan_{ts}_{job.job_id}.obj"
        shutil.copy2(obj_path, dest)

        # Limpiar carpeta temporal del job (fotos, db, intermedios)
        try:
            shutil.rmtree(work_dir)
            log.info(f"Job {job.job_id} temp dir cleaned")
        except Exception as clean_err:
            log.warning(f"Could not clean temp dir: {clean_err}")

        with _lock:
            job.status      = "done"
            job.progress    = 100
            job.message     = f"✓ Listo — {dest.name}"
            job.output_path = str(dest)

        log.info(f"Job {job.job_id} done → {dest}")

        # Notify desktop app if running embedded
        if _on_mesh_ready_cb:
            try:
                from datetime import datetime
                scan_name = f"Escaneo {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                _on_mesh_ready_cb(str(dest), scan_name)
            except Exception as cb_err:
                log.warning(f"mesh_ready_cb error: {cb_err}")

    except Exception as e:
        with _lock:
            job.status  = "error"
            job.error   = str(e)
            job.message = f"Error: {e}"
        log.error(f"Job {job.job_id} failed: {e}", exc_info=True)


def _build_imu_summary(job_id: str):
    """Aggregate per-frame JSON metadata into a single IMU summary file."""
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

    summary = {"frames": frames, "width": width, "height": height}
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
