# Control de Desgaste Refractario — Guía de Proyecto para IA

## Qué es este proyecto

App de escritorio Windows + app Android companion para análisis de desgaste del
refractario de hornos de inducción. El usuario escanea el horno con el celular,
el PC reconstruye el modelo 3D, y la app de escritorio compara campañas para
visualizar el desgaste con mapas de calor.

---

## Estructura del repositorio

```
refractory_analyzer/          ← raíz del repo (app de escritorio)
├── main.py                   ← entrada: QSurfaceFormat ANTES de QApplication
├── requirements.txt          ← PySide6, trimesh, numpy, scipy, matplotlib, PyOpenGL
├── app/
│   ├── main_window.py        ← QMainWindow, menú Archivo, toolbar, layout 3 paneles
│   ├── gl_widget.py          ← QOpenGLWidget, modos Navigate/Annotate/Align/Calibrate
│   ├── camera.py             ← OrbitCamera numpy, near=distance*0.001 (crítico)
│   ├── renderer.py           ← Renderer dual-mesh, clip planes GL_CLIP_DISTANCE
│   └── shaders.py            ← GLSL 330 core, u_clip_h / u_clip_v uniforms
├── core/
│   ├── loader.py             ← STL/GLB/OBJ/PLY/ZIP, soporta ZIPs anidados
│   ├── picking.py            ← Möller-Trumbore vectorizado, EPS relativo al mesh
│   ├── alignment.py          ← 3-point align + ICP, escala uniforme opcional
│   ├── wear.py               ← KDTree scipy, fallback rtree si está instalado
│   ├── heatmap.py            ← matplotlib.colormaps.get_cmap() (no cm.get_cmap)
│   └── project.py            ← .refproj = ZIP con project.json + meshes/*.npz
├── ui/
│   ├── panel.py              ← CampaignPanel sidebar izquierdo
│   └── comparison_dialog.py  ← QThread para wear, llama mw.show_comparison()
├── pc_server/
│   ├── server.py             ← Flask API, jobs en memoria, frames en disco
│   ├── reconstructor.py      ← pycolmap → Meshroom CLI → error
│   └── start_server.bat      ← doble clic para iniciar servidor
├── android_app/              ← PROYECTO ANDROID NATIVO (Kotlin)
│   ├── build.gradle.kts      ← Configuración de Gradle
│   ├── app/src/main/java/com/refractoryanalyzer/
│   │   ├── MainActivity.kt
│   │   ├── CaptureFragment.kt  ← Lógica de cámara y sensores
│   │   ├── ConnectFragment.kt  ← Conexión al servidor Flask
│   │   └── ProgressFragment.kt ← Upload y Reconstrucción
│   └── res/navigation/nav_graph.xml
└── .github/
    └── workflows/
        └── build-apk.yml     ← GitHub Actions: buildozer → Release automático
```

---

## Cómo publicar una actualización correctamente

### Caso A — Solo cambios en la app de escritorio (Python desktop)

No hay APK que compilar. Solo hacer push.

```bash
# 1. Hacer los cambios en los archivos de app/, core/, ui/, main.py
# 2. Commit y push
git add <archivos modificados>
git commit -m "fix: descripcion del cambio"
git push
```

GitHub Actions NO se dispara para cambios fuera de `android_app/`.

---

### Caso B — Cambios en la app Android (Kivy)

**Paso obligatorio: subir el número de versión ANTES del push.**

```python
# android_app/version.py
VERSION = "1.0.1"   # ← incrementar: mayor.menor.patch
```

Reglas de versionado:
- `patch` (1.0.0 → 1.0.1): bug fix, cambio menor de UI
- `minor` (1.0.0 → 1.1.0): nueva funcionalidad
- `major` (1.0.0 → 2.0.0): cambio breaking, nueva arquitectura

```bash
# 1. Editar android_app/version.py con la nueva versión
# 2. Hacer los cambios en android_app/
# 3. Commit incluyendo version.py
git add android_app/
git commit -m "feat: descripcion — bump version a 1.0.1"
git push
```

**Qué pasa después del push:**
1. GitHub Actions detecta cambios en `android_app/**`
2. Ubuntu runner instala Buildozer (~5 min primera vez, ~2 min con caché)
3. Compila el APK: `refractory-capture-v1.0.1.apk`
4. Crea un Release en GitHub con tag `v1.0.1` y el APK como asset
5. Los teléfonos con la app instalada detectan la nueva versión al abrirla
6. Se ofrece instalar automáticamente via FileProvider intent

---

### Caso C — Cambios en el PC Server (Flask)

No afecta el APK. Solo push.

```bash
git add pc_server/
git commit -m "fix: descripcion del server"
git push
```

Los usuarios del server deben hacer `git pull` en el PC y reiniciar `start_server.bat`.

