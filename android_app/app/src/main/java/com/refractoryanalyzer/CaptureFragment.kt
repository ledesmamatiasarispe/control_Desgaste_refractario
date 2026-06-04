package com.refractoryanalyzer

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.util.Log
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.navigation.fragment.findNavController
import androidx.navigation.fragment.navArgs
import androidx.core.content.edit
import com.google.ar.core.Anchor
import com.google.ar.core.ArCoreApk
import com.google.ar.core.CameraConfig
import com.google.ar.core.CameraConfigFilter
import com.google.ar.core.Config
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Frame
import com.google.ar.core.Pose
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.refractoryanalyzer.databinding.FragmentCaptureBinding
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import java.util.EnumSet
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10
import kotlin.math.sqrt
import kotlin.math.tan

class CaptureFragment : Fragment(), SensorEventListener, GLSurfaceView.Renderer {

    private var _binding: FragmentCaptureBinding? = null
    private val binding get() = _binding!!
    private val args: CaptureFragmentArgs by navArgs()

    private lateinit var sensorManager: SensorManager
    private lateinit var cameraExecutor: ExecutorService
    private lateinit var coverageOverlay: CoverageOverlayView

    private var surfaceWidth = 0
    private var surfaceHeight = 0

    private var arSession: Session? = null
    private var installRequested = false
    private var lastArPose: Pose? = null
    
    private var isCapturing = false
    private var isTakingPhoto = false
    private var captureJob: Job? = null
    
    private var isPhotoPending = false
    private var photoCallback: ((Int) -> Unit)? = null
    private var latestFrame: Frame? = null

    private val alignPoints = mutableListOf<AlignPoint>()
    private val alignAnchors = mutableMapOf<Int, Anchor>()
    private val cylinderAnchors = mutableListOf<Anchor>()
    private var lastRotationMatrix = FloatArray(9).apply { this[0]=1f; this[4]=1f; this[8]=1f }

    companion object {
        const val TARGET_FRAMES = 120
        const val INTERVAL_MS   = 150L
        const val GYRO_MAX      = 1.2f
    }

    private var lastAccel = FloatArray(3)
    private var lastGyro = FloatArray(3)
    private var lastOrient = FloatArray(3)
    private var gyroMag = 0f

    private val capturedFrames = mutableListOf<CapturedFrame>()
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .build()
    private var uploadLoopJob: Job? = null

    data class CapturedFrame(
        val frameId: Int,
        var jpegBytes: ByteArray?,
        val imu: JSONObject,
        val camera: JSONObject
    ) {
        override fun equals(other: Any?): Boolean {
            if (this === other) return true
            if (javaClass != other?.javaClass) return false
            other as CapturedFrame
            if (frameId != other.frameId) return false
            if (jpegBytes != null) {
                if (other.jpegBytes == null) return false
                if (!jpegBytes.contentEquals(other.jpegBytes)) return false
            } else if (other.jpegBytes != null) return false
            return true
        }

        override fun hashCode(): Int {
            var result = frameId
            result = 31 * result + (jpegBytes?.contentHashCode() ?: 0)
            return result
        }
    }

