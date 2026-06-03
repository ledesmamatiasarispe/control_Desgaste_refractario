# Prompt para Gemini — Overlay AR en CaptureFragment

## Contexto

App Android nativa (Kotlin, Android Studio) para captura fotogramétrica del interior de hornos de inducción cilíndricos.

El archivo `CaptureFragment.kt` ya existe con:
- Captura automática de fotos cada 150ms con CameraX
- Datos IMU: `lastAccel`, `lastGyro`, `lastOrient` actualizados por `onSensorChanged`
- `capturedFrames: MutableList<CapturedFrame>` con los frames capturados
- Lock de foco/exposición al iniciar (`lockFocusAndExposure()`)

También ya existe: `CoverageOverlayView.kt` con anillo de cobertura yaw, barra pitch, flecha guía y % cobertura.

**Gemini debe extender `CoverageOverlayView.kt`, actualizar `fragment_capture.xml` y conectar todo en `CaptureFragment.kt`.**

---

## Dos juegos de 3 puntos a implementar

### Juego A — Cilindro guía (🟡 amarillo, SOLO VISUAL, no se envía al servidor)

El usuario apunta el teléfono a la pared del horno y presiona botones para definir el cilindro.

**Flujo:**
1. Botones "Cil 1", "Cil 2", "Cil 3" en el layout
2. Al presionar cada uno: guarda el pixel central `(width/2f, height/2f)` en `cylinderPts: MutableList<PointF>`
3. Con 3 puntos: calcular **circuncentro** → centro del cilindro + radio
4. Dibujar como **elipse** ajustada por pitch del IMU:
   - `radiusX = radio`
   - `radiusY = radio * abs(sin(currentPitch * PI / 180))`
5. Guarda solo en memoria (no persiste, no se envía)

**Cálculo del circuncentro:**
```kotlin
fun circumcenter(a: PointF, b: PointF, c: PointF): PointF {
    val D = 2 * (a.x*(b.y-c.y) + b.x*(c.y-a.y) + c.x*(a.y-b.y))
    if (abs(D) < 1e-6f) return PointF((a.x+b.x+c.x)/3, (a.y+b.y+c.y)/3)
    val ux = ((a.x*a.x+a.y*a.y)*(b.y-c.y) + (b.x*b.x+b.y*b.y)*(c.y-a.y) + (c.x*c.x+c.y*c.y)*(a.y-b.y)) / D
    val uy = ((a.x*a.x+a.y*a.y)*(c.x-b.x) + (b.x*b.x+b.y*b.y)*(a.x-c.x) + (c.x*c.x+c.y*c.y)*(b.x-a.x)) / D
    return PointF(ux, uy)
}
```

---

### Juego B — Puntos de alineación (🔴🟢🔵, SE ENVÍAN al servidor)

Los 3 pernos/marcas físicas permanentes del horno que permiten alinear este escaneo con anteriores.

**Data class a crear:**
```kotlin
data class AlignPoint(
    val index: Int,              // 0, 1 o 2
    val frameId: Int,
    val px: Int,                 // siempre width/2
    val py: Int,                 // siempre height/2
    val imuAccel: List<Float>,
    val imuGyro: List<Float>,
    val imuOrient: List<Float>   // [roll, pitch, yaw]
)
```

**Flujo:**
1. Botones "Al 1" (rojo), "Al 2" (verde), "Al 3" (azul)
2. Al presionar: tomar foto + crear `AlignPoint` + guardar en SharedPreferences
3. Mostrar marcador permanente en overlay (punto grande + número)
4. Los puntos persisten entre sesiones (SharedPreferences con key `"align_pts_json"`)

**Envío al servidor (en `ProgressFragment` o antes de `startReconstruction`):**
```json
{
  "align_pts": [
    {"index":0, "frame_id":5, "px":640, "py":360,
     "imu":{"accel":[0.1,-9.8,0.0],"gyro":[0.01,0.02,0.01],"orient":[2.5,88.3,45.1]}},
    {"index":1, "frame_id":12, "px":640, "py":360, "imu":{...}},
    {"index":2, "frame_id":23, "px":640, "py":360, "imu":{...}}
  ]
}
```
Este JSON va como campo adicional en el body JSON de `POST /start_reconstruct/{job_id}`.

