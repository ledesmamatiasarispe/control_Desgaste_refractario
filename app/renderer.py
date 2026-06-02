import ctypes
import numpy as np
from OpenGL import GL

from app.shaders import MESH_VERT, MESH_FRAG, POINT_VERT, POINT_FRAG, CLIP_PASS_ALL


# ── shader helpers ──────────────────────────────────────────────────────────

def _compile(src: str, kind: int) -> int:
    s = GL.glCreateShader(kind)
    GL.glShaderSource(s, src)
    GL.glCompileShader(s)
    if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
        raise RuntimeError(GL.glGetShaderInfoLog(s).decode())
    return s


def _link(vert_src: str, frag_src: str) -> int:
    v = _compile(vert_src, GL.GL_VERTEX_SHADER)
    f = _compile(frag_src, GL.GL_FRAGMENT_SHADER)
    p = GL.glCreateProgram()
    GL.glAttachShader(p, v)
    GL.glAttachShader(p, f)
    GL.glLinkProgram(p)
    if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
        raise RuntimeError(GL.glGetProgramInfoLog(p).decode())
    GL.glDeleteShader(v)
    GL.glDeleteShader(f)
    return p


def _uloc(prog: int, name: str) -> int:
    return GL.glGetUniformLocation(prog, name)


def set_mat4(prog: int, name: str, mat: np.ndarray):
    loc = _uloc(prog, name)
    if loc >= 0:
        GL.glUniformMatrix4fv(loc, 1, GL.GL_TRUE, mat.astype(np.float32))


def set_vec3(prog: int, name: str, v):
    loc = _uloc(prog, name)
    if loc >= 0:
        GL.glUniform3f(loc, float(v[0]), float(v[1]), float(v[2]))


def set_vec4(prog: int, name: str, v):
    loc = _uloc(prog, name)
    if loc >= 0:
        GL.glUniform4f(loc, float(v[0]), float(v[1]), float(v[2]), float(v[3]))


def set_int(prog: int, name: str, val: int):
    loc = _uloc(prog, name)
    if loc >= 0:
        GL.glUniform1i(loc, val)


def set_float(prog: int, name: str, val: float):
    loc = _uloc(prog, name)
    if loc >= 0:
        GL.glUniform1f(loc, val)


# ── GPU mesh object ─────────────────────────────────────────────────────────

class MeshObject:
    """Holds the VAO/VBO/IBO for one mesh or point cloud on the GPU.

    VBO layout (interleaved, stride 40 bytes):
        offset  0 – position  vec3 (12 bytes)
        offset 12 – normal    vec3 (12 bytes)
        offset 24 – color     vec4 (16 bytes)
    """

    STRIDE = 10 * 4  # 10 floats × 4 bytes

    def __init__(self):
        self.vao          = None
        self.vbo          = None
        self.ibo          = None
        self.vertex_count = 0
        self.index_count  = 0
        self.is_point_cloud = False
        self._vdata       = None   # (N, 10) float32 kept for color updates

    def upload(self, mesh_data):
        self.vertex_count   = mesh_data.vertex_count
        self.is_point_cloud = mesh_data.is_point_cloud

        buf = np.empty((mesh_data.vertex_count, 10), dtype=np.float32)
        buf[:, 0:3]  = mesh_data.vertices
        buf[:, 3:6]  = mesh_data.normals
        buf[:, 6:10] = mesh_data.colors
        self._vdata  = buf

        self.vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self.vao)

        self.vbo = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, buf.nbytes, buf, GL.GL_DYNAMIC_DRAW)

        s = self.STRIDE
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, s, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, s, ctypes.c_void_p(12))
        GL.glEnableVertexAttribArray(2)
        GL.glVertexAttribPointer(2, 4, GL.GL_FLOAT, GL.GL_FALSE, s, ctypes.c_void_p(24))

        if not self.is_point_cloud and mesh_data.faces is not None:
            faces = mesh_data.faces.astype(np.uint32).flatten()
            self.index_count = len(faces)
            self.ibo = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.ibo)
            GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER,
                            faces.nbytes, faces, GL.GL_STATIC_DRAW)

        GL.glBindVertexArray(0)

    def update_colors(self, colors: np.ndarray):
        if self._vdata is None:
            return
        self._vdata[:, 6:10] = colors
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER,
                        self._vdata.nbytes, self._vdata, GL.GL_DYNAMIC_DRAW)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

    def reset_colors(self, base_color=(0.72, 0.78, 0.85, 1.0)):
        if self._vdata is None:
            return
        self._vdata[:, 6:10] = base_color
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER,
                        self._vdata.nbytes, self._vdata, GL.GL_DYNAMIC_DRAW)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

    def draw(self):
        if self.vao is None:
            return
        GL.glBindVertexArray(self.vao)
        if self.is_point_cloud:
            GL.glDrawArrays(GL.GL_POINTS, 0, self.vertex_count)
        else:
            GL.glDrawElements(GL.GL_TRIANGLES, self.index_count,
                              GL.GL_UNSIGNED_INT, None)
        GL.glBindVertexArray(0)

    def destroy(self):
        if self.vbo:
            GL.glDeleteBuffers(1, [self.vbo])
        if self.ibo:
            GL.glDeleteBuffers(1, [self.ibo])
        if self.vao:
            GL.glDeleteVertexArrays(1, [self.vao])
        self.vao = self.vbo = self.ibo = None
        self._vdata = None


