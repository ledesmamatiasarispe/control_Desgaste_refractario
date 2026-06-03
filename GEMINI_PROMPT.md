# Prompt para Gemini — App Android Nativa: Refractory Capture

## Contexto del proyecto

Existe una aplicación de escritorio Windows llamada **Refractory Analyzer** que analiza el desgaste del refractario de hornos de inducción a partir de modelos 3D (STL/GLB). Los usuarios realizan escaneos periódicos del horno con fotogrametría y comparan campañas para ver el desgaste acumulado.

Necesitamos una app Android nativa (Kotlin) que permita capturar fotogramas del horno junto con datos de sensores IMU, enviarlos a un servidor Python que corre en el PC del usuario por WiFi, y monitorear el progreso de la reconstrucción 3D.

---

## Lo que necesitás construir

Una app Android nativa en **Kotlin** con **Android Studio**, que tenga tres pantallas navegadas con Navigation Component:

1. **Pantalla Conectar** — ingreso de IP del PC, verificación de conexión
2. **Pantalla Capturar** — preview de cámara + captura automática con datos IMU
3. **Pantalla Progreso** — upload de frames al servidor + polling de reconstrucción

---

## Especificación completa de la API del servidor

El servidor es un **Flask Python** que corre en el PC del usuario en el **puerto 5005**.
Todos los endpoints son HTTP/1.1. No hay autenticación.

### `GET /ping`
Verifica que el servidor esté activo.

**Respuesta 200:**
```json
{
  "ok": true,
  "server": "Refractory Capture Server 1.0",
  "hostname": "NOMBRE_DEL_PC"
}
```

---

### `POST /new_job`
Crea un nuevo trabajo de reconstrucción.

**Body:** vacío

**Respuesta 200:**
```json
{
  "job_id": "abc12345"
}
```

---

### `POST /upload_frame/{job_id}/{frame_id}`
Sube un fotograma individual con sus datos IMU.

**Body:** `multipart/form-data`

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `image` | file (JPEG) | Fotograma comprimido como JPEG |
| `meta` | string (JSON) | Metadatos del frame (ver formato abajo) |

**Formato del JSON en `meta`:**
```json
{
  "frame_id": 0,
  "timestamp_ms": 1717600000000,
  "imu": {
    "accel":  [0.12, -9.81, 0.05],
    "gyro":   [0.01, -0.02, 0.003],
    "orient": [2.5, 88.3, 45.1]
  },
  "camera": {
    "focal_px": 1440.5,
    "width": 1280,
    "height": 720
  }
}
```

- `accel`: acelerómetro en m/s²  [x, y, z]
- `gyro`: giroscopio en rad/s  [x, y, z]
- `orient`: orientación en grados  [roll, pitch, yaw]
- `focal_px`: distancia focal estimada en píxeles

**Respuesta 200:**
```json
{
  "ok": true,
  "received": 5
}
```

**Respuesta 400** si el trabajo no existe o no está en estado aceptable.

---

### `GET /received_frames/{job_id}`
Devuelve qué frames ya recibió el servidor (para reanudar uploads interrumpidos).

**Respuesta 200:**
```json
{
  "frames": [0, 1, 2, 4, 5]
}
```

---

### `POST /start_reconstruct/{job_id}`
Inicia la reconstrucción 3D en el servidor (proceso en background).

**Body:** `application/json`
```json
{
  "total_frames": 60
}
```

**Respuesta 200:**
```json
{
  "ok": true
}
```

**Respuesta 400** si hay menos de 5 frames subidos.

---

### `GET /status/{job_id}`
Polling del estado de reconstrucción.

**Respuesta 200:**
```json
{
  "job_id": "abc12345",
  "status": "running",
  "progress": 45,
  "message": "Extrayendo características SIFT…",
  "received_frames": [0, 1, 2, 3, 4],
  "total_frames": 60,
  "output_path": null,
  "error": null
}
```

**Valores posibles de `status`:**
- `"waiting"` — trabajo creado, esperando frames
- `"uploading"` — recibiendo frames
- `"running"` — reconstrucción en proceso
- `"done"` — terminó, mesh disponible
- `"error"` — falló, ver campo `error`

**`progress`:** entero 0-100.

---

### `GET /download/{job_id}`
Descarga el mesh resultante (OBJ o STL).

Solo disponible cuando `status == "done"`.

**Respuesta 200:** archivo binario OBJ/STL como attachment.

---

## Lógica de upload con resume

La app DEBE implementar upload con resume ante cortes de red:

1. `POST /new_job` → obtener `job_id`
2. `GET /received_frames/{job_id}` → obtener lista de frames ya recibidos
3. Para cada frame **no recibido**: `POST /upload_frame/{job_id}/{frame_id}` con retry hasta 3 veces
4. `POST /start_reconstruct/{job_id}` cuando todos los frames estén subidos
5. Polling cada 3 segundos a `GET /status/{job_id}` hasta `status == "done"` o `"error"`

