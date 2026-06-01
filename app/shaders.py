MESH_VERT = """
#version 330 core
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_norm;
layout(location = 2) in vec4 a_color;

uniform mat4 u_mvp;
uniform int  u_use_vcolor;
uniform vec4 u_base_color;

// Clipping planes: dot(plane, vec4(pos,1)) >= 0 → keep
uniform vec4 u_clip_h;   // horizontal (Y axis)
uniform vec4 u_clip_v;   // vertical   (X axis)

out vec3 v_norm;
out vec3 v_pos;
out vec4 v_color;

void main() {
    v_pos       = a_pos;
    v_norm      = a_norm;
    v_color     = (u_use_vcolor != 0) ? a_color : u_base_color;
    gl_Position = u_mvp * vec4(a_pos, 1.0);

    gl_ClipDistance[0] = dot(u_clip_h, vec4(a_pos, 1.0));
    gl_ClipDistance[1] = dot(u_clip_v, vec4(a_pos, 1.0));
}
"""

MESH_FRAG = """
#version 330 core
in vec3 v_norm;
in vec3 v_pos;
in vec4 v_color;

uniform vec3 u_cam_pos;

out vec4 frag_color;

void main() {
    vec3 N = normalize(v_norm);

    vec3 L1 = normalize(vec3(1.0, 2.0, 1.5));
    vec3 L2 = normalize(vec3(-1.0, 0.5, -1.0));
    vec3 V  = normalize(u_cam_pos - v_pos);

    float ambient = 0.20;
    float d1 = max(dot(N, L1), 0.0) * 0.55 + max(dot(-N, L1), 0.0) * 0.20;
    float d2 = max(dot(N, L2), 0.0) * 0.25;
    vec3  R  = reflect(-L1, N);
    float sp = pow(max(dot(V, R), 0.0), 40.0) * 0.18;

    float light = clamp(ambient + d1 + d2 + sp, 0.0, 1.1);
    frag_color  = vec4(v_color.rgb * light, v_color.a);
}
"""

POINT_VERT = """
#version 330 core
layout(location = 0) in vec3 a_pos;
layout(location = 2) in vec4 a_color;

uniform mat4  u_mvp;
uniform float u_point_size;
uniform vec4  u_clip_h;
uniform vec4  u_clip_v;

out vec4 v_color;

void main() {
    v_color      = a_color;
    gl_Position  = u_mvp * vec4(a_pos, 1.0);
    gl_PointSize = u_point_size;
    gl_ClipDistance[0] = dot(u_clip_h, vec4(a_pos, 1.0));
    gl_ClipDistance[1] = dot(u_clip_v, vec4(a_pos, 1.0));
}
"""

POINT_FRAG = """
#version 330 core
in vec4 v_color;
out vec4 frag_color;

void main() {
    vec2 c = gl_PointCoord - 0.5;
    if (dot(c, c) > 0.25) discard;
    frag_color = v_color;
}
"""

# Pass-all plane: dot((0,0,0,1), (x,y,z,1)) = 1 > 0 always
CLIP_PASS_ALL = (0.0, 0.0, 0.0, 1.0)
