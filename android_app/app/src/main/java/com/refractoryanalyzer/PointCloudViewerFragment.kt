package com.refractoryanalyzer

import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.opengl.Matrix
import android.os.Bundle
import android.view.*
import android.widget.Button
import android.widget.ProgressBar
import android.widget.TextView
import androidx.fragment.app.Fragment
import androidx.navigation.fragment.findNavController
import androidx.navigation.fragment.navArgs
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10
import kotlin.math.*

class PointCloudViewerFragment : Fragment() {

    private val args: PointCloudViewerFragmentArgs by navArgs()
    private lateinit var glView: GLSurfaceView
    private lateinit var renderer: PointCloudRenderer
    private lateinit var statusTxt: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var btnMorePhotos: Button
    private lateinit var btnGenerate: Button

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(120, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, s: Bundle?): View =
        inflater.inflate(R.layout.fragment_point_cloud_viewer, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        glView      = view.findViewById(R.id.glSurfaceView)
        statusTxt   = view.findViewById(R.id.tvStatus)
        progressBar = view.findViewById(R.id.progressBar)
        btnMorePhotos = view.findViewById(R.id.btnMorePhotos)
        btnGenerate   = view.findViewById(R.id.btnGenerate)

        renderer = PointCloudRenderer()
        glView.setEGLContextClientVersion(2)
        glView.setRenderer(renderer)
        glView.renderMode = GLSurfaceView.RENDERMODE_WHEN_DIRTY

        setupTouch()

        btnMorePhotos.setOnClickListener {
            // Go back to capture screen with the same server IP
            val action = PointCloudViewerFragmentDirections
                .actionPointCloudToCapture(args.serverIp)
            findNavController().navigate(action)
        }

        btnGenerate.isEnabled = false
        btnGenerate.setOnClickListener { startFullReconstruction() }

        pollUntilReady()
    }

    // ── polling loop ──────────────────────────────────────────────────────────

    private fun pollUntilReady() {
        statusTxt.text = "Esperando nube de puntos…"
        pollStatus()
    }

    private fun pollStatus() {
        val url = "http://${args.serverIp}:5005/status/${args.jobId}"
        client.newCall(Request.Builder().url(url).build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                scheduleRetry()
            }
            override fun onResponse(call: Call, response: Response) {
                val body = response.body?.string() ?: return scheduleRetry()
                val json = org.json.JSONObject(body)
                val status   = json.optString("status")
                val progress = json.optInt("progress", 0)
                val message  = json.optString("message")

                requireActivity().runOnUiThread {
                    progressBar.progress = progress
                    statusTxt.text = message
                }

                when (status) {
                    "preview_done" -> downloadPointCloud()
                    "error"        -> requireActivity().runOnUiThread {
                        statusTxt.text = "Error: ${json.optString("error")}"
                    }
                    else           -> scheduleRetry()
                }
            }
        })
    }

    private fun scheduleRetry() {
        glView.postDelayed({ pollStatus() }, 3000)
    }

    private fun downloadPointCloud() {
        requireActivity().runOnUiThread {
            statusTxt.text = "Descargando nube de puntos…"
        }
        val url = "http://${args.serverIp}:5005/pointcloud/${args.jobId}"
        client.newCall(Request.Builder().url(url).build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                requireActivity().runOnUiThread {
                    statusTxt.text = "Error descargando nube: ${e.message}"
                }
            }
            override fun onResponse(call: Call, response: Response) {
                val bytes = response.body?.bytes() ?: return
                val points = parsePly(bytes)
                requireActivity().runOnUiThread {
                    if (points.isEmpty()) {
                        statusTxt.text = "Nube vacía o sin correspondencias — tomá más fotos"
                    } else {
                        statusTxt.text = "${points.size / 6} puntos cargados"
                        btnGenerate.isEnabled = true
                        renderer.setPoints(points)
                        glView.requestRender()
                    }
                }
            }
        })
    }

    // ── full reconstruction ───────────────────────────────────────────────────

    private fun startFullReconstruction() {
        btnGenerate.isEnabled   = false
        btnMorePhotos.isEnabled = false
        statusTxt.text          = "Iniciando reconstrucción completa…"

        val url  = "http://${args.serverIp}:5005/continue_reconstruct/${args.jobId}"
        val body = "{}".toRequestBody("application/json".toMediaType())
        client.newCall(Request.Builder().url(url).post(body).build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                requireActivity().runOnUiThread {
                    statusTxt.text = "Error: ${e.message}"
                    btnGenerate.isEnabled = true
                }
            }
            override fun onResponse(call: Call, response: Response) {
                requireActivity().runOnUiThread {
                    val action = PointCloudViewerFragmentDirections
                        .actionPointCloudToProgress(args.serverIp, IntArray(0), existingJobId = args.jobId)
                    findNavController().navigate(action)
                }
            }
        })
    }

    // ── PLY parser ────────────────────────────────────────────────────────────

    private fun parsePly(data: ByteArray): FloatArray {
        val text      = String(data, Charsets.US_ASCII)
        val endMarker = "end_header\n"
        val headerEnd = text.indexOf(endMarker)
        if (headerEnd < 0) return FloatArray(0)

        val header      = text.substring(0, headerEnd)
        val vertexCount = Regex("element vertex (\\d+)").find(header)
            ?.groupValues?.get(1)?.toIntOrNull() ?: return FloatArray(0)

        val result = FloatArray(vertexCount * 6)
        var idx    = 0

        text.substring(headerEnd + endMarker.length)
            .lineSequence()
            .filter { it.isNotBlank() }
            .take(vertexCount)
            .forEach { line ->
                val parts = line.trim().split(" ")
                if (parts.size >= 6) {
                    result[idx++] = parts[0].toFloatOrNull() ?: 0f
                    result[idx++] = parts[1].toFloatOrNull() ?: 0f
                    result[idx++] = parts[2].toFloatOrNull() ?: 0f
                    result[idx++] = (parts[3].toIntOrNull() ?: 128) / 255f
                    result[idx++] = (parts[4].toIntOrNull() ?: 128) / 255f
                    result[idx++] = (parts[5].toIntOrNull() ?: 128) / 255f
                }
            }
        return result.copyOf(idx)
    }

    // ── touch rotation ────────────────────────────────────────────────────────

    private var lastX = 0f
    private var lastY = 0f
    private var scaleDetector: ScaleGestureDetector? = null

    private fun setupTouch() {
        scaleDetector = ScaleGestureDetector(requireContext(),
            object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
                override fun onScale(d: ScaleGestureDetector): Boolean {
                    renderer.zoom *= d.scaleFactor
                    renderer.zoom = renderer.zoom.coerceIn(0.1f, 20f)
                    glView.requestRender()
                    return true
                }
            })

        glView.setOnTouchListener { _, event ->
            scaleDetector?.onTouchEvent(event)
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> { lastX = event.x; lastY = event.y }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.x - lastX
                    val dy = event.y - lastY
                    renderer.rotY += dx * 0.5f
                    renderer.rotX += dy * 0.5f
                    lastX = event.x; lastY = event.y
                    glView.requestRender()
                }
            }
            true
        }
    }

    override fun onResume()  { super.onResume();  glView.onResume()  }
    override fun onPause()   { super.onPause();   glView.onPause()   }
}