---

## Crosshair (puntero central fijo)

Siempre visible en el centro exacto de la pantalla:
```kotlin
private fun drawCrosshair(canvas: Canvas) {
    val cx = width / 2f
    val cy = height / 2f
    val r  = 32f
    // Semitransparente blanco
    val paintRing = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(160, 255, 255, 255); style = Paint.Style.STROKE; strokeWidth = 2f
    }
    val paintLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(180, 255, 255, 255); strokeWidth = 1.5f
    }
    val paintDot = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; style = Paint.Style.FILL
    }
    canvas.drawCircle(cx, cy, r, paintRing)
    canvas.drawLine(cx - r*1.6f, cy, cx + r*1.6f, cy, paintLine)
    canvas.drawLine(cx, cy - r*1.6f, cx, cy + r*1.6f, paintLine)
    canvas.drawCircle(cx, cy, 4f, paintDot)
}
```
Cuando hay un punto de alineación marcado cerca (< 60px del centro): anillo exterior verde.

---

## Métodos públicos a agregar en `CoverageOverlayView`

```kotlin
// Juego A
fun addCylinderPoint(x: Float, y: Float)    // agrega punto, calcula cilindro al 3°
fun clearCylinderPoints()

// Juego B
fun addAlignPoint(pt: AlignPoint)
fun clearAlignPoints()
fun setAlignPoints(pts: List<AlignPoint>)   // restaurar desde SharedPreferences

// Ya existen — conectar en CaptureFragment:
fun updateOrientation(yaw: Float, pitch: Float)
fun markCaptured(yaw: Float, pitch: Float)
fun reset()
fun coveragePercent(): Int
```

---

## Cambios en `fragment_capture.xml`

1. Envolver el `PreviewView` en `FrameLayout`
2. Agregar `CoverageOverlayView` encima con `match_parent`
3. Agregar 2 filas de botones compactos antes de los botones existentes:

```xml
<!-- Fila 1: cilindro -->
<LinearLayout android:layout_width="match_parent" android:layout_height="wrap_content"
    android:orientation="horizontal" android:gravity="center">
    <com.google.android.material.button.MaterialButton
        android:id="@+id/btn_cyl1" android:layout_width="0dp" android:layout_weight="1"
        style="@style/Widget.Material3.Button.TonalButton"
        android:text="🟡 Cil.1" android:textSize="11sp"/>
    <com.google.android.material.button.MaterialButton
        android:id="@+id/btn_cyl2" android:layout_width="0dp" android:layout_weight="1"
        style="@style/Widget.Material3.Button.TonalButton"
        android:text="🟡 Cil.2" android:textSize="11sp"/>
    <com.google.android.material.button.MaterialButton
        android:id="@+id/btn_cyl3" android:layout_width="0dp" android:layout_weight="1"
        style="@style/Widget.Material3.Button.TonalButton"
        android:text="🟡 Cil.3" android:textSize="11sp"/>
</LinearLayout>

<!-- Fila 2: alineación -->
<LinearLayout android:layout_width="match_parent" android:layout_height="wrap_content"
    android:orientation="horizontal" android:gravity="center">
    <com.google.android.material.button.MaterialButton
        android:id="@+id/btn_al1" android:layout_width="0dp" android:layout_weight="1"
        style="@style/Widget.Material3.Button.TonalButton"
        android:text="🔴 Al.1" android:textSize="11sp"/>
    <com.google.android.material.button.MaterialButton
        android:id="@+id/btn_al2" android:layout_width="0dp" android:layout_weight="1"
        style="@style/Widget.Material3.Button.TonalButton"
        android:text="🟢 Al.2" android:textSize="11sp"/>
    <com.google.android.material.button.MaterialButton
        android:id="@+id/btn_al3" android:layout_width="0dp" android:layout_weight="1"
        style="@style/Widget.Material3.Button.TonalButton"
        android:text="🔵 Al.3" android:textSize="11sp"/>
</LinearLayout>
```

---

## Cambios en `CaptureFragment.kt`

