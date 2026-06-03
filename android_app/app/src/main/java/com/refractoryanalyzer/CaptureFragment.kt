package com.refractoryanalyzer

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Bundle
import android.util.Log
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.*
import androidx.camera.core.Camera
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.navigation.fragment.findNavController
import androidx.navigation.fragment.navArgs
import com.refractoryanalyzer.databinding.FragmentCaptureBinding
import kotlinx.coroutines.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.math.sqrt
import kotlin.math.tan

class CaptureFragment : Fragment(), SensorEventListener {

    private var _binding: FragmentCaptureBinding? = null
    private val binding get() = _binding!!
    private val args: CaptureFragmentArgs by navArgs()

    private lateinit var sensorManager: SensorManager
    private lateinit var cameraExecutor: ExecutorService

    private var imageCapture: ImageCapture? = null
    private var camera: Camera? = null          // keep reference to lock focus/exposure
    private var isCapturing = false
    private var isTakingPhoto = false
    private var captureJob: Job? = null

    companion object {
        const val TARGET_FRAMES = 120    // was 60 — more overlap = better reconstruction
        const val INTERVAL_MS   = 150L   // was 300ms — denser capture
        const val GYRO_MAX      = 1.2f   // was 0.6f — furnace scanning requires rotation
    }

    // IMU Data
    private var lastAccel = FloatArray(3)
    private var lastGyro = FloatArray(3)
    private var lastOrient = FloatArray(3)
    private var gyroMag = 0f

    private val capturedFrames = mutableListOf<CapturedFrame>()

    data class CapturedFrame(
        val frameId: Int,
        val jpegBytes: ByteArray,
        val imu: JSONObject,
        val camera: JSONObject
    )

