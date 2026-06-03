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
import com.google.ar.core.*
import com.google.ar.core.exceptions.*
import com.refractoryanalyzer.databinding.FragmentCaptureBinding
import kotlinx.coroutines.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
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

    private var arSession: Session? = null
    private var installRequested = false
    private var lastArPose: Pose? = null
    
    private var isCapturing = false
    private var isTakingPhoto = false
    private var captureJob: Job? = null

    private val alignPoints = mutableListOf<AlignPoint>()
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

    data class CapturedFrame(
        val frameId: Int,
        var jpegBytes: ByteArray?,
        val imu: JSONObject,
        val camera: JSONObject
    )

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
            Toast.makeText(context, "Permiso de cámara requerido", Toast.LENGTH_SHORT).show()
            findNavController().popBackStack()
        }
    }

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
        binding.btnSend.setOnClickListener { sendData() }
        binding.btnBack.setOnClickListener { findNavController().popBackStack() }
        binding.btnCyl1.setOnClickListener { markCylinderPoint() }
        binding.btnCyl2.setOnClickListener { markCylinderPoint() }
        binding.btnCyl3.setOnClickListener { markCylinderPoint() }
        binding.btnAl1.setOnClickListener { markAlignPoint(0) }
        binding.btnAl2.setOnClickListener { markAlignPoint(1) }
        binding.btnAl3.setOnClickListener { markAlignPoint(2) }

        updateUI()
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
            arSession = Session(requireContext())
            val config = Config(arSession)
            config.focusMode = Config.FocusMode.AUTO
            config.updateMode = Config.UpdateMode.LATEST_CAMERA_IMAGE
            arSession?.configure(config)
        } catch (e: Exception) { Log.e("CaptureFragment", "ARCore session failed", e) }
    }

    private fun toggleCapture() { if (isCapturing) stopAutoCapture() else startAutoCapture() }

    private fun startAutoCapture() {
        isCapturing = true
        binding.btnCapture.text = "■ DETENER"
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
        binding.btnCapture.text = "● INICIAR"
    }

    private fun takePhoto(onCaptured: ((Int) -> Unit)? = null) {
        if (isTakingPhoto) return
        val session = arSession ?: return
        isTakingPhoto = true
        viewLifecycleOwner.lifecycleScope.launch(Dispatchers.Default) {
            try {
                var frame: Frame? = null
                repeat(10) { if (frame?.camera?.trackingState != TrackingState.TRACKING) { frame = session.update(); if (frame?.camera?.trackingState != TrackingState.TRACKING) delay(50) } }
                val image = frame?.acquireCameraImage() ?: throw Exception("No image")
                val bytes = yuvToJpeg(image)
                val width = image.width
                val height = image.height
                image.close()
                withContext(Dispatchers.Main) {
                    isTakingPhoto = false
                    val frameId = capturedFrames.size
                    capturedFrames.add(CapturedFrame(frameId, bytes, buildImuJson(), buildCameraJson(width, height)))
                    coverageOverlay.markCaptured(lastOrient[0], lastOrient[1])
                    updateUI()
                    onCaptured?.invoke(frameId)
                }
            } catch (e: Exception) { isTakingPhoto = false; Log.e("CaptureFragment", "Photo failed", e) }
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
        val pose = lastArPose ?: return
        coverageOverlay.addCylinderWorldPoint(pose.tx(), pose.ty(), pose.tz())
    }

    private fun markAlignPoint(index: Int) {
        takePhoto { frameId ->
            val pose = lastArPose ?: return@takePhoto
            val pt = AlignPoint(index = index, frameId = frameId, worldX = pose.tx(), worldY = pose.ty(), worldZ = pose.tz(), imuAccel = lastAccel.toList(), imuGyro = lastGyro.toList(), imuOrient = lastOrient.toList())
            alignPoints.removeAll { it.index == index }
            alignPoints.add(pt)
            saveAlignPointsToPrefs()
            coverageOverlay.addAlignPoint(pt)
        }
    }

    private fun saveAlignPointsToPrefs() {
        val json = JSONArray(alignPoints.map { pt ->
            JSONObject().apply {
                put("index", pt.index); put("frame_id", pt.frameId); put("worldX", pt.worldX); put("worldY", pt.worldY); put("worldZ", pt.worldZ)
                put("imu", JSONObject().apply { put("accel", JSONArray(pt.imuAccel)); put("gyro", JSONArray(pt.imuGyro)); put("orient", JSONArray(pt.imuOrient)) })
            }
        }).toString()
        requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE).edit().putString("align_pts_json", json).apply()
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
        } catch (e: Exception) { Log.e("CaptureFragment", "Load points failed", e) }
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
        binding.tvCounter.text = "$count / $TARGET_FRAMES fotos"
        binding.progressBar.progress = count
        binding.btnSend.isEnabled = count >= 30
        binding.btnSend.text = "ENVIAR $count →"
        if (!args.isAutomatic && !isCapturing) binding.btnCapture.text = "● CAPTURAR"
    }

    private fun updateStabilityIndicator(stable: Boolean) {
        binding.tvGyro.text = if (stable) "● Estable" else "⚠ Muy rápido"
        binding.tvGyro.setTextColor(ContextCompat.getColor(requireContext(), if (stable) android.R.color.holo_green_light else android.R.color.holo_orange_light))
    }

    private fun sendData() {
        stopAutoCapture()
        FrameStore.frames = capturedFrames.toList()
        findNavController().navigate(CaptureFragmentDirections.actionCaptureToProgress(args.serverIp, capturedFrames.map { it.frameId }.toIntArray(), capturedFrames.size))
    }

    override fun onResume() {
        super.onResume()
        if (arSession == null) {
            if (ArCoreApk.getInstance().requestInstall(requireActivity(), !installRequested) == ArCoreApk.InstallStatus.INSTALL_REQUESTED) { installRequested = true; return }
            setupArSession()
        }
        try { arSession?.resume() } catch (e: Exception) { arSession = null }
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

        val vShader = "attribute vec4 a_Pos; attribute vec2 a_Tex; varying vec2 v_Tex; void main() { gl_Position = a_Pos; v_Tex = a_Tex; }"
        val fShader = "#extension GL_OES_EGL_image_external : require\nprecision mediump float; uniform samplerExternalOES s_Tex; varying vec2 v_Tex; void main() { gl_FragColor = texture2D(s_Tex, v_Tex); }"
        program = createProgram(vShader, fShader)
    }

    override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
        GLES20.glViewport(0, 0, width, height)
        arSession?.setDisplayGeometry(0, width, height)
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
        val session = arSession ?: return
        try {
            val frame = session.update()
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
                activity?.runOnUiThread { coverageOverlay.updateArCamera(arCamera) }
            }
        } catch (e: Exception) {
            Log.e("CaptureFragment", "Draw frame failed", e)
        }
    }

    private fun createProgram(v: String, f: String): Int {
        val vs = GLES20.glCreateShader(GLES20.GL_VERTEX_SHADER).apply { GLES20.glShaderSource(this, v); GLES20.glCompileShader(this) }
        val fs = GLES20.glCreateShader(GLES20.GL_FRAGMENT_SHADER).apply { GLES20.glShaderSource(this, f); GLES20.glCompileShader(this) }
        return GLES20.glCreateProgram().apply { GLES20.glAttachShader(this, vs); GLES20.glAttachShader(this, fs); GLES20.glLinkProgram(this) }
    }

    private fun floatBufferOf(data: FloatArray): FloatBuffer = ByteBuffer.allocateDirect(data.size * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().put(data).apply { position(0) }

    override fun onDestroyView() { super.onDestroyView(); cameraExecutor.shutdown(); _binding = null }
}