---

## Puntos críticos que NO hay que romper

### Desktop app

- **`main.py` línea 1**: `QSurfaceFormat` con OpenGL 3.3 Core DEBE ir antes de `QApplication`.
  Si se mueve, el visor 3D no renderiza nada.

- **`app/camera.py` near plane**: `near = max(self.distance * 0.001, 1e-4)`
  Si se pone un valor absoluto muy pequeño (ej: `1e-8`), la matriz VP se vuelve singular
  y el ray casting falla — el modo 3-point align no puede seleccionar puntos.

- **`core/picking.py` EPS de Möller-Trumbore**: El EPS es relativo al mesh:
  `EPS_PARALLEL = float(np.abs(a).max()) * 1e-8`
  Un EPS absoluto (ej: `1e-7`) falla para meshes pequeños (radio < 1cm) porque
  `|a| ≈ edge² ≈ 4e-10 < 1e-7` → todas las caras se marcan como paralelas.

- **`gl_widget.py` mousePressEvent**: Los modos ALIGN_3PT y CALIBRATE_3PT ambos
  deben estar en la condición de pick:
  `if self._mode in (Mode.ANNOTATE, Mode.ALIGN_3PT, Mode.CALIBRATE_3PT):`
  Omitir CALIBRATE_3PT hace que no se pueda seleccionar puntos en ese modo.

- **`app/renderer.py` GL_CLIP_DISTANCE**: Se habilitan con valores numéricos hexadecimales
  porque PyOpenGL no siempre expone las constantes:
  `GL.glEnable(0x3000)  # GL_CLIP_DISTANCE0`
  `GL.glEnable(0x3001)  # GL_CLIP_DISTANCE1`

### Android app

- **`android_app/main.py`**: `updater.inject_update_path()` DEBE ser la primera
  llamada, antes de cualquier `from screens.xxx import`. Si se mueve después,
  las actualizaciones descargadas no tienen efecto.

- **`android_app/version.py`**: Si se hace push a `android_app/` SIN incrementar
  VERSION, GitHub Actions publica un release con el mismo tag y falla porque
  el tag ya existe. Siempre incrementar VERSION antes de push.

- **`android_app/buildozer.spec`**: `p4a.hook = p4a_hook.py` es necesario para
  que el FileProvider quede en el AndroidManifest. Sin él, la instalación
  automática del APK falla en Android 7+ con SecurityException.

---

## Dependencias del proyecto

### Desktop (pip install)
```
PySide6>=6.5          # UI + OpenGL widget
trimesh>=4.0          # carga STL/GLB/OBJ/PLY/ZIP
numpy>=1.24           # arrays, math
scipy>=1.10           # KDTree para wear analysis
matplotlib>=3.7       # colormaps (usar matplotlib.colormaps.get_cmap, no cm.get_cmap)
PyOpenGL>=3.1         # GL calls en el renderer
flask>=3.0            # PC server
pycolmap>=4.0         # reconstrucción 3D (incluye COLMAP binario)
```

### Android (buildozer)
```
kivy==2.3.0, kivymd==1.1.1, requests, plyer, pillow
```

### Opcionales (mejoran calidad)
```
rtree                 # mejora distancia vértice→superficie en wear analysis
                      # si no está, usa KDTree (menos preciso pero funciona)
```

---

## Archivos de datos que NO van al repo (.gitignore)

- `*.refproj` — proyectos de usuario con meshes
- `*.npz`, `*.npy` — arrays numpy de meshes comprimidos
- `.refractory_calibration.json` — calibración de escala 3-point (local por PC)
- `.refractory_recent.json` — historial de proyectos recientes (local)
- `android_app/.buildozer/` — build artifacts de Buildozer (~3 GB)
- `android_app/bin/` — APKs compilados localmente
- `android_app/_updates/` — updates descargados en el celular

---

## Cómo arrancar el proyecto (setup inicial en PC nuevo)

```bash
git clone git@github.com:ledesmamatiasarispe/control_Desgaste_refractario.git
cd control_Desgaste_refractario
pip install -r requirements.txt
pip install PyOpenGL pycolmap flask  # extras del servidor
python main.py                        # lanza la app de escritorio
```

Para el servidor de captura:
```bash
cd pc_server
pip install -r requirements.txt
python server.py   # o doble clic en start_server.bat
```

---

## Flujo de trabajo diario

```bash
# Trabajar en el código
git status            # ver qué cambió
git diff              # revisar cambios antes de commitear

# Commitear
git add <archivos>
git commit -m "tipo: descripción corta"
git push

# Tipos de commit:
# feat:  nueva funcionalidad
# fix:   bug fix
# refactor: refactoring sin cambio de funcionalidad
# docs:  solo documentación
# chore: tareas de mantenimiento (deps, config)
```