    data class AlignPoint(
        val index: Int,
        val frameId: Int,
        val worldX: Float,
        val worldY: Float,
        val worldZ: Float,
        val imuAccel: List<Float>,
        val imuGyro: List<Float>,
        val imuOrient: List<Float>
    )

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        if (isGranted) setupArSession()
        else {
            Toast.makeText(context, R.string.permiso_camara_requerido, Toast.LENGTH_SHORT).show()
            findNavController().popBackStack()
        }
    }

    private var currentAlignIndex = 0

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentCaptureBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        sensorManager = requireContext().getSystemService(Context.SENSOR_SERVICE) as SensorManager
        cameraExecutor = Executors.newSingleThreadExecutor()
        coverageOverlay = binding.coverageOverlay

        binding.surfaceView.setEGLContextClientVersion(2)
        binding.surfaceView.setRenderer(this)
        binding.surfaceView.renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY

        loadAlignPointsFromPrefs()
        checkPermissions()

        binding.btnCapture.setOnClickListener { if (args.isAutomatic) toggleCapture() else takePhoto() }
        binding.btnSend.setOnClickListener { viewPointCloud() }
        binding.btnBack.setOnClickListener { findNavController().popBackStack() }

        // Resume upload loop if returning from point cloud viewer
        if (FrameStore.currentJobId.isNotEmpty() && FrameStore.serverIp == args.serverIp) {
            capturedFrames.clear()
            startUploadLoop()
        } else {
            FrameStore.reset()
            FrameStore.serverIp = args.serverIp
            ensureJobCreated()
        }
        binding.btnCyl.setOnClickListener { markCylinderPoint() }
        binding.btnAlign.setOnClickListener { markAlignPoint() }

        updateUI()
        updateAlignButtonText()
    }

    private fun checkPermissions() {
        if (ContextCompat.checkSelfPermission(requireContext(), Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            setupArSession()
        } else {
            requestPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun setupArSession() {
        if (arSession != null) return
        try {
            val session = Session(requireContext())
            arSession = session

            // Limitar la cámara a 30fps para ahorrar batería y reducir calor
            val filter = CameraConfigFilter(session)
            filter.targetFps = EnumSet.of(CameraConfig.TargetFps.TARGET_FPS_30)
            val cameraConfigs = session.getSupportedCameraConfigs(filter)
            if (cameraConfigs.isNotEmpty()) {
                session.cameraConfig = cameraConfigs[0]
            }

            val config = Config(session)
            config.focusMode = Config.FocusMode.AUTO
            config.updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
            session.configure(config)
        } catch (_: Exception) { Log.e("CaptureFragment", "ARCore session failed") }
    }

    private fun toggleCapture() { if (isCapturing) stopAutoCapture() else startAutoCapture() }

    private fun startAutoCapture() {
        isCapturing = true
        binding.btnCapture.text = getString(R.string.btn_detener)
        captureJob = viewLifecycleOwner.lifecycleScope.launch {
            while (isCapturing && capturedFrames.size < TARGET_FRAMES) {
                if (gyroMag < GYRO_MAX) {
                    updateStabilityIndicator(true)
                    takePhoto()
                } else updateStabilityIndicator(false)
                delay(INTERVAL_MS)
            }
            if (capturedFrames.size >= TARGET_FRAMES) stopAutoCapture()
        }
    }

    private fun stopAutoCapture() {
        isCapturing = false
        captureJob?.cancel()
        captureJob = null
        binding.btnCapture.text = getString(R.string.btn_iniciar)
    }

    private fun takePhoto(onCaptured: ((Int) -> Unit)? = null) {
        if (isTakingPhoto || isPhotoPending) return
        photoCallback = onCaptured
        isPhotoPending = true
    }

    private fun processPhoto(image: android.media.Image, frameId: Int, onCaptured: ((Int) -> Unit)?) {
        val bytes = yuvToJpeg(image)
        val width = image.width
        val height = image.height
        image.close()

        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.Main) {
            capturedFrames.add(CapturedFrame(frameId, bytes, buildImuJson(), buildCameraJson(width, height)))
            coverageOverlay.markCaptured(lastOrient[0], lastOrient[1])
            updateUI()
            isTakingPhoto = false
            onCaptured?.invoke(frameId)
        }
    }

    private fun yuvToJpeg(image: android.media.Image): ByteArray {
        val yBuffer = image.planes[0].buffer
        val uBuffer = image.planes[1].buffer
        val vBuffer = image.planes[2].buffer
        val nv21 = ByteArray(yBuffer.remaining() + uBuffer.remaining() + vBuffer.remaining())
        yBuffer.get(nv21, 0, yBuffer.remaining())
        vBuffer.get(nv21, yBuffer.remaining(), vBuffer.remaining())
        uBuffer.get(nv21, yBuffer.remaining() + vBuffer.remaining(), uBuffer.remaining())
        val out = ByteArrayOutputStream()
        YuvImage(nv21, ImageFormat.NV21, image.width, image.height, null).compressToJpeg(Rect(0, 0, image.width, image.height), 90, out)
        return out.toByteArray()
    }

    private fun markCylinderPoint() {
        val session = arSession ?: return
        val pose = lastArPose?.compose(Pose.makeTranslation(0f, 0f, -0.01f)) ?: return
        
        try {
            val anchor = session.createAnchor(pose)
            cylinderAnchors.add(anchor)
            if (cylinderAnchors.size > 3) cylinderAnchors.removeAt(0)
            
            // Actualizar los puntos en el overlay
            val pts = cylinderAnchors.map { it.pose }
            coverageOverlay.resetCylinderPoints()
            pts.forEach { coverageOverlay.addCylinderWorldPoint(it.tx(), it.ty(), it.tz()) }
        } catch (_: Exception) {
            Log.e("CaptureFragment", "Failed to create cylinder anchor")
        }
    }

    private fun markAlignPoint() {
        val session = arSession ?: return
        takePhoto { frameId ->
            val pose = lastArPose?.compose(Pose.makeTranslation(0f, 0f, -0.01f)) ?: return@takePhoto
            
            try {
                val anchor = session.createAnchor(pose)
                alignAnchors[currentAlignIndex] = anchor
                
                val pt = AlignPoint(
                    index = currentAlignIndex,
                    frameId = frameId,
                    worldX = pose.tx(),
                    worldY = pose.ty(),
                    worldZ = pose.tz(),
                    imuAccel = lastAccel.toList(),
                    imuGyro = lastGyro.toList(),
                    imuOrient = lastOrient.toList()
                )
                alignPoints.removeAll { it.index == currentAlignIndex }
                alignPoints.add(pt)
                saveAlignPointsToPrefs()
                
                updateOverlayPoints()
                
                currentAlignIndex = (currentAlignIndex + 1) % 3
                updateAlignButtonText()
            } catch (_: Exception) {
                Log.e("CaptureFragment", "Failed to create alignment anchor")
            }
        }
    }

    private fun updateOverlayPoints() {
        val updatedPoints = alignPoints.map { pt ->
            val anchor = alignAnchors[pt.index]
            if (anchor != null && anchor.trackingState == TrackingState.TRACKING) {
                val p = anchor.pose
                pt.copy(worldX = p.tx(), worldY = p.ty(), worldZ = p.tz())
            } else pt
        }
        coverageOverlay.setAlignPoints(updatedPoints)
        
        if (cylinderAnchors.size == 3) {
            coverageOverlay.resetCylinderPoints()
            cylinderAnchors.forEach { anchor ->
                if (anchor.trackingState == TrackingState.TRACKING) {
                    val p = anchor.pose
                    coverageOverlay.addCylinderWorldPoint(p.tx(), p.ty(), p.tz())
                }
            }
        }
    }

    private fun updateAlignButtonText() {
        val colors = arrayOf(getString(R.string.rojo), getString(R.string.verde), getString(R.string.azul))
        binding.btnAlign.text = getString(R.string.marcar_color, colors[currentAlignIndex])
    }

    private fun saveAlignPointsToPrefs() {
        val json = JSONArray(alignPoints.map { pt ->
            JSONObject().apply {
                put("index", pt.index); put("frame_id", pt.frameId); put("worldX", pt.worldX); put("worldY", pt.worldY); put("worldZ", pt.worldZ)
                put("imu", JSONObject().apply { put("accel", JSONArray(pt.imuAccel)); put("gyro", JSONArray(pt.imuGyro)); put("orient", JSONArray(pt.imuOrient)) })
            }
        }).toString()
        requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE).edit(commit = true) {
            putString("align_pts_json", json)
        }
    }

    private fun loadAlignPointsFromPrefs() {
        val jsonStr = requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE).getString("align_pts_json", null) ?: return
        try {
            val array = JSONArray(jsonStr)
            alignPoints.clear()
            for (i in 0 until array.length()) {
                val obj = array.getJSONObject(i); val imu = obj.getJSONObject("imu")
                alignPoints.add(AlignPoint(index = obj.getInt("index"), frameId = obj.getInt("frame_id"), worldX = obj.optDouble("worldX", 0.0).toFloat(), worldY = obj.optDouble("worldY", 0.0).toFloat(), worldZ = obj.optDouble("worldZ", 0.0).toFloat(), imuAccel = jsonArrayToList(imu.getJSONArray("accel")), imuGyro = jsonArrayToList(imu.getJSONArray("gyro")), imuOrient = jsonArrayToList(imu.getJSONArray("orient"))) )
            }
            coverageOverlay.setAlignPoints(alignPoints)
        } catch (_: Exception) { Log.e("CaptureFragment", "Load points failed") }
    }

    private fun jsonArrayToList(array: JSONArray): List<Float> {
        val list = mutableListOf<Float>()
        for (i in 0 until array.length()) list.add(array.getDouble(i).toFloat())
        return list
    }

    private fun buildImuJson() = JSONObject().apply { put("accel", JSONArray(lastAccel.map { it })); put("gyro", JSONArray(lastGyro.map { it })); put("orient", JSONArray(lastOrient.map { it })) }
    private fun buildCameraJson(w: Int, h: Int) = JSONObject().apply { put("focal_px", (w.coerceAtLeast(h) / 2.0) / tan(Math.toRadians(35.0))); put("width", w); put("height", h) }

    private fun updateUI() {
        val count = capturedFrames.size
        binding.tvCounter.text = getString(R.string.fotos_counter, count, TARGET_FRAMES)
        binding.progressBar.progress = count
        binding.btnSend.isEnabled = count >= 30
        binding.btnSend.text = getString(R.string.btn_enviar, count)
        if (!args.isAutomatic && !isCapturing) binding.btnCapture.text = getString(R.string.btn_capturar)
    }

    private fun updateStabilityIndicator(stable: Boolean) {
        binding.tvGyro.text = if (stable) getString(R.string.estable) else getString(R.string.muy_rapido)
        binding.tvGyro.setTextColor(ContextCompat.getColor(requireContext(), if (stable) android.R.color.holo_green_light else android.R.color.holo_orange_light))
    }

    // ── streaming upload ──────────────────────────────────────────────────────

    private fun ensureJobCreated() {
        if (FrameStore.currentJobId.isNotEmpty()) { startUploadLoop(); return }
        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            try {
                val req = Request.Builder().url("http://${args.serverIp}:5005/new_job")
                    .post("".toRequestBody()).build()
                val jid = httpClient.newCall(req).execute().use {
                    JSONObject(it.body!!.string()).getString("job_id")
                }
                FrameStore.currentJobId = jid
                withContext(Dispatchers.Main) { updateUploadStatus() }
                startUploadLoop()
            } catch (e: Exception) {
                Log.e("CaptureFragment", "create job failed: $e")
            }
        }
    }

    private fun startUploadLoop() {
        uploadLoopJob?.cancel()
        uploadLoopJob = viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            while (isActive) {
                val pending = synchronized(capturedFrames) {
                    capturedFrames.filter {
                        it.frameId !in FrameStore.uploadedFrameIds && it.jpegBytes != null
                    }.toList()
                }
                for (frame in pending) {
                    if (!isActive) break
                    if (uploadFrame(frame)) {
                        FrameStore.uploadedFrameIds.add(frame.frameId)
                        frame.jpegBytes = null   // liberar memoria
                        withContext(Dispatchers.Main) { updateUploadStatus() }
                    }
                }
                delay(400)
            }
        }
    }

    private fun uploadFrame(frame: CapturedFrame): Boolean {
        val bytes = frame.jpegBytes ?: return true
        val jid   = FrameStore.currentJobId.takeIf { it.isNotEmpty() } ?: return false
        val meta  = JSONObject().apply {
            put("frame_id", frame.frameId); put("timestamp_ms", System.currentTimeMillis())
            put("imu", frame.imu); put("camera", frame.camera)
        }
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("image", "f.jpg", bytes.toRequestBody("image/jpeg".toMediaType()))
            .addFormDataPart("meta", meta.toString())
            .build()
        return try {
            httpClient.newCall(
                Request.Builder().url("http://${args.serverIp}:5005/upload_frame/$jid/${frame.frameId}")
                    .post(body).build()
            ).execute().use { it.isSuccessful }
        } catch (e: Exception) { false }
    }

    private fun updateUploadStatus() {
        val uploaded = FrameStore.uploadedFrameIds.size
        val total    = capturedFrames.size
        val jid      = FrameStore.currentJobId
        val label    = if (jid.isEmpty()) "Sin conexión" else "↑ $uploaded/$total subidas"
        binding.tvUploadStatus?.text = label
    }

    private fun viewPointCloud() {
        val jid = FrameStore.currentJobId
        if (jid.isEmpty() || capturedFrames.size < 5) {
            Toast.makeText(context, "Necesitás al menos 5 fotos capturadas", Toast.LENGTH_SHORT).show()
            return
        }
        stopAutoCapture()
        binding.btnSend.isEnabled = false

        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.IO) {
            // Esperar hasta que todas las fotos capturadas estén subidas
            val totalToUpload = capturedFrames.size
            while (isActive && FrameStore.uploadedFrameIds.size < totalToUpload) {
                withContext(Dispatchers.Main) {
                    val uploaded = FrameStore.uploadedFrameIds.size
                    binding.tvUploadStatus?.text = "Subiendo $uploaded/$totalToUpload…"
                }
                delay(500)
            }
            if (!isActive) return@launch

            withContext(Dispatchers.Main) {
                binding.tvUploadStatus?.text = "Iniciando análisis…"
            }

            try {
                val alignJson = requireContext()
                    .getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE)
                    .getString("align_pts_json", "[]")
                val json = JSONObject().apply {
                    put("total_frames", totalToUpload)
                    put("align_pts", JSONArray(alignJson))
                    put("mode", "preview")
                }
                httpClient.newCall(
                    Request.Builder().url("http://${args.serverIp}:5005/start_reconstruct/$jid")
                        .post(json.toString().toRequestBody("application/json".toMediaType()))
                        .build()
                ).execute().close()

                withContext(Dispatchers.Main) {
                    findNavController().navigate(
                        CaptureFragmentDirections.actionCaptureToPointCloud(args.serverIp, jid)
                    )
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    binding.btnSend.isEnabled = true
                    Toast.makeText(context, "Error: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    override fun onResume() {
        super.onResume()
        if (arSession == null) {
            if (ArCoreApk.getInstance().requestInstall(requireActivity(), !installRequested) == ArCoreApk.InstallStatus.INSTALL_REQUESTED) { installRequested = true; return }
            setupArSession()
        }
        try { arSession?.resume() } catch (_: Exception) { arSession = null }
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER), SensorManager.SENSOR_DELAY_UI)
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE), SensorManager.SENSOR_DELAY_UI)
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR), SensorManager.SENSOR_DELAY_UI)
        binding.surfaceView.onResume()
    }

    override fun onPause() {
        super.onPause()
        binding.surfaceView.onPause()
        arSession?.pause()
        sensorManager.unregisterListener(this)
        stopAutoCapture()
    }

    override fun onSensorChanged(event: SensorEvent) {
        when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> lastAccel = event.values.clone()
            Sensor.TYPE_GYROSCOPE -> { lastGyro = event.values.clone(); gyroMag = sqrt(lastGyro[0] * lastGyro[0] + lastGyro[1] * lastGyro[1] + lastGyro[2] * lastGyro[2]) }
            Sensor.TYPE_ROTATION_VECTOR -> {
                val rm = FloatArray(9); SensorManager.getRotationMatrixFromVector(rm, event.values); lastRotationMatrix = rm.clone()
                val orient = FloatArray(3); SensorManager.getOrientation(rm, orient)
                lastOrient[0] = Math.toDegrees(orient[0].toDouble()).toFloat(); lastOrient[1] = Math.toDegrees(orient[1].toDouble()).toFloat(); lastOrient[2] = Math.toDegrees(orient[2].toDouble()).toFloat()
                coverageOverlay.updateOrientation(lastOrient[0], lastOrient[1], lastOrient[2]); coverageOverlay.updateRotationMatrix(rm)
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    // GL Renderer & Background Rendering
    private var textureId = -1
    private var program = -1
    private val quadCoords = floatBufferOf(floatArrayOf(-1f, -1f, 0f, 1f, -1f, 0f, -1f, 1f, 0f, 1f, 1f, 0f))
    private val ndcCoords = floatBufferOf(floatArrayOf(-1f, -1f, 1f, -1f, -1f, 1f, 1f, 1f))
    private val transformedTexCoords = ByteBuffer.allocateDirect(8 * 4).order(ByteOrder.nativeOrder()).asFloatBuffer()

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        GLES20.glClearColor(0f, 0f, 0f, 1f)
        val textures = IntArray(1); GLES20.glGenTextures(1, textures, 0); textureId = textures[0]
        GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, textureId)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
        arSession?.setCameraTextureName(textureId)

        program = createDefaultProgram()
    }

    private fun createDefaultProgram(): Int {
        val vShader = "attribute vec4 a_Pos; attribute vec2 a_Tex; varying vec2 v_Tex; void main() { gl_Position = a_Pos; v_Tex = a_Tex; }"
        val fShader = "#extension GL_OES_EGL_image_external : require\nprecision mediump float; uniform samplerExternalOES s_Tex; varying vec2 v_Tex; void main() { gl_FragColor = texture2D(s_Tex, v_Tex); }"
        
        val vs = GLES20.glCreateShader(GLES20.GL_VERTEX_SHADER).apply { GLES20.glShaderSource(this, vShader); GLES20.glCompileShader(this) }
        val fs = GLES20.glCreateShader(GLES20.GL_FRAGMENT_SHADER).apply { GLES20.glShaderSource(this, fShader); GLES20.glCompileShader(this) }
        return GLES20.glCreateProgram().apply { GLES20.glAttachShader(this, vs); GLES20.glAttachShader(this, fs); GLES20.glLinkProgram(this) }
    }

    override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
        surfaceWidth = width
        surfaceHeight = height
        GLES20.glViewport(0, 0, width, height)
        arSession?.setDisplayGeometry(0, width, height)
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
        val session = arSession ?: return
        try {
            val frame = session.update()
            latestFrame = frame
            val arCamera = frame.camera
            lastArPose = arCamera.pose
            
            // Correct camera texture rotation/scaling
            frame.transformCoordinates2d(
                Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES,
                ndcCoords,
                Coordinates2d.TEXTURE_NORMALIZED,
                transformedTexCoords
            )

            // Draw background
            GLES20.glUseProgram(program)
            val posHandle = GLES20.glGetAttribLocation(program, "a_Pos")
            val texHandle = GLES20.glGetAttribLocation(program, "a_Tex")
            
            GLES20.glEnableVertexAttribArray(posHandle)
            GLES20.glVertexAttribPointer(posHandle, 3, GLES20.GL_FLOAT, false, 0, quadCoords)
            
            GLES20.glEnableVertexAttribArray(texHandle)
            GLES20.glVertexAttribPointer(texHandle, 2, GLES20.GL_FLOAT, false, 0, transformedTexCoords)
            
            GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
            GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES, textureId)
            GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)

            if (arCamera.trackingState == TrackingState.TRACKING) {
                activity?.runOnUiThread { 
                    if (_binding == null) return@runOnUiThread
                    updateOverlayPoints() // Actualizar posiciones desde los anchors
                    coverageOverlay.updateArCamera(arCamera)
                    binding.tvStatus.text = getString(R.string.status_tracking_ok)
                }

                if (isPhotoPending) {
                    isPhotoPending = false
                    isTakingPhoto = true
                    try {
                        val image = frame.acquireCameraImage()
                        val frameId = capturedFrames.size
                        val callback = photoCallback
                        photoCallback = null
                        // Procesar en segundo plano para no bloquear el renderizado
                        cameraExecutor.execute { processPhoto(image, frameId, callback) }
                    } catch (_: Exception) {
                        isTakingPhoto = false
                        Log.e("CaptureFragment", "Frame acquisition failed")
                    }
                }
            } else {
                activity?.runOnUiThread { 
                    if (_binding == null) return@runOnUiThread
                    binding.tvStatus.text = getString(R.string.status_buscando_superficie)
                }
            }
        } catch (_: Exception) {
            Log.e("CaptureFragment", "Draw frame failed")
        }
    }

    private fun floatBufferOf(data: FloatArray): FloatBuffer = ByteBuffer.allocateDirect(data.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().put(data).apply { position(0) }

    override fun onDestroyView() {
        super.onDestroyView()
        uploadLoopJob?.cancel()
        cameraExecutor.shutdown()
        _binding = null
    }
}