# ── small point marker object (annotations / 3-pt align) ────────────────────

class MarkersObject:
    """Renders a small list of world-space markers as GL_POINTS."""

    def __init__(self):
        self.vao          = None
        self.vbo          = None
        self.vertex_count = 0

    def upload(self, positions: np.ndarray, colors: np.ndarray):
        """positions: (N,3) float32  colors: (N,4) float32."""
        N = len(positions)
        self.vertex_count = N

        buf = np.zeros((N, 10), dtype=np.float32)
        buf[:, 0:3]  = positions
        buf[:, 6:10] = colors

        if self.vao is None:
            self.vao = GL.glGenVertexArrays(1)
            self.vbo = GL.glGenBuffers(1)

        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, buf.nbytes, buf, GL.GL_DYNAMIC_DRAW)

        s = 10 * 4
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, s, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(2)
        GL.glVertexAttribPointer(2, 4, GL.GL_FLOAT, GL.GL_FALSE, s, ctypes.c_void_p(24))
        GL.glBindVertexArray(0)

    def draw(self):
        if self.vao is None or self.vertex_count == 0:
            return
        GL.glBindVertexArray(self.vao)
        GL.glDrawArrays(GL.GL_POINTS, 0, self.vertex_count)
        GL.glBindVertexArray(0)

    def destroy(self):
        if self.vbo:
            GL.glDeleteBuffers(1, [self.vbo])
        if self.vao:
            GL.glDeleteVertexArrays(1, [self.vao])
        self.vao = self.vbo = None
        self.vertex_count = 0


# ── measurement lines (GL_LINES reuses point shader) ────────────────────────

class LinesObject:
    """Pairs of vertices rendered as GL_LINES (measurement segments)."""

    def __init__(self):
        self.vao          = None
        self.vbo          = None
        self.vertex_count = 0   # must be even

    def upload(self, positions: np.ndarray, colors: np.ndarray):
        """positions (N,3), colors (N,4) — N must be even (pairs of endpoints)."""
        N = len(positions)
        if N == 0:
            self.vertex_count = 0
            return
        self.vertex_count = N
        buf = np.zeros((N, 10), dtype=np.float32)
        buf[:, 0:3]  = positions
        buf[:, 6:10] = colors

        if self.vao is None:
            self.vao = GL.glGenVertexArrays(1)
            self.vbo = GL.glGenBuffers(1)

        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, buf.nbytes, buf, GL.GL_DYNAMIC_DRAW)
        s = 10 * 4
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, s, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(2)
        GL.glVertexAttribPointer(2, 4, GL.GL_FLOAT, GL.GL_FALSE, s, ctypes.c_void_p(24))
        GL.glBindVertexArray(0)

    def draw(self):
        if self.vao is None or self.vertex_count < 2:
            return
        GL.glBindVertexArray(self.vao)
        GL.glLineWidth(2.5)
        GL.glDrawArrays(GL.GL_LINES, 0, self.vertex_count)
        GL.glLineWidth(1.0)
        GL.glBindVertexArray(0)

    def destroy(self):
        if self.vbo:
            GL.glDeleteBuffers(1, [self.vbo])
        if self.vao:
            GL.glDeleteVertexArrays(1, [self.vao])
        self.vao = self.vbo = None
        self.vertex_count = 0


