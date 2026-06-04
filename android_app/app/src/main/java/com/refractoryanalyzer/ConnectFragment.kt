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

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentConnectBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        // Cargar última IP guardada
        val prefs = requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE)
        val lastIp = prefs.getString("server_ip", "")
        binding.editIp.setText(lastIp)

        binding.btnConnect.setOnClickListener {
            val ip = binding.editIp.text.toString().trim()
            if (ip.isEmpty()) {
                showError(getString(R.string.ingresar_ip))
            } else {
                testConnection(ip)
            }
        }
    }

    private fun testConnection(ip: String) {
        binding.btnConnect.isEnabled = false
        binding.progressBar.visibility = View.VISIBLE
        binding.tvStatus.text = getString(R.string.conectando)

        lifecycleScope.launch {
            val isOk = withContext(Dispatchers.IO) {
                try {
                    val url = "http://$ip:5005/ping"
                    val request = Request.Builder().url(url).build()
                    http.newCall(request).execute().use { response ->
                        response.isSuccessful
                    }
                } catch (ignored: Exception) {
                    false
                }
            }

            binding.btnConnect.isEnabled = true
            binding.progressBar.visibility = View.GONE

            if (isOk) {
                binding.tvStatus.text = getString(R.string.conectado)
                // Guardar IP exitosa
                requireContext().getSharedPreferences("refractory_prefs", Context.MODE_PRIVATE).edit {
                    putString("server_ip", ip)
                }
                
                // Nueva sesión — limpiar estado del scan anterior
                FrameStore.reset()

                val action = ConnectFragmentDirections.actionConnectToSelection(ip)
                findNavController().navigate(action)
            } else {
                binding.tvStatus.text = getString(R.string.error_conexion, ip)
                showError(getString(R.string.error_servidor))
            }
        }
    }

    private fun showError(message: String) {
        Snackbar.make(binding.root, message, Snackbar.LENGTH_LONG).show()
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
