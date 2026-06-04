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
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.navigation.fragment.findNavController
import com.refractoryanalyzer.databinding.FragmentCalibrationBinding
import kotlin.time.Duration.Companion.milliseconds
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.sqrt

class CalibrationFragment : Fragment(), SensorEventListener {

    private var _binding: FragmentCalibrationBinding? = null
    private val binding get() = _binding!!

    private lateinit var sensorManager: SensorManager
    private var gyroStabilityCount = 0
    private var isCalibrated = false

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentCalibrationBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        sensorManager = requireContext().getSystemService(Context.SENSOR_SERVICE) as SensorManager

        binding.btnFinishCalib.setOnClickListener {
            findNavController().popBackStack()
        }

        startCalibrationSim()
    }

    private fun startCalibrationSim() {
        viewLifecycleOwner.lifecycleScope.launch {
            binding.tvStatusCalib.text = getString(R.string.detectando_movimiento)
            delay(2000.milliseconds)
            binding.tvStatusCalib.text = getString(R.string.calibrando_acelerometro)
            delay(2000.milliseconds)
            binding.tvStatusCalib.text = getString(R.string.sincronizando_arcore)
            delay(2000.milliseconds)
            
            isCalibrated = true
            binding.tvStatusCalib.text = getString(R.string.sensores_optimizados)
            binding.progressCalibration.visibility = View.GONE
            binding.btnFinishCalib.isEnabled = true
            binding.ivInstruction.setImageResource(android.R.drawable.checkbox_on_background)
        }
    }

    override fun onResume() {
        super.onResume()
        sensorManager.registerListener(this, sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE), SensorManager.SENSOR_DELAY_UI)
    }

    override fun onPause() {
        super.onPause()
        sensorManager.unregisterListener(this)
    }

    override fun onSensorChanged(event: SensorEvent) {
        if (event.sensor.type == Sensor.TYPE_GYROSCOPE) {
            val v = event.values
            val mag = sqrt((v[0] * v[0]) + (v[1] * v[1]) + (v[2] * v[2]))
            if (mag > 0.5f) {
                gyroStabilityCount++
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
