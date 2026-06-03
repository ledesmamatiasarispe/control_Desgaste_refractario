package com.refractoryanalyzer

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import androidx.navigation.fragment.findNavController
import androidx.navigation.fragment.navArgs
import com.refractoryanalyzer.databinding.FragmentSelectionBinding

class SelectionFragment : Fragment() {

    private var _binding: FragmentSelectionBinding? = null
    private val binding get() = _binding!!
    private val args: SelectionFragmentArgs by navArgs()

    override fun onCreateView(
        inflater: LayoutInflater, container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentSelectionBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        binding.cardAuto.setOnClickListener {
            navigateToCapture(isAutomatic = true)
        }

        binding.cardManual.setOnClickListener {
            navigateToCapture(isAutomatic = false)
        }
    }

    private fun navigateToCapture(isAutomatic: Boolean) {
        val action = SelectionFragmentDirections.actionSelectionToCapture(
            serverIp = args.serverIp,
            isAutomatic = isAutomatic
        )
        findNavController().navigate(action)
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
