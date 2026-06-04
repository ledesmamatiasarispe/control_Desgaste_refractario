package com.refractoryanalyzer

object FrameStore {
    var frames: List<CaptureFragment.CapturedFrame> = emptyList()
    var currentJobId: String = ""
    var serverIp: String = ""
    val uploadedFrameIds: MutableSet<Int> = mutableSetOf()

    fun reset() {
        frames = emptyList()
        currentJobId = ""
        serverIp = ""
        uploadedFrameIds.clear()
    }
}
