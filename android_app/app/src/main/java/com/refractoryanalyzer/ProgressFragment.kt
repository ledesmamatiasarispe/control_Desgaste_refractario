package com.refractoryanalyzer

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.navigation.fragment.navArgs
import com.refractoryanalyzer.databinding.FragmentProgressBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

class ProgressFragment : Fragment() {

    private var _binding: FragmentProgressBinding? = null
    private val binding get() = _binding!!
    private val args: ProgressFragmentArgs by navArgs()

    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private val baseUrl get() = "http://${args.serverIp}:5005"
    private var jobId: String? = null
    private var cancelled = false

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentProgressBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        binding.btnCancel.setOnClickListener { cancelled = true; parentFragmentManager.popBackStack() }
        binding.btnRetry.visibility = View.GONE
        binding.btnRetry.setOnClickListener { startUpload() }
        startUpload()
    }

    private fun startUpload() {
        binding.btnRetry.visibility = View.GONE
        cancelled = false
        lifecycleScope.launch { runUpload() }
    }

    private suspend fun runUpload() {
        val frames = FrameStore.frames
        val total  = frames.size

        // 1. Create job
        updateStatus("Creando trabajo…", 0, 0, total)
        val jid = withContext(Dispatchers.IO) { createJob() } ?: run {
            showError("No se pudo crear trabajo en el servidor"); return
        }
        jobId = jid

        // 2. Check already received
        val received = withContext(Dispatchers.IO) { getReceivedFrames(jid) }.toMutableSet()
        var uploaded = received.size

        // 3. Upload frames
        for (frame in frames) {
            if (cancelled) return
            if (frame.frameId in received) continue

            var ok = false
            repeat(3) { attempt ->
                if (!ok) {
                    ok = withContext(Dispatchers.IO) { uploadFrame(jid, frame) }
                    if (!ok && attempt < 2) Thread.sleep(1500)
                }
            }
            if (ok) {
                uploaded++
                val pct = (uploaded * 50 / total)
                updateStatus("Subiendo $uploaded/$total fotos…", pct, uploaded, total)
            }
        }

        // 4. Start reconstruction
        updateStatus("Iniciando reconstrucción…", 52, uploaded, total)
        withContext(Dispatchers.IO) { startReconstruct(jid, total) }

        // 5. Poll status
        pollStatus(jid)
    }

    private suspend fun pollStatus(jid: String) {
        while (!cancelled) {
            val status = withContext(Dispatchers.IO) { getJobStatus(jid) }
            when (status?.optString("status")) {
                "done"  -> { updateStatus("✓ Mesh listo — abrilo en el PC", 100, 0, 0); showDone(); return }
                "error" -> { showError(status.optString("error", "Error desconocido")); return }
                else    -> {
                    val pct  = 50 + (status?.optInt("progress", 0) ?: 0) / 2
                    val msg  = status?.optString("message", "Procesando…") ?: "Procesando…"
                    updateStatus(msg, pct, 0, 0)
                }
            }
            Thread.sleep(3000)
        }
    }

    // ── HTTP calls ────────────────────────────────────────────────────────────

    private fun createJob(): String? = try {
        val r = http.newCall(Request.Builder().url("$baseUrl/new_job").post("".toRequestBody()).build()).execute()
        JSONObject(r.body!!.string()).optString("job_id")
    } catch (e: Exception) { null }

    private fun getReceivedFrames(jid: String): Set<Int> = try {
        val r = http.newCall(Request.Builder().url("$baseUrl/received_frames/$jid").build()).execute()
        val arr = JSONObject(r.body!!.string()).getJSONArray("frames")
        (0 until arr.length()).map { arr.getInt(it) }.toSet()
    } catch (e: Exception) { emptySet() }

    private fun uploadFrame(jid: String, frame: CaptureFragment.CapturedFrame): Boolean = try {
        val meta = JSONObject().apply {
            put("frame_id",    frame.frameId)
            put("timestamp_ms", System.currentTimeMillis())
            put("imu",         frame.imu)
            put("camera",      frame.camera)
        }
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("image", "frame.jpg", frame.jpegBytes.toRequestBody("image/jpeg".toMediaType()))
            .addFormDataPart("meta", meta.toString())
            .build()
        val r = http.newCall(Request.Builder().url("$baseUrl/upload_frame/$jid/${frame.frameId}").post(body).build()).execute()
        r.isSuccessful
    } catch (e: Exception) { false }

    private fun startReconstruct(jid: String, total: Int): Boolean = try {
        val body = JSONObject().apply { put("total_frames", total) }.toString()
            .toRequestBody("application/json".toMediaType())
        http.newCall(Request.Builder().url("$baseUrl/start_reconstruct/$jid").post(body).build()).execute().isSuccessful
    } catch (e: Exception) { false }

    private fun getJobStatus(jid: String): JSONObject? = try {
        val r = http.newCall(Request.Builder().url("$baseUrl/status/$jid").build()).execute()
        JSONObject(r.body!!.string())
    } catch (e: Exception) { null }

    // ── UI ────────────────────────────────────────────────────────────────────

    private fun updateStatus(msg: String, pct: Int, done: Int, total: Int) {
        activity?.runOnUiThread {
            binding.tvStatus.text      = msg
            binding.progressBar.progress = pct
            if (total > 0) binding.tvUpload.text = "Subiendo: $done/$total"
        }
    }

    private fun showDone() = activity?.runOnUiThread {
        binding.tvResult.text       = "✓ Mesh generado en el PC"
        binding.tvResult.visibility = View.VISIBLE
        binding.btnCancel.text      = "← Volver"
    }

    private fun showError(msg: String) = activity?.runOnUiThread {
        binding.tvResult.text       = "Error: $msg"
        binding.tvResult.visibility = View.VISIBLE
        binding.btnRetry.visibility = View.VISIBLE
    }

    override fun onDestroyView() { super.onDestroyView(); _binding = null }
}