### 1. Inicializar overlay y restaurar puntos de alineación
```kotlin
private lateinit var coverageOverlay: CoverageOverlayView
private val alignPoints = mutableListOf<AlignPoint>()

// En onViewCreated():
coverageOverlay = binding.coverageOverlay
loadAlignPointsFromPrefs()  // restaurar puntos guardados
```

### 2. Conectar IMU al overlay
```kotlin
// En onSensorChanged, después de actualizar lastOrient:
coverageOverlay.updateOrientation(lastOrient[2], lastOrient[1])  // yaw, pitch
```

### 3. Conectar captura al overlay
```kotlin
// En onCaptureSuccess, después de capturedFrames.add(frame):
coverageOverlay.markCaptured(lastOrient[2], lastOrient[1])
```

### 4. Botones cilindro (Juego A)
```kotlin
binding.btnCyl1.setOnClickListener { coverageOverlay.addCylinderPoint(binding.viewFinder.width/2f, binding.viewFinder.height/2f) }
binding.btnCyl2.setOnClickListener { coverageOverlay.addCylinderPoint(binding.viewFinder.width/2f, binding.viewFinder.height/2f) }
binding.btnCyl3.setOnClickListener { coverageOverlay.addCylinderPoint(binding.viewFinder.width/2f, binding.viewFinder.height/2f) }
```

### 5. Botones alineación (Juego B)
```kotlin
private fun markAlignPoint(index: Int) {
    takePhotoForAlign { jpegBytes ->
        val pt = AlignPoint(
            index = index,
            frameId = capturedFrames.size,
            px = binding.viewFinder.width / 2,
            py = binding.viewFinder.height / 2,
            imuAccel = lastAccel.toList(),
            imuGyro  = lastGyro.toList(),
            imuOrient = lastOrient.toList()
        )
        // Si ya había uno con ese index, reemplazar
        alignPoints.removeAll { it.index == index }
        alignPoints.add(pt)
        saveAlignPointsToPrefs()
        coverageOverlay.addAlignPoint(pt)
    }
}
binding.btnAl1.setOnClickListener { markAlignPoint(0) }
binding.btnAl2.setOnClickListener { markAlignPoint(1) }
binding.btnAl3.setOnClickListener { markAlignPoint(2) }
```

### 6. Persistencia (SharedPreferences)
```kotlin
private fun saveAlignPointsToPrefs() {
    val json = JSONArray(alignPoints.map { pt ->
        JSONObject().apply {
            put("index", pt.index); put("frame_id", pt.frameId)
            put("px", pt.px); put("py", pt.py)
            put("imu", JSONObject().apply {
                put("accel", JSONArray(pt.imuAccel))
                put("gyro",  JSONArray(pt.imuGyro))
                put("orient",JSONArray(pt.imuOrient))
            })
        }
    }).toString()
    requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE)
        .edit().putString("align_pts_json", json).apply()
}

private fun loadAlignPointsFromPrefs() {
    val json = requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE)
        .getString("align_pts_json", null) ?: return
    // parsear JSONArray → lista AlignPoint → coverageOverlay.setAlignPoints(...)
}
```

### 7. Enviar al servidor (en ProgressFragment o al construir el body de startReconstruct)
```kotlin
// Leer align_pts desde SharedPreferences y agregarlos al JSON de start_reconstruct
val alignJson = requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE)
    .getString("align_pts_json", "[]")
val body = JSONObject().apply {
    put("total_frames", frames.size)
    put("align_pts", JSONArray(alignJson))
}
```

---

## Paleta de colores

| Elemento | Color ARGB |
|---|---|
| Cilindro elipse | `argb(80, 255, 220, 0)` fill + `argb(200, 255, 200, 0)` stroke 3dp |
| Punto cil. marcado | `argb(200, 255, 200, 0)` círculo radio 12dp |
| Al.1 (rojo) | `argb(220, 220, 60, 60)` radio 18dp |
| Al.2 (verde) | `argb(220, 60, 200, 80)` radio 18dp |
| Al.3 (azul) | `argb(220, 60, 120, 220)` radio 18dp |
| Número del punto | Blanco bold 14sp, sombra negra |
| Crosshair normal | `argb(160, 255, 255, 255)` |
| Crosshair con align cerca | `argb(220, 80, 220, 100)` verde |