// ── OpenGL ES 2.0 point cloud renderer ───────────────────────────────────────

class PointCloudRenderer : GLSurfaceView.Renderer {

    var rotX = 20f
    var rotY = 0f
    var zoom = 1f

    private var vertexBuf: FloatBuffer? = null
    private var colorBuf: FloatBuffer?  = null
    private var pointCount = 0
    private var program = 0

    private val projMatrix  = FloatArray(16)
    private val viewMatrix  = FloatArray(16)
    private val modelMatrix = FloatArray(16)
    private val mvpMatrix   = FloatArray(16)
    private val tmpMatrix   = FloatArray(16)

    private val VS = """
        uniform mat4 uMVP;
        attribute vec3 aPos;
        attribute vec3 aColor;
        varying vec4 vColor;
        void main() {
            gl_Position  = uMVP * vec4(aPos, 1.0);
            gl_PointSize = 4.0;
            vColor = vec4(aColor, 1.0);
        }
    """.trimIndent()

    private val FS = """
        precision mediump float;
        varying vec4 vColor;
        void main() { gl_FragColor = vColor; }
    """.trimIndent()

    fun setPoints(data: FloatArray) {
        // data layout: x,y,z,r,g,b per point
        pointCount = data.size / 6
        if (pointCount == 0) return

        val xyz = FloatArray(pointCount * 3)
        val rgb = FloatArray(pointCount * 3)

        // Compute centroid + scale for normalization
        var minX = Float.MAX_VALUE; var maxX = -Float.MAX_VALUE
        var minY = Float.MAX_VALUE; var maxY = -Float.MAX_VALUE
        var minZ = Float.MAX_VALUE; var maxZ = -Float.MAX_VALUE
        for (i in 0 until pointCount) {
            val x = data[i * 6]; val y = data[i * 6 + 1]; val z = data[i * 6 + 2]
            if (x < minX) minX = x; if (x > maxX) maxX = x
            if (y < minY) minY = y; if (y > maxY) maxY = y
            if (z < minZ) minZ = z; if (z > maxZ) maxZ = z
        }
        val cx = (minX + maxX) / 2; val cy = (minY + maxY) / 2; val cz = (minZ + maxZ) / 2
        val scale = 2f / maxOf(maxX - minX, maxY - minY, maxZ - minZ, 0.001f)

        for (i in 0 until pointCount) {
            xyz[i * 3]     = (data[i * 6]     - cx) * scale
            xyz[i * 3 + 1] = (data[i * 6 + 1] - cy) * scale
            xyz[i * 3 + 2] = (data[i * 6 + 2] - cz) * scale
            rgb[i * 3]     = data[i * 6 + 3]
            rgb[i * 3 + 1] = data[i * 6 + 4]
            rgb[i * 3 + 2] = data[i * 6 + 5]
        }

        // Si todos los colores son cero (pycolmap no los populó), usar gradiente por altura
        val allBlack = rgb.none { it > 0.01f }
        if (allBlack) {
            val minY = xyz.filterIndexed { i, _ -> i % 3 == 1 }.minOrNull() ?: 0f
            val maxY = xyz.filterIndexed { i, _ -> i % 3 == 1 }.maxOrNull() ?: 1f
            val rangeY = (maxY - minY).coerceAtLeast(0.001f)
            for (i in 0 until pointCount) {
                val t = (xyz[i * 3 + 1] - minY) / rangeY   // 0=bottom, 1=top
                rgb[i * 3]     = 0.2f + 0.6f * t            // R: azul→naranja
                rgb[i * 3 + 1] = 0.4f + 0.3f * t            // G
                rgb[i * 3 + 2] = 0.9f - 0.7f * t            // B
            }
        }

        vertexBuf = ByteBuffer.allocateDirect(xyz.size * 4)
            .order(ByteOrder.nativeOrder()).asFloatBuffer().apply { put(xyz); position(0) }
        colorBuf  = ByteBuffer.allocateDirect(rgb.size * 4)
            .order(ByteOrder.nativeOrder()).asFloatBuffer().apply { put(rgb); position(0) }
    }

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        GLES20.glClearColor(0.1f, 0.1f, 0.1f, 1f)
        program = buildProgram(VS, FS)
    }

    override fun onSurfaceChanged(gl: GL10?, w: Int, h: Int) {
        GLES20.glViewport(0, 0, w, h)
        val ratio = w.toFloat() / h
        Matrix.frustumM(projMatrix, 0, -ratio, ratio, -1f, 1f, 2f, 50f)
    }

    override fun onDrawFrame(gl: GL10?) {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
        val vb = vertexBuf ?: return
        val cb = colorBuf  ?: return

        Matrix.setLookAtM(viewMatrix, 0, 0f, 0f, 5f, 0f, 0f, 0f, 0f, 1f, 0f)
        Matrix.setIdentityM(modelMatrix, 0)
        Matrix.scaleM(modelMatrix, 0, zoom, zoom, zoom)
        Matrix.rotateM(modelMatrix, 0, rotX, 1f, 0f, 0f)
        Matrix.rotateM(modelMatrix, 0, rotY, 0f, 1f, 0f)
        Matrix.multiplyMM(tmpMatrix, 0, viewMatrix, 0, modelMatrix, 0)
        Matrix.multiplyMM(mvpMatrix, 0, projMatrix, 0, tmpMatrix, 0)

        GLES20.glUseProgram(program)
        val mvpLoc   = GLES20.glGetUniformLocation(program, "uMVP")
        val posLoc   = GLES20.glGetAttribLocation(program, "aPos")
        val colorLoc = GLES20.glGetAttribLocation(program, "aColor")

        GLES20.glUniformMatrix4fv(mvpLoc, 1, false, mvpMatrix, 0)

        GLES20.glEnableVertexAttribArray(posLoc)
        GLES20.glVertexAttribPointer(posLoc, 3, GLES20.GL_FLOAT, false, 0, vb)

        GLES20.glEnableVertexAttribArray(colorLoc)
        GLES20.glVertexAttribPointer(colorLoc, 3, GLES20.GL_FLOAT, false, 0, cb)

        GLES20.glDrawArrays(GLES20.GL_POINTS, 0, pointCount)

        GLES20.glDisableVertexAttribArray(posLoc)
        GLES20.glDisableVertexAttribArray(colorLoc)
    }

    private fun buildProgram(vs: String, fs: String): Int {
        fun compile(type: Int, src: String): Int {
            val s = GLES20.glCreateShader(type)
            GLES20.glShaderSource(s, src)
            GLES20.glCompileShader(s)
            return s
        }
        val p = GLES20.glCreateProgram()
        GLES20.glAttachShader(p, compile(GLES20.GL_VERTEX_SHADER, vs))
        GLES20.glAttachShader(p, compile(GLES20.GL_FRAGMENT_SHADER, fs))
        GLES20.glLinkProgram(p)
        return p
    }
}