# ── top-level renderer ───────────────────────────────────────────────────────

_CLIP_PASS = np.array(CLIP_PASS_ALL, dtype=np.float32)


class Renderer:
    """Owns all GPU resources and draws frames with optional dual mesh + clip planes."""

    def __init__(self):
        self.mesh_prog    = None
        self.point_prog   = None
        self.mesh_obj     = None
        self.mesh_obj_ref = None
        self.markers      = MarkersObject()
        self.meas_lines   = LinesObject()    # measurement line segments
        self.meas_markers = MarkersObject()  # measurement point markers
        self._wireframe   = False
        self._clip_h      = _CLIP_PASS.copy()
        self._clip_v      = _CLIP_PASS.copy()
        self._ref_mode    = "wireframe"      # "wireframe" | "solid_transparent"

    def initialize(self):
        self.mesh_prog  = _link(MESH_VERT, MESH_FRAG)
        self.point_prog = _link(POINT_VERT, POINT_FRAG)

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glEnable(GL.GL_MULTISAMPLE)
        GL.glEnable(GL.GL_PROGRAM_POINT_SIZE)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glClearColor(0.15, 0.15, 0.18, 1.0)
        # Enable two clip distance planes
        GL.glEnable(0x3000)   # GL_CLIP_DISTANCE0
        GL.glEnable(0x3001)   # GL_CLIP_DISTANCE1

    # ── mesh management ──────────────────────────────────────────────────────

    def load_mesh(self, mesh_data):
        if self.mesh_obj:
            self.mesh_obj.destroy()
        self.mesh_obj = MeshObject()
        self.mesh_obj.upload(mesh_data)

    def load_reference(self, mesh_data):
        if self.mesh_obj_ref:
            self.mesh_obj_ref.destroy()
        self.mesh_obj_ref = MeshObject()
        self.mesh_obj_ref.upload(mesh_data)

    def clear_reference(self):
        if self.mesh_obj_ref:
            self.mesh_obj_ref.destroy()
        self.mesh_obj_ref = None

    def update_colors(self, colors: np.ndarray):
        if self.mesh_obj:
            self.mesh_obj.update_colors(colors)

    def reset_colors(self):
        if self.mesh_obj:
            self.mesh_obj.reset_colors()

    def set_wireframe(self, enabled: bool):
        self._wireframe = enabled

    def set_ref_mode(self, mode: str):
        self._ref_mode = mode

    def set_clip_planes(self, clip_h: np.ndarray, clip_v: np.ndarray):
        self._clip_h = clip_h.astype(np.float32)
        self._clip_v = clip_v.astype(np.float32)

    def update_markers(self, positions: np.ndarray, colors: np.ndarray):
        if len(positions):
            self.markers.upload(positions, colors)
        else:
            self.markers.vertex_count = 0

    def update_measurements(self, segments: np.ndarray, seg_colors: np.ndarray,
                            pts: np.ndarray, pt_colors: np.ndarray):
        """segments: (N*2, 3) endpoints; pts: (M, 3) point markers."""
        if len(segments) >= 2:
            self.meas_lines.upload(segments, seg_colors)
        else:
            self.meas_lines.vertex_count = 0
        if len(pts):
            self.meas_markers.upload(pts, pt_colors)
        else:
            self.meas_markers.vertex_count = 0

    # ── drawing ───────────────────────────────────────────────────────────────

    def draw(self, mvp: np.ndarray, cam_pos: np.ndarray,
             use_vcolor: bool = False):
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        # ── reference mesh ──
        if self.mesh_obj_ref and self.mesh_obj_ref.vao:
            GL.glUseProgram(self.mesh_prog)
            if self._ref_mode == "solid_transparent":
                GL.glDepthMask(GL.GL_FALSE)
                self._set_mesh_uniforms(mvp, cam_pos,
                                        use_vcolor=False,
                                        base_color=(0.2, 0.6, 1.0, 0.38))
                self.mesh_obj_ref.draw()
                GL.glDepthMask(GL.GL_TRUE)
            else:
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
                self._set_mesh_uniforms(mvp, cam_pos,
                                        use_vcolor=False,
                                        base_color=(0.55, 0.55, 0.60, 1.0))
                self.mesh_obj_ref.draw()
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)

        # ── current mesh (solid / heatmap / wireframe) ──
        if self.mesh_obj and self.mesh_obj.vao:
            mode = GL.GL_LINE if self._wireframe else GL.GL_FILL
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, mode)

            if self.mesh_obj.is_point_cloud:
                GL.glUseProgram(self.point_prog)
                self._set_point_uniforms(mvp, 3.0)
            else:
                GL.glUseProgram(self.mesh_prog)
                self._set_mesh_uniforms(mvp, cam_pos,
                                        use_vcolor=use_vcolor,
                                        base_color=(0.72, 0.78, 0.85, 1.0))

            self.mesh_obj.draw()
            GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)

        # ── always-on-top layer (markers, lines, measure points) ──
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(0x3000)   # GL_CLIP_DISTANCE0
        GL.glDisable(0x3001)   # GL_CLIP_DISTANCE1
        GL.glUseProgram(self.point_prog)
        set_vec4(self.point_prog, "u_clip_h", CLIP_PASS_ALL)
        set_vec4(self.point_prog, "u_clip_v", CLIP_PASS_ALL)

        if self.markers.vertex_count > 0:
            set_mat4 (self.point_prog, "u_mvp",       mvp)
            set_float(self.point_prog, "u_point_size", 18.0)
            self.markers.draw()

        # Measurement lines (re-use point shader — gl_PointSize ignored for GL_LINES)
        if self.meas_lines.vertex_count >= 2:
            set_mat4(self.point_prog, "u_mvp", mvp)
            self.meas_lines.draw()

        # Measurement point markers
        if self.meas_markers.vertex_count > 0:
            set_mat4 (self.point_prog, "u_mvp",       mvp)
            set_float(self.point_prog, "u_point_size", 14.0)
            self.meas_markers.draw()

        GL.glEnable(0x3000)
        GL.glEnable(0x3001)
        GL.glEnable(GL.GL_DEPTH_TEST)

    def _set_mesh_uniforms(self, mvp, cam_pos, use_vcolor, base_color):
        set_mat4(self.mesh_prog, "u_mvp",       mvp)
        set_vec3(self.mesh_prog, "u_cam_pos",   cam_pos)
        set_int (self.mesh_prog, "u_use_vcolor", 1 if use_vcolor else 0)
        set_vec4(self.mesh_prog, "u_base_color", base_color)
        set_vec4(self.mesh_prog, "u_clip_h",    self._clip_h)
        set_vec4(self.mesh_prog, "u_clip_v",    self._clip_v)

    def _set_point_uniforms(self, mvp, size):
        set_mat4 (self.point_prog, "u_mvp",       mvp)
        set_float(self.point_prog, "u_point_size", size)
        set_vec4 (self.point_prog, "u_clip_h",    self._clip_h)
        set_vec4 (self.point_prog, "u_clip_v",    self._clip_v)

    def destroy(self):
        if self.mesh_obj:
            self.mesh_obj.destroy()
        if self.mesh_obj_ref:
            self.mesh_obj_ref.destroy()
        self.markers.destroy()
        self.meas_lines.destroy()
        self.meas_markers.destroy()
        if self.mesh_prog:
            GL.glDeleteProgram(self.mesh_prog)
        if self.point_prog:
            GL.glDeleteProgram(self.point_prog)
