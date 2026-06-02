import numpy as np
from core.loader import MeshData


def circumscribed_circle(p1: np.ndarray, p2: np.ndarray, p3: np.ndarray):
    """
    Circumscribed circle of 3 points in 3D.
    Returns (center_3d, radius, axis_unit_vector).
    Raises ValueError if points are collinear.
    """
    a = (p2 - p1).astype(np.float64)
    b = (p3 - p1).astype(np.float64)

    n = np.cross(a, b)
    n_norm = np.linalg.norm(n)
    if n_norm < 1e-12:
        raise ValueError("Los 3 puntos son colineales — no definen un círculo.")
    axis = n / n_norm

    # Build orthonormal basis in the plane and project to 2D
    e1 = a / np.linalg.norm(a)
    e2 = np.cross(axis, e1)

    A2 = np.array([0.0, 0.0])
    B2 = np.array([np.dot(a, e1), np.dot(a, e2)])
    C2 = np.array([np.dot(b, e1), np.dot(b, e2)])

    D = 2.0 * (A2[0]*(B2[1]-C2[1]) + B2[0]*(C2[1]-A2[1]) + C2[0]*(A2[1]-B2[1]))
    if abs(D) < 1e-12:
        raise ValueError("Los 3 puntos son colineales — no definen un círculo.")

    ux = ((A2[0]**2 + A2[1]**2)*(B2[1]-C2[1]) +
          (B2[0]**2 + B2[1]**2)*(C2[1]-A2[1]) +
          (C2[0]**2 + C2[1]**2)*(A2[1]-B2[1])) / D
    uy = ((A2[0]**2 + A2[1]**2)*(C2[0]-B2[0]) +
          (B2[0]**2 + B2[1]**2)*(A2[0]-C2[0]) +
          (C2[0]**2 + C2[1]**2)*(B2[0]-A2[0])) / D

    p1_f = p1.astype(np.float64)
    center = p1_f + ux * e1 + uy * e2
    radius = float(np.linalg.norm(center - p1_f))

    return center, radius, axis


def crop_cylinder(mesh_data: MeshData, center: np.ndarray,
                  radius: float, axis: np.ndarray) -> MeshData:
    """
    Remove all triangles that have any vertex outside the infinite cylinder
    defined by (center, radius, axis). Returns a new MeshData.
    """
    verts = mesh_data.vertices.astype(np.float64)
    faces = mesh_data.faces

    v_rel = verts - center
    proj  = (v_rel @ axis)[:, np.newaxis] * axis
    dist  = np.linalg.norm(v_rel - proj, axis=1)

    inside = dist <= radius

    f_mask    = inside[faces[:, 0]] & inside[faces[:, 1]] & inside[faces[:, 2]]
    new_faces = faces[f_mask]

    if len(new_faces) == 0:
        raise ValueError(
            "El cilindro no contiene ninguna cara del mesh. "
            "Revisá los puntos seleccionados."
        )

    used   = np.unique(new_faces)
    remap  = np.full(len(verts), -1, dtype=np.int32)
    remap[used] = np.arange(len(used), dtype=np.int32)

    new_verts   = mesh_data.vertices[used]
    new_normals = mesh_data.normals[used] if mesh_data.normals is not None \
                  else np.zeros((len(used), 3), dtype=np.float32)
    new_colors  = mesh_data.colors[used] if mesh_data.colors is not None \
                  else np.full((len(used), 4), [0.72, 0.78, 0.85, 1.0], dtype=np.float32)
    new_faces   = remap[new_faces].astype(np.uint32)

    centroid   = new_verts.mean(axis=0).astype(np.float32)
    new_radius = float(np.max(np.linalg.norm(
        new_verts.astype(np.float64) - centroid.astype(np.float64), axis=1)))

    return MeshData(
        vertices      = new_verts,
        faces         = new_faces,
        normals       = new_normals,
        colors        = new_colors,
        centroid      = centroid,
        radius        = max(new_radius, 1e-6),
        is_point_cloud= False,
        source_path   = mesh_data.source_path,
        vertex_count  = len(new_verts),
        face_count    = len(new_faces),
    )