    private val requestPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        if (isGranted) {
            startCamera()
        } else {
            Toast.makeText(context, "Permiso de cámara requerido", Toast.LENGTH_SHORT).show()
            findNavController().popBackStack()
        }
    }

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentCaptureBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        
        sensorManager = requireContext().getSystemService(Context.SENSOR_SERVICE) as SensorManager
        cameraExecutor = Executors.newSingleThreadExecutor()

        checkPermissions()

        binding.btnCapture.setOnClickListener {
            if (args.isAutomatic) {
                toggleCapture()
            } else {
                takePhoto()
            }
        }
        binding.btnSend.setOnClickListener { sendData() }
        binding.btnBack.setOnClickListener { findNavController().popBackStack() }

        if (args.isAutomatic) {
            binding.tvStatus.text = "Rodeá el horno lentamente (Modo Auto)"
        } else {
            binding.tvStatus.text = "Presioná para capturar (Modo Manual)"
        }

        updateUI()
    }

    private fun checkPermissions() {
        if (ContextCompat.checkSelfPermission(requireContext(), Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            requestPermissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private fun startCamera() {
        val cameraProviderFuture = ProcessCameraProvider.getInstance(requireContext())
        cameraProviderFuture.addListener({
            val cameraProvider: ProcessCameraProvider = cameraProviderFuture.get()

            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(binding.viewFinder.surfaceProvider)
            }

            imageCapture = ImageCapture.Builder()
                .setTargetRotation(binding.viewFinder.display.rotation)
                .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                .build()

            try {
                cameraProvider.unbindAll()
                camera = cameraProvider.bindToLifecycle(
                    viewLifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, preview, imageCapture
                )
                // Lock focus to a fixed distance (~50cm — typical furnace interior)
                // and freeze exposure so features are stable across frames
                lockFocusAndExposure()
            } catch (e: Exception) {
                Log.e("CaptureFragment", "Use case binding failed", e)
            }
        }, ContextCompat.getMainExecutor(requireContext()))
    }

    private fun toggleCapture() {
        if (isCapturing) {
            stopAutoCapture()
        } else {
            startAutoCapture()
        }
    }

    private fun lockFocusAndExposure() {
        val cam = camera ?: return
        try {
            // Fix focus at center of frame — stable for furnace at ~50cm
            val factory = binding.viewFinder.meteringPointFactory
            val pt = factory.createPoint(0.5f, 0.5f)
            val action = FocusMeteringAction.Builder(pt)
                .disableAutoCancel()   // stay locked, don't re-trigger
                .build()
            cam.cameraControl.startFocusAndMetering(action)
        } catch (e: Exception) {
            Log.w("CaptureFragment", "Could not lock focus/exposure: ${e.message}")
        }
    }

    private fun startAutoCapture() {
        isCapturing = true
        binding.btnCapture.text = "■ DETENER"
        binding.tvStatus.text = "Rodeá el horno EN ESPIRAL — de arriba hacia abajo"

        captureJob = viewLifecycleOwner.lifecycleScope.launch {
            while (isCapturing && capturedFrames.size < TARGET_FRAMES) {
                if (gyroMag < GYRO_MAX) {
                    updateStabilityIndicator(true)
                    takePhoto()
                } else {
                    updateStabilityIndicator(false)
                }
                delay(INTERVAL_MS)
            }
            if (capturedFrames.size >= TARGET_FRAMES) {
                stopAutoCapture()
                binding.tvStatus.text = "¡${TARGET_FRAMES} fotos! Podés enviar ahora."
            }
        }
    }

    private fun stopAutoCapture() {
        isCapturing = false
        isTakingPhoto = false
        captureJob?.cancel()
        captureJob = null
        binding.btnCapture.text = "● INICIAR"
    }

    private fun takePhoto() {
        if (isTakingPhoto) return
        if (args.isAutomatic && !isCapturing) return
        val imageCapture = imageCapture ?: return

        isTakingPhoto = true
        imageCapture.takePicture(cameraExecutor, object : ImageCapture.OnImageCapturedCallback() {
            override fun onCaptureSuccess(image: ImageProxy) {
                isTakingPhoto = false
                if (args.isAutomatic && !isCapturing) {
                    image.close()
                    return
                }

                val buffer = image.planes[0].buffer
                val bytes = ByteArray(buffer.remaining())
                buffer.get(bytes)
                image.close()

                val frameId = capturedFrames.size
                val imuJson = buildImuJson()
                val cameraJson = buildCameraJson(image.width, image.height)

                capturedFrames.add(CapturedFrame(frameId, bytes, imuJson, cameraJson))
                
                activity?.runOnUiThread { updateUI() }
            }

            override fun onError(exception: ImageCaptureException) {
                isTakingPhoto = false
                Log.e("CaptureFragment", "Photo capture failed: ${exception.message}", exception)
            }
        })
    }

    private fun buildImuJson() = JSONObject().apply {
        put("accel", JSONArray(lastAccel.map { it }))
        put("gyro", JSONArray(lastGyro.map { it }))
        put("orient", JSONArray(lastOrient.map { it }))
    }

    private fun buildCameraJson(w: Int, h: Int) = JSONObject().apply {
        val focalPx = (w.coerceAtLeast(h) / 2.0) / tan(Math.toRadians(35.0))
        put("focal_px", focalPx)
        put("width", w)
        put("height", h)
    }

    private fun updateUI() {
        val count = capturedFrames.size
        binding.tvCounter.text = "$count / $TARGET_FRAMES fotos"
        binding.progressBar.progress = count
        binding.btnSend.isEnabled = count >= 30   // allow sending from 30 frames
        binding.btnSend.text = "ENVIAR $count →"

        if (!args.isAutomatic && !isCapturing) {
            binding.btnCapture.text = "● CAPTURAR"
        }
    }

    private fun updateStabilityIndicator(stable: Boolean) {
        binding.tvGyro.text = if (stable) "● Estable" else "⚠ Muy rápido"
        binding.tvGyro.setTextColor(ContextCompat.getColor(requireContext(), 
            if (stable) android.R.color.holo_green_light else android.R.color.holo_orange_light))
    }

    private fun sendData() {
        stopAutoCapture()
        FrameStore.frames = capturedFrames.toList()
        val action = CaptureFragmentDirections.actionCaptureToProgress(
            serverIp = args.serverIp,
            frameCount = capturedFrames.size,
            framesCache = capturedFrames.map { it.frameId }.toIntArray()
        )
        findNavController().navigate(action)
    }

    override fun onResume() {
        super.onResume()
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER), SensorManager.SENSOR_DELAY_UI)
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE), SensorManager.SENSOR_DELAY_UI)
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_ROTATION_VECTOR), SensorManager.SENSOR_DELAY_UI)
    }

    override fun onPause() {
        super.onPause()
        sensorManager.unregisterListener(this)
        stopAutoCapture()
    }

    override fun onSensorChanged(event: SensorEvent) {
        when (event.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> lastAccel = event.values.clone()
            Sensor.TYPE_GYROSCOPE -> {
                lastGyro = event.values.clone()
                gyroMag = sqrt(lastGyro[0] * lastGyro[0] + lastGyro[1] * lastGyro[1] + lastGyro[2] * lastGyro[2])
            }
            Sensor.TYPE_ROTATION_VECTOR -> {
                val rotationMatrix = FloatArray(9)
                SensorManager.getRotationMatrixFromVector(rotationMatrix, event.values)
                val orientation = FloatArray(3)
                SensorManager.getOrientation(rotationMatrix, orientation)
                // Convert to degrees
                lastOrient[0] = Math.toDegrees(orientation[0].toDouble()).toFloat() // yaw/azimuth
                lastOrient[1] = Math.toDegrees(orientation[1].toDouble()).toFloat() // pitch
                lastOrient[2] = Math.toDegrees(orientation[2].toDouble()).toFloat() // roll
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    override fun onDestroyView() {
        super.onDestroyView()
        cameraExecutor.shutdown()
        _binding = null
    }
}
