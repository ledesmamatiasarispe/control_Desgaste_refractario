"""
Project save/load.

A .refproj file is a standard ZIP archive containing:
  project.json          – metadata, scan list, calibration
  meshes/0.npz          – compressed numpy arrays for each scan
  meshes/1.npz
  ...

Mesh NPZ keys:
  vertices  (N,3) float32
  faces     (M,3) uint32   (empty array for point clouds)
  normals   (N,3) float32
  colors    (N,4) float32

JSON format v2:
  version, id, name, calibration, start_date, end_date,
  scans[]: {id, name, source, mesh_idx, load_date}

JSON format v1 (legacy, auto-migrated on load):
  version, name, calibration,
  campaigns[]: {name, source, mesh_idx}
"""

import io
import json
import pathlib
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from uuid import uuid4

import numpy as np

from core.loader import MeshData

PROJECT_EXT     = ".refproj"
PROJECT_FILTER  = f"Proyecto Refractory (*{PROJECT_EXT});;Todos los archivos (*)"
_RECENT_FILE    = pathlib.Path.home() / ".refractory_recent.json"
_MAX_RECENT     = 8


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class ScanMeta:
    id:          str
    name:        str
    source_path: str                        # original file, informative only
    load_date:   str                        # ISO 8601 datetime string
    align_pts:   Optional[List] = None      # [[x,y,z],[x,y,z],[x,y,z]] or None


@dataclass
class CampaignData:
    id:          str
    name:        str
    scans:       List[ScanMeta]
    meshes:      List[MeshData]
    calibration:          Optional[float] = None   # ref P1-P2 distance
    start_date:           Optional[str]   = None   # ISO 8601, auto from first scan
    end_date:             Optional[str]   = None   # ISO 8601, set by user when closing
    calibrated_scan_idx:  Optional[int]   = None   # index of the scan used as base


# ── save ─────────────────────────────────────────────────────────────────────

def save_project(path: str, campaign: CampaignData):
    meta = {
        "version":     2,
        "id":          campaign.id,
        "name":        campaign.name or pathlib.Path(path).stem,
        "calibration": campaign.calibration,
        "start_date":          campaign.start_date,
        "end_date":            campaign.end_date,
        "calibrated_scan_idx": campaign.calibrated_scan_idx,
        "scans": [
            {
                "id":        s.id,
                "name":      s.name,
                "source":    s.source_path,
                "mesh_idx":  i,
                "load_date": s.load_date,
                "align_pts": s.align_pts,
            }
            for i, s in enumerate(campaign.scans)
        ],
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        zf.writestr("project.json", json.dumps(meta, indent=2))
        for i, mesh in enumerate(campaign.meshes):
            buf = io.BytesIO()
            np.savez_compressed(
                buf,
                vertices = mesh.vertices,
                faces    = mesh.faces if mesh.faces is not None
                           else np.empty((0, 3), dtype=np.uint32),
                normals  = mesh.normals,
                colors   = mesh.colors,
            )
            zf.writestr(f"meshes/{i}.npz", buf.getvalue())

    _add_recent(path)


# ── load ─────────────────────────────────────────────────────────────────────

def load_project(path: str) -> CampaignData:
    scans:  List[ScanMeta] = []
    meshes: List[MeshData] = []

    with zipfile.ZipFile(path, "r") as zf:
        meta = json.loads(zf.read("project.json"))

        # Migrate v1 → v2
        if meta.get("version", 1) == 1:
            now = datetime.now().isoformat()
            scans_raw = [
                {
                    "id":        str(uuid4()),
                    "name":      c["name"],
                    "source":    c.get("source", ""),
                    "mesh_idx":  c["mesh_idx"],
                    "load_date": now,
                }
                for c in meta.get("campaigns", [])
            ]
            meta["scans"]      = scans_raw
            meta["id"]         = str(uuid4())
            meta["start_date"] = scans_raw[0]["load_date"] if scans_raw else None
            meta["end_date"]   = None

        for entry in meta["scans"]:
            idx  = entry["mesh_idx"]
            raw  = zf.read(f"meshes/{idx}.npz")
            arrs = np.load(io.BytesIO(raw))

            vertices = arrs["vertices"].astype(np.float32)
            faces_a  = arrs["faces"]
            faces    = faces_a.astype(np.uint32) if faces_a.size else None
            normals  = arrs["normals"].astype(np.float32)
            colors   = arrs["colors"].astype(np.float32)

            is_pc    = faces is None
            centroid = vertices.mean(axis=0)
            radius   = float(np.max(np.linalg.norm(vertices - centroid, axis=1)))

            meshes.append(MeshData(
                vertices       = vertices,
                faces          = faces,
                normals        = normals,
                colors         = colors,
                centroid       = centroid,
                radius         = max(radius, 1e-6),
                is_point_cloud = is_pc,
                source_path    = entry.get("source", ""),
                vertex_count   = len(vertices),
                face_count     = len(faces) if faces is not None else 0,
            ))
            scans.append(ScanMeta(
                id          = entry.get("id", str(uuid4())),
                name        = entry["name"],
                source_path = entry.get("source", ""),
                load_date   = entry.get("load_date", datetime.now().isoformat()),
                align_pts   = entry.get("align_pts"),
            ))

    _add_recent(path)

    return CampaignData(
        id          = meta.get("id", str(uuid4())),
        name        = meta.get("name", pathlib.Path(path).stem),
        scans       = scans,
        meshes      = meshes,
        calibration = meta.get("calibration"),
        start_date           = meta.get("start_date"),
        end_date             = meta.get("end_date"),
        calibrated_scan_idx  = meta.get("calibrated_scan_idx"),
    )


# ── recent projects ───────────────────────────────────────────────────────────

def get_recent() -> List[str]:
    try:
        if _RECENT_FILE.exists():
            data = json.loads(_RECENT_FILE.read_text())
            return [p for p in data if pathlib.Path(p).exists()]
    except Exception:
        pass
    return []


def _add_recent(path: str):
    path = str(pathlib.Path(path).resolve())
    recent = [p for p in get_recent() if p != path]
    recent.insert(0, path)
    recent = recent[:_MAX_RECENT]
    try:
        _RECENT_FILE.write_text(json.dumps(recent))
    except Exception:
        pass