---

## Datos IMU requeridos por frame

Por cada fotograma la app debe capturar **simultáneamente**:

| Sensor Android | API | Descripción |
|---|---|---|
| `Sensor.TYPE_ACCELEROMETER` | `SensorManager` | m/s² — 3 ejes |
| `Sensor.TYPE_GYROSCOPE` | `SensorManager` | rad/s — 3 ejes |
| `Sensor.TYPE_ROTATION_VECTOR` | `SensorManager` | para obtener roll/pitch/yaw |

**Filtro de calidad:** descartar frames donde la magnitud del giroscopio sea > 0.6 rad/s (frame movido/trepidante):
```kotlin
val gyroMag = sqrt(gx*gx + gy*gy + gz*gz)
if (gyroMag > 0.6f) { /* descartar frame */ }
```

**Focal length estimada:**
```kotlin
// FOV típico de smartphone ~70°
val focalPx = (maxOf(width, height) / 2.0) / tan(Math.toRadians(35.0))
```

---

## Especificación de pantallas

### Pantalla 1: Conectar (`ConnectFragment`)

**UI:**
- `TextInputEditText` para IP del PC (ej: `192.168.1.100`)
- Botón "CONECTAR"
- `ProgressBar` indeterminada mientras conecta
- `TextView` de estado ("Conectando…" / "✓ Conectado" / "No encontrado")

**Lógica:**
- Al tocar CONECTAR: `GET http://{ip}:5005/ping` con timeout de 4 segundos
- Si responde `{"ok": true}` → navegar a Capturar pasando la IP como argumento
- Si falla → mostrar error en el `TextView`
- Guardar la última IP en `SharedPreferences` y cargarla al abrir

---

### Pantalla 2: Capturar (`CaptureFragment`)

**UI:**
- `PreviewView` (CameraX) que ocupa ~65% de la pantalla
- Indicador de estabilidad ("● Estable" en verde / "⚠ Muy rápido" en naranja)
- Contador de fotos `"23 / 60 fotos"`
- `ProgressBar` horizontal 0-60
- Botón "● INICIAR" / "■ DETENER" (toggle)
- Botón "ENVIAR X →" (deshabilitado hasta tener ≥30 fotos, habilitado al tener ≥30)
- Texto guía "Rodeá el horno lentamente"

**Lógica de captura:**
- Usar **CameraX** (`ImageCapture`) para capturar frames JPEG
- Captura automática cada 300ms cuando el giroscopio indica estabilidad
- Redimensionar imágenes a máximo 1280×720 antes de guardar
- Almacenar frames en memoria como lista de `CapturedFrame(frameId, jpegBytes, imu, camera)`
- Pausar la captura si `gyroMag > 0.6 rad/s` y actualizar el indicador
- Al llegar a 60 fotos: detener automáticamente y mostrar mensaje

**Permisos necesarios:** `CAMERA`

---

### Pantalla 3: Progreso (`ProgressFragment`)

**UI:**
- Título "Procesando escaneo"
- `TextView` "Subiendo: 23/60 fotos"
- `ProgressBar` horizontal 0-100 (50% = upload completo, 50-100% = reconstrucción)
- `TextView` de estado ("Conectando…" / "Subiendo 23/60…" / "Calculando SfM…" / "✓ Mesh listo")
- Botón "Cancelar" / "← Volver" (cambia de texto cuando termina)
- Botón "Reintentar" (visible solo si hubo error)

**Lógica:**
- Implementar el flujo de upload con resume descrito arriba
- Coroutines en `lifecycleScope` para las llamadas HTTP (no bloquear UI)
- Al terminar con `status == "done"`: mostrar "✓ Mesh generado en D:\\stl hornos\\reconstructions\\ en el PC"
- Polling cada 3 segundos usando `delay(3000)` en coroutine

---

## Dependencias a usar (Gradle `libs.versions.toml`)

