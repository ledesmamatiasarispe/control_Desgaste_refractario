package com.refractoryanalyzer

import android.content.Context
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.core.content.edit
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import androidx.navigation.fragment.findNavController
import com.google.android.material.snackbar.Snackbar
import com.refractoryanalyzer.databinding.FragmentConnectBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

class ConnectFragment : Fragment() {

    private var _binding: FragmentConnectBinding? = null
    private val binding get() = _binding!!

    private val http = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .build()

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        _binding = FragmentConnectBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        // Restore last IP
        val prefs = requireContext().getSharedPreferences("refractory", Context.MODE_PRIVATE)
        binding.editIp.setText(prefs.getString("server_ip", ""))

        binding.btnConnect.setOnClickListener { connect() }
    }

    private fun connect() {
        val ip = binding.editIp.text.toString().trim()
        if (ip.isEmpty()) { showError("Ingresá la IP del PC"); return }

        binding.btnConnect.isEnabled = false
        binding.progressBar.visibility = View.VISIBLE
        binding.tvStatus.text = "Conectando…"

        lifecycleScope.launch {
            val ok = withContext(Dispatchers.IO) { ping(ip) }
            binding.btnConnect.isEnabled = true
            binding.progressBar.visibility = View.GONE
            if (ok) {
                requireContext().getSharedPreferences("refractory", Context.MODE_PRIVATE)
                    .edit { putString("server_ip", ip) }
                binding.tvStatus.text = "✓ Conectado"
                findNavController().navigate(
                    ConnectFragmentDirections.actionConnectToCapture(ip)
                )
            } else {
                binding.tvStatus.text = "No se encontró servidor en $ip:5005"
                showError("No se pudo conectar")
            }
        }
    }

    private fun ping(ip: String): Boolean = try {
        val req = Request.Builder().url("http://$ip:5005/ping").build()
        http.newCall(req).execute().use { it.isSuccessful }
    } catch (e: Exception) { false }

    private fun showError(msg: String) =
        Snackbar.make(binding.root, msg, Snackbar.LENGTH_SHORT).show()

    override fun onDestroyView() { super.onDestroyView(); _binding = null }
}
