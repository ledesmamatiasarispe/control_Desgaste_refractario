"""
Project save/load.

A .refproj file is a standard ZIP archive containing:
  project.json          – metadata, campaign list, calibration
  meshes/0.npz          – compressed numpy arrays for each campaign
  meshes/1.npz
  ...

Mesh NPZ keys:
  vertices  (N,3) float32
  faces     (M,3) uint32   (empty array for point clouds)
  normals   (N,3) float32
  colors    (N,4) float32
  is_pc     scalar bool
  source    scalar str (original file path, for display only)
"""

import io
import json
import pathlib
import zipfile
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from core.loader import MeshData

PROJECT_EXT     = ".refproj"
PROJECT_FILTER  = f"Proyecto Refractory (*{PROJECT_EXT});;Todos los archivos (*)"
_RECENT_FILE    = pathlib.Path.home() / ".refractory_recent.json"
_MAX_RECENT     = 8


# ── data class ───────────────────────────────────────────────────────────────

@dataclass
class CampaignMeta:
    name:        str
    source_path: str          # original file, informative only


@dataclass
class ProjectData:
    name:       str
    campaigns:  List[CampaignMeta]
    meshes:     List[MeshData]
    calibration: Optional[float] = None   # ref P1-P2 distance


# ── save ─────────────────────────────────────────────────────────────────────

def save_project(path: str,
                 campaign_names: List[str],
                 mesh_data_list: List[MeshData],
                 calibration: Optional[float] = None,
                 project_name: str = ""):

    meta = {
        "version":     1,
        "name":        project_name or pathlib.Path(path).stem,
        "calibration": calibration,
        "campaigns": [
            {"name": n, "source": m.source_path, "mesh_idx": i}
            for i, (n, m) in enumerate(zip(campaign_names, mesh_data_list))
        ],
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        zf.writestr("project.json", json.dumps(meta, indent=2))
        for i, mesh in enumerate(mesh_data_list):
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

def load_project(path: str) -> ProjectData:
    campaigns: List[CampaignMeta] = []
    meshes:    List[MeshData]     = []

    with zipfile.ZipFile(path, "r") as zf:
        meta = json.loads(zf.read("project.json"))

        for entry in meta["campaigns"]:
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
                vertices     = vertices,
                faces        = faces,
                normals      = normals,
                colors       = colors,
                centroid     = centroid,
                radius       = max(radius, 1e-6),
                is_point_cloud = is_pc,
                source_path  = entry.get("source", ""),
                vertex_count = len(vertices),
                face_count   = len(faces) if faces is not None else 0,
            ))
            campaigns.append(CampaignMeta(
                name        = entry["name"],
                source_path = entry.get("source", ""),
            ))

    _add_recent(path)

    return ProjectData(
        name        = meta.get("name", pathlib.Path(path).stem),
        campaigns   = campaigns,
        meshes      = meshes,
        calibration = meta.get("calibration"),
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