```toml
[versions]
agp              = "8.7.3"
kotlin           = "1.9.25"
coreKtx          = "1.13.1"
appcompat        = "1.7.0"
material         = "1.12.0"
constraintlayout = "2.2.0"
navigationKtx    = "2.8.4"
cameraX          = "1.3.4"
okhttp           = "4.12.0"
lifecycleKtx     = "2.8.7"

[libraries]
androidx-core-ktx                = { group = "androidx.core", name = "core-ktx", version.ref = "coreKtx" }
androidx-appcompat               = { group = "androidx.appcompat", name = "appcompat", version.ref = "appcompat" }
material                         = { group = "com.google.android.material", name = "material", version.ref = "material" }
androidx-constraintlayout        = { group = "androidx.constraintlayout", name = "constraintlayout", version.ref = "constraintlayout" }
androidx-navigation-fragment-ktx = { group = "androidx.navigation", name = "navigation-fragment-ktx", version.ref = "navigationKtx" }
androidx-navigation-ui-ktx       = { group = "androidx.navigation", name = "navigation-ui-ktx", version.ref = "navigationKtx" }
androidx-camera-core             = { group = "androidx.camera", name = "camera-core", version.ref = "cameraX" }
androidx-camera-camera2          = { group = "androidx.camera", name = "camera-camera2", version.ref = "cameraX" }
androidx-camera-lifecycle        = { group = "androidx.camera", name = "camera-lifecycle", version.ref = "cameraX" }
androidx-camera-view             = { group = "androidx.camera", name = "camera-view", version.ref = "cameraX" }
okhttp                           = { group = "com.squareup.okhttp3", name = "okhttp", version.ref = "okhttp" }
androidx-lifecycle-viewmodel-ktx = { group = "androidx.lifecycle", name = "lifecycle-viewmodel-ktx", version.ref = "lifecycleKtx" }
androidx-lifecycle-livedata-ktx  = { group = "androidx.lifecycle", name = "lifecycle-livedata-ktx", version.ref = "lifecycleKtx" }

[plugins]
android-application = { id = "com.android.application", version.ref = "agp" }
kotlin-android      = { id = "org.jetbrains.kotlin.android", version.ref = "kotlin" }
```

---

## Configuración Android (`app/build.gradle.kts`)

```kotlin
android {
    namespace   = "com.refractoryanalyzer"
    compileSdk  = 35

    defaultConfig {
        applicationId = "com.refractoryanalyzer"
        minSdk        = 26
        targetSdk     = 35
        versionCode   = 1
        versionName   = "1.0"
    }

    buildFeatures {
        viewBinding = true
    }
}
```

---

## Permisos (`AndroidManifest.xml`)

```xml
<uses-permission android:name="android.permission.CAMERA" />
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

<uses-feature android:name="android.hardware.camera" android:required="true" />
<uses-feature android:name="android.hardware.sensor.accelerometer" />
<uses-feature android:name="android.hardware.sensor.gyroscope" />
```

Solicitar permiso `CAMERA` en runtime usando `ActivityResultContracts.RequestPermission()`.

---

## Estructura de archivos esperada

```
android_app/
├── settings.gradle.kts
├── build.gradle.kts
├── gradle.properties
├── gradle/
│   ├── libs.versions.toml
│   └── wrapper/gradle-wrapper.properties
└── app/
    ├── build.gradle.kts
    ├── proguard-rules.pro
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/refractoryanalyzer/
        │   ├── MainActivity.kt
        │   ├── ConnectFragment.kt
        │   ├── CaptureFragment.kt
        │   └── ProgressFragment.kt
        └── res/
            ├── layout/
            │   ├── activity_main.xml
            │   ├── fragment_connect.xml
            │   ├── fragment_capture.xml
            │   └── fragment_progress.xml
            ├── navigation/nav_graph.xml
            └── values/
                ├── strings.xml
                ├── colors.xml
                └── themes.xml
```

---

## Requerimientos técnicos adicionales

- **Arquitectura:** Single Activity + Navigation Component + Fragments
- **Concurrencia:** Kotlin Coroutines (`lifecycleScope.launch`) para HTTP. No usar `AsyncTask`.
- **HTTP:** OkHttp3 con `suspend fun` wrapping usando `suspendCancellableCoroutine` o ejecutando en `Dispatchers.IO`
- **ViewBinding:** habilitado, usar `binding.xxx` para acceder a vistas
- **Navegación:** Safe Args para pasar `serverIp: String` entre fragments
- **Tema:** Material Design 3 oscuro (`Theme.Material3.DayNight`)
- **Orientación:** solo portrait (`android:screenOrientation="portrait"`)
- **minSdk:** 26 (Android 8.0) para compatibilidad con Sensor APIs y CameraX
- **targetSdk:** 35 (Android 15)

---

## Lo que Gemini debe generar

Todos los archivos Kotlin y XML necesarios para que el proyecto compile y funcione directamente al abrirlo en Android Studio, incluyendo:

1. Todos los archivos `.kt` con lógica completa (no stubs)
2. Todos los archivos XML de layout con IDs correctos que coincidan con el ViewBinding
3. `nav_graph.xml` con argumentos Safe Args correctos
4. `AndroidManifest.xml` completo con permisos
5. Solicitud de permiso de cámara en runtime en `CaptureFragment`
6. Manejo de errores HTTP (retry, timeout, mensajes al usuario)
7. El flujo completo de upload con resume ante cortes de red

El código debe ser limpio, idiomático en Kotlin moderno, y compilar sin errores.
