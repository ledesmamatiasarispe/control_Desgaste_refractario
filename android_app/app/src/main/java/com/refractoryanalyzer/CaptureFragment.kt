package com.refractoryanalyzer

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.camera.core.*
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.navigation.fragment.findNavController
import androidx.navigation.fragment.navArgs
import com.refractoryanalyzer.databinding.FragmentCaptureBinding
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

class CaptureFragment : Fragment(), SensorEventListener {

    private var _binding: FragmentCaptureBinding? = null
    private val binding get() = _binding!!
    private val args: CaptureFragmentArgs by navArgs()

    private lateinit var sensorManager: SensorManager
    private var cameraExecutor: ExecutorService = Executors.newSingleThreadExecutor()

    // IMU state
    private val accel  = FloatArray(3)
    private val gyro   = FloatArray(3)
    private val orient = FloatArray(3)
    private var gyroMag = 0f

    // Capture state
    private val frames = mutableListOf<CapturedFrame>()
    private var imageCapture: ImageCapture? = null
    private var isAutoCapturing = false
    private var captureJob: Thread? = null

    data class CapturedFrame(
        val frameId: Int,
        val jpegBytes: ByteArray,
        val imu: JSONObject,
        val camera: JSONObject
    )

    companion object {
        const val TARGET_FRAMES = 60
        const val MIN_FRAMES    = 30
        const val GYRO_MAX      = 0.6f   // rad/s — discard if shaking
        const val INTERVAL_MS   = 300L
    }

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentCaptureBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        sensorManager = requireContext().getSystemService(Context.SENSOR_SERVICE) as SensorManager

        startCamera()
        updateCounter()

        binding.btnCapture.setOnClickListener { toggleCapture() }
        binding.btnSend.isEnabled = false
        binding.btnSend.setOnClickListener { goToProgress() }
        binding.btnBack.setOnClickListener { findNavController().popBackStack() }
    }

    override fun onResume() {
        super.onResume()
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER), SensorManager.SENSOR_DELAY_NORMAL)
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE),     SensorManager.SENSOR_DELAY_NORMAL)
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR), SensorManager.SENSOR_DELAY_NORMAL)
    }

    override fun onPause() {
        super.onPause()
        sensorManager.unregisterListener(this)
        stopCapture()
    }

    // ── Camera ──────────────────────────────────────────────────────────────

    private fun startCamera() {
        val providerFuture = ProcessCameraProvider.getInstance(requireContext())
        providerFuture.addListener({
            val provider = providerFuture.get()
            val preview = Preview.Builder().build()
                .also { it.surfaceProvider = binding.viewFinder.surfaceProvider }
            imageCapture = ImageCapture.Builder()
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                .build()
            provider.unbindAll()
            provider.bindToLifecycle(viewLifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture)
        }, ContextCompat.getMainExecutor(requireContext()))
    }

    // ── Capture loop ─────────────────────────────────────────────────────────

    private fun toggleCapture() {
        if (!isAutoCapturing) startCapture() else stopCapture()
    }

    private fun startCapture() {
        isAutoCapturing = true
        binding.btnCapture.text = "■ DETENER"
        captureJob = Thread {
            while (isAutoCapturing && frames.size < TARGET_FRAMES) {
                if (gyroMag > GYRO_MAX) {
                    updateGyroIndicator(false)
                    Thread.sleep(50); continue
                }
                updateGyroIndicator(true)
                grabFrame()
                Thread.sleep(INTERVAL_MS)
            }
            if (frames.size >= TARGET_FRAMES) {
                activity?.runOnUiThread {
                    stopCapture()
                    binding.tvStatus.text = "✓ ${TARGET_FRAMES} fotos — tocá ENVIAR"
                }
            }
        }.also { it.isDaemon = true; it.start() }
    }

    private fun stopCapture() {
        isAutoCapturing = false
        captureJob?.interrupt()
        activity?.runOnUiThread { binding.btnCapture.text = "● INICIAR" }
    }

    private fun grabFrame() {
        val ic = imageCapture ?: return
        ic.takePicture(cameraExecutor, object : ImageCapture.OnImageCapturedCallback() {
            override fun onCaptureSuccess(image: ImageProxy) {
                val bytes = imageToJpeg(image)
                image.close()
                if (bytes != null) {
                    val frame = CapturedFrame(
                        frameId   = frames.size,
                        jpegBytes = bytes,
                        imu       = buildImu(),
                        camera    = buildCameraInfo()
                    )
                    frames.add(frame)
                    activity?.runOnUiThread { updateCounter() }
                }
            }
            override fun onError(e: ImageCaptureException) { /* skip frame */ }
        })
    }

    private fun imageToJpeg(image: ImageProxy): ByteArray? = try {
        val buf = image.planes[0].buffer
        ByteArray(buf.remaining()).also { buf.get(it) }
    } catch (e: Exception) { null }

    // ── IMU ──────────────────────────────────────────────────────────────────

    override fun onSensorChanged(event: SensorEvent) {
        when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER   -> event.values.copyInto(accel)
            Sensor.TYPE_GYROSCOPE       -> {
                event.values.copyInto(gyro)
                gyroMag = Math.sqrt((gyro[0]*gyro[0] + gyro[1]*gyro[1] + gyro[2]*gyro[2]).toDouble()).toFloat()
            }
            Sensor.TYPE_ROTATION_VECTOR -> {
                SensorManager.getOrientation(FloatArray(9).also {
                    SensorManager.getRotationMatrixFromVector(it, event.values)
                }, orient)
                orient.forEachIndexed { i, v -> orient[i] = Math.toDegrees(v.toDouble()).toFloat() }
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    private fun buildImu() = JSONObject().apply {
        put("accel",  JSONArray(accel.map { it }))
        put("gyro",   JSONArray(gyro.map { it }))
        put("orient", JSONArray(orient.map { it }))
    }

    private fun buildCameraInfo() = JSONObject().apply {
        put("focal_px", 1440.0)
        put("width",    binding.viewFinder.width)
        put("height",   binding.viewFinder.height)
    }

    // ── UI helpers ────────────────────────────────────────────────────────────

    private fun updateCounter() {
        val n = frames.size
        binding.tvCounter.text = "$n / $TARGET_FRAMES fotos"
        binding.progressBar.max      = TARGET_FRAMES
        binding.progressBar.progress = n
        if (n >= MIN_FRAMES) {
            binding.btnSend.isEnabled = true
            binding.btnSend.text = "ENVIAR $n →"
        }
    }

    private fun updateGyroIndicator(stable: Boolean) {
        activity?.runOnUiThread {
            binding.tvGyro.text = if (stable) "● Estable" else "⚠ Muy rápido"
            binding.tvGyro.setTextColor(
                ContextCompat.getColor(requireContext(),
                    if (stable) android.R.color.holo_green_light
                    else android.R.color.holo_orange_light)
            )
        }
    }

    private fun goToProgress() {
        findNavController().navigate(
            CaptureFragmentDirections.actionCaptureToProgress(
                serverIp    = args.serverIp,
                frameCount  = frames.size,
                framesCache = frames.map { it.frameId }.toIntArray()
            )
        )
        // Pass frames through a shared ViewModel in production
        FrameStore.frames = frames
    }

    override fun onDestroyView() {
        super.onDestroyView()
        stopCapture()
        cameraExecutor.shutdown()
        _binding = null
    }
}

// Simple singleton to pass frames between fragments
object FrameStore {
    var frames: List<CaptureFragment.CapturedFrame> = emptyList()
}
