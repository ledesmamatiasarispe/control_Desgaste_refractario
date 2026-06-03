package com.refractoryanalyzer

import android.content.Context
import android.os.Bundle
import android.util.Log
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.navigation.fragment.findNavController
import androidx.navigation.fragment.navArgs
import com.refractoryanalyzer.databinding.FragmentProgressBinding
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class ProgressFragment : Fragment() {

    private var _binding: FragmentProgressBinding? = null
    private val binding get() = _binding!!
    private val args: ProgressFragmentArgs by navArgs()

    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private var jobId: String? = null
    private var isCancelled = false

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentProgressBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        binding.btnCancel.setOnClickListener {
            isCancelled = true
            findNavController().popBackStack()
        }
        
        binding.btnRetry.setOnClickListener {
            startProcess()
        }

        startProcess()
    }

    private fun startProcess() {
        isCancelled = false
        binding.btnRetry.visibility = View.GONE
        binding.tvResult.visibility = View.GONE
        binding.btnCancel.text = "Cancelar"
        binding.tvStatus.setTextColor(ContextCompat.getColor(requireContext(), android.R.color.tab_indicator_text))
        
        viewLifecycleOwner.lifecycleScope.launch {
            try {
                processWorkflow()
            } catch (e: Exception) {
                if (!isCancelled) {
                    showError("Error fatal: ${e.message}")
                }
            }
        }
    }

    private suspend fun processWorkflow() {
        val serverIp = args.serverIp
        val frames = FrameStore.frames
        if (frames.isEmpty()) {
            showError("No hay fotos para procesar")
            return
        }

        // 1. Obtener o crear Job ID
        updateStatus("Creando trabajo en el PC…", 5)
        jobId = createNewJob(serverIp) ?: throw IOException("No se pudo conectar con el servidor")
        val jid = jobId!!

        // 2. Resume logic
        updateStatus("Verificando progreso previo…", 10)
        val uploadedIds = getReceivedFrames(serverIp, jid)
        
        // Clear memory for already uploaded frames
        for (frame in frames) {
            if (frame.frameId in uploadedIds) {
                frame.jpegBytes = null
            }
        }

        // 3. Upload frames
        var uploadedCount = uploadedIds.size
        for (frame in frames) {
            if (isCancelled) return
            if (frame.frameId in uploadedIds) {
                // Ya estaba en el servidor
                continue
            }

            val progress = 10 + (uploadedCount * 45 / frames.size)
            updateStatus("Subiendo foto ${uploadedCount + 1} de ${frames.size}…", progress)
            binding.tvUpload.text = "Foto ID: ${frame.frameId}"
            
            var success = false
            repeat(3) { attempt ->
                if (!success && !isCancelled) {
                    success = uploadFrame(serverIp, jid, frame)
                    if (!success && attempt < 2) delay(1500)
                }
            }
            
            if (success) {
                uploadedCount++
            } else {
                showError("No se pudo subir la foto ${frame.frameId}. Reintentá.")
                return
            }
        }

        // 4. Iniciar reconstrucción
        updateStatus("Iniciando reconstrucción…", 60)
        binding.tvUpload.text = "Procesando en el PC"
        if (!startReconstruction(serverIp, jid, frames.size)) {
            showError("El servidor no pudo iniciar el proceso 3D")
            return
        }

        // 5. Polling de estado
        pollStatus(serverIp, jid)
    }

    private suspend fun pollStatus(ip: String, jid: String) {
        while (!isCancelled) {
            val statusJson = getStatus(ip, jid)
            if (statusJson == null) {
                delay(3000)
                continue
            }

            val status = statusJson.optString("status", "running")
            val progress = statusJson.optInt("progress", 0)
            val message = statusJson.optString("message", "Calculando nube de puntos…")

            when (status) {
                "done" -> {
                    updateStatus("✓ Reconstrucción finalizada", 100)
                    binding.tvUpload.text = "Proceso completo"
                    binding.tvResult.visibility = View.VISIBLE
                    binding.tvResult.text = "✓ Mesh listo en el PC"
                    binding.btnCancel.text = "← Volver"
                    FrameStore.frames = emptyList() // Clear all memory
                    return
                }
                "error" -> {
                    showError("Error en PC: ${statusJson.optString("error")}")
                    return
                }
                else -> {
                    // Mapear 0-100 de reconstrucción a 60-100 de la UI
                    val totalProgress = 60 + (progress * 40 / 100)
                    updateStatus(message, totalProgress)
                }
            }
            delay(3000)
        }
    }

    // --- Network Calls (IO Context) ---

    private suspend fun createNewJob(ip: String): String? = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url("http://$ip:5005/new_job")
            .post("".toRequestBody())
            .build()
        try {
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) JSONObject(response.body!!.string()).getString("job_id") else null
            }
        } catch (e: Exception) { null }
    }

    private suspend fun getReceivedFrames(ip: String, jid: String): Set<Int> = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("http://$ip:5005/received_frames/$jid").build()
        try {
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    val array = JSONObject(response.body!!.string()).getJSONArray("frames")
                    mutableSetOf<Int>().apply { for (i in 0 until array.length()) add(array.getInt(i)) }
                } else emptySet()
            }
        } catch (e: Exception) { emptySet() }
    }

    private suspend fun uploadFrame(ip: String, jid: String, frame: CaptureFragment.CapturedFrame): Boolean = withContext(Dispatchers.IO) {
        val bytes = frame.jpegBytes ?: return@withContext true // Already uploaded/cleared
        
        val meta = JSONObject().apply {
            put("frame_id", frame.frameId)
            put("timestamp_ms", System.currentTimeMillis())
            put("imu", frame.imu)
            put("camera", frame.camera)
        }
        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("image", "f.jpg", bytes.toRequestBody("image/jpeg".toMediaType()))
            .addFormDataPart("meta", meta.toString())
            .build()
        val request = Request.Builder().url("http://$ip:5005/upload_frame/$jid/${frame.frameId}").post(requestBody).build()
        try { 
            val success = client.newCall(request).execute().use { it.isSuccessful }
            if (success) {
                frame.jpegBytes = null // Free memory!
            }
            success
        } catch (e: Exception) { false }
    }

    private suspend fun startReconstruction(ip: String, jid: String, total: Int): Boolean = withContext(Dispatchers.IO) {
        val alignJson = requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE)
            .getString("align_pts_json", "[]")
        
        val json = JSONObject().apply {
            put("total_frames", total)
            put("align_pts", JSONArray(alignJson))
        }
        val request = Request.Builder()
            .url("http://$ip:5005/start_reconstruct/$jid")
            .post(json.toString().toRequestBody("application/json".toMediaType()))
            .build()
        try { client.newCall(request).execute().use { it.isSuccessful } } catch (e: Exception) { false }
    }

    private suspend fun getStatus(ip: String, jid: String): JSONObject? = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("http://$ip:5005/status/$jid").build()
        try { client.newCall(request).execute().use { response ->
            if (response.isSuccessful) JSONObject(response.body!!.string()) else null
        } } catch (e: Exception) { null }
    }

    // --- UI Update ---

    private fun updateStatus(message: String, progress: Int) {
        binding.tvStatus.text = message
        binding.progressBar.progress = progress
    }

    private fun showError(error: String) {
        binding.tvStatus.text = "Error de conexión o servidor"
        binding.tvStatus.setTextColor(ContextCompat.getColor(requireContext(), android.R.color.holo_red_light))
        binding.tvResult.visibility = View.VISIBLE
        binding.tvResult.text = error
        binding.btnRetry.visibility = View.VISIBLE
    }

    override fun onDestroyView() {
        super.onDestroyView()
        isCancelled = true
        _binding = null
    }
}
