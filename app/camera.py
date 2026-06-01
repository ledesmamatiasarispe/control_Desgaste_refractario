import math
import numpy as np


class OrbitCamera:
    def __init__(self):
        self.target   = np.zeros(3, dtype=np.float64)
        self.distance = 5.0
        self.yaw      = 30.0
        self.pitch    = 20.0
        self._min_dist = 0.001
        self._max_dist = 10000.0

    # ── geometry ────────────────────────────────────────────────────────────

    @property
    def position(self) -> np.ndarray:
        yr = math.radians(self.yaw)
        pr = math.radians(self.pitch)
        x  = self.distance * math.cos(pr) * math.sin(yr)
        y  = self.distance * math.sin(pr)
        z  = self.distance * math.cos(pr) * math.cos(yr)
        return self.target + np.array([x, y, z], dtype=np.float64)

    def _lookat(self, eye, center, up) -> np.ndarray:
        f = center - eye
        fn = np.linalg.norm(f)
        if fn < 1e-12:
            return np.eye(4, dtype=np.float32)
        f = f / fn

        r = np.cross(f, up)
        rn = np.linalg.norm(r)
        if rn < 1e-6:
            up = np.array([0, 0, 1], dtype=np.float64)
            r  = np.cross(f, up)
            rn = np.linalg.norm(r)
        r = r / rn
        u = np.cross(r, f)

        M       = np.eye(4, dtype=np.float32)
        M[0,:3] = r
        M[1,:3] = u
        M[2,:3] = -f
        M[0, 3] = float(-np.dot(r, eye))
        M[1, 3] = float(-np.dot(u, eye))
        M[2, 3] = float( np.dot(f, eye))
        return M

    def get_view_matrix(self) -> np.ndarray:
        return self._lookat(
            self.position, self.target,
            np.array([0, 1, 0], dtype=np.float64)
        )

    def get_projection_matrix(self, aspect: float) -> np.ndarray:
        fov  = math.radians(45.0)
        # near/far derived from current camera distance to avoid
        # a degenerate (near→0) projection matrix that breaks ray unprojection
        near = max(self.distance * 0.001, 1e-4)
        far  = max(self.distance * 500.0, near * 1000.0)
        f    = 1.0 / math.tan(fov / 2.0)

        M        = np.zeros((4, 4), dtype=np.float32)
        M[0, 0]  = f / aspect
        M[1, 1]  = f
        M[2, 2]  = (far + near) / (near - far)
        M[2, 3]  = (2 * far * near) / (near - far)
        M[3, 2]  = -1.0
        return M

    def get_mvp(self, aspect: float) -> np.ndarray:
        return self.get_projection_matrix(aspect) @ self.get_view_matrix()

    # ── controls ────────────────────────────────────────────────────────────

    def fit(self, center: np.ndarray, radius: float):
        self.target    = np.array(center, dtype=np.float64)
        self.distance  = max(radius * 2.5, 0.01)
        self._min_dist = max(radius * 0.01, 1e-4)
        self._max_dist = max(radius * 200.0, 1.0)

    def orbit(self, dx: float, dy: float):
        self.yaw  += dx * 0.45
        self.pitch = max(-89.0, min(89.0, self.pitch - dy * 0.45))

    def pan(self, dx: float, dy: float, viewport_h: float):
        eye = self.position
        f   = (self.target - eye)
        f  /= (np.linalg.norm(f) + 1e-12)
        up  = np.array([0, 1, 0], dtype=np.float64)
        r   = np.cross(f, up)
        rn  = np.linalg.norm(r)
        if rn < 1e-6:
            r = np.array([1, 0, 0], dtype=np.float64)
        else:
            r /= rn
        u = np.cross(r, f)

        scale = self.distance / (viewport_h + 1) * 2.0
        self.target -= r * (dx * scale)
        self.target += u * (dy * scale)

    def zoom(self, delta: float):
        factor        = 0.88 if delta > 0 else 1.14
        self.distance = max(self._min_dist, min(self._max_dist, self.distance * factor))

    # ── ray casting ─────────────────────────────────────────────────────────

    def get_ray(self, x: float, y: float, w: int, h: int):
        """Return (origin, direction) world-space ray for screen pixel (x, y)."""
        aspect = w / max(h, 1)
        P      = self.get_projection_matrix(aspect).astype(np.float64)
        V      = self.get_view_matrix().astype(np.float64)
        VP_inv = np.linalg.inv(P @ V)

        nx = (2.0 * x / w) - 1.0
        ny = 1.0 - (2.0 * y / h)

        def unproject(nz):
            v  = VP_inv @ np.array([nx, ny, nz, 1.0])
            return v[:3] / v[3]

        near = unproject(-1.0)
        far  = unproject( 1.0)

        origin    = near.astype(np.float32)
        direction = (far - near).astype(np.float32)
        n         = np.linalg.norm(direction)
        if n > 0:
            direction /= n
        return origin, direction
