package com.refractoryanalyzer

import android.content.Context
import android.graphics.*
import android.util.AttributeSet
import android.view.View
import com.google.ar.core.TrackingState
import kotlin.math.*

/**
 * AR overlay drawn on top of the camera preview.
 */
class CoverageOverlayView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : View(context, attrs) {

    // ── coverage grid (ANGULAR) ───────────────────────────────────────────────
    private val YAW_SECTORS   = 36
    private val PITCH_SECTORS = 15 // Sincronizado con los niveles del cilindro
    private val coverage = Array(YAW_SECTORS) { BooleanArray(PITCH_SECTORS) }

    private var currentYaw   = 0f
    private var currentPitch = 0f
    private var currentRoll  = 0f
    private var rotationMatrix = FloatArray(9).apply { this[0]=1f; this[4]=1f; this[8]=1f }
    
    private var arCamera: com.google.ar.core.Camera? = null

    // ── 3D World Points & Cylinder ──────────────────────────────────────────
    data class WorldPoint(val x: Float, val y: Float, val z: Float)
    private val cylinderPoints = mutableListOf<WorldPoint>()
    private var alignPoints = mutableListOf<CaptureFragment.AlignPoint>()

    private var hasCylinder = false
    private var cylCenterWorldX = 0f
    private var cylCenterWorldZ = 0f
    private var cylRadius = 0f

    // ── paints ────────────────────────────────────────────────────────────────
    private val paintCovered = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(200, 80, 220, 100); style = Paint.Style.FILL }
    private val paintEmpty = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(100, 180, 180, 180); style = Paint.Style.FILL }
    private val paintCylCovered = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(140, 80, 220, 100); style = Paint.Style.FILL }
    private val paintCylEmpty = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(60, 255, 230, 100); style = Paint.Style.FILL_AND_STROKE; strokeWidth = 2f }
    private val paintCurrent = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(230, 255, 220, 0); style = Paint.Style.STROKE; strokeWidth = 4f }
    private val paintArrow = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(220, 255, 100, 60); style = Paint.Style.FILL }
    private val paintText = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 42f; typeface = Typeface.DEFAULT_BOLD; textAlign = Paint.Align.CENTER; setShadowLayer(4f, 2f, 2f, Color.BLACK) }
    private val paintTextSmall = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(220, 220, 220, 220); textSize = 30f; typeface = Typeface.DEFAULT_BOLD; textAlign = Paint.Align.CENTER; setShadowLayer(3f, 1f, 1f, Color.BLACK) }
    private val paintBg = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(120, 0, 0, 0); style = Paint.Style.FILL }
    private val paintCylPoint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(200, 255, 200, 0); style = Paint.Style.FILL }

    private val alignColors = intArrayOf(Color.argb(220, 220, 60, 60), Color.argb(220, 60, 200, 80), Color.argb(220, 60, 120, 220))

    // ── public API ────────────────────────────────────────────────────────────

    fun updateOrientation(yaw: Float, pitch: Float, roll: Float) {
        currentYaw = yaw; currentPitch = pitch; currentRoll = roll; invalidate()
    }

    fun updateRotationMatrix(matrix: FloatArray) {
        rotationMatrix = matrix.clone()
        invalidate()
    }

    fun updateArCamera(camera: com.google.ar.core.Camera) {
        arCamera = camera
        invalidate()
    }

    fun markCaptured(yaw: Float, pitch: Float) {
        val camera = arCamera
        if (hasCylinder && camera != null && camera.trackingState == TrackingState.TRACKING) {
            val baseY = cylinderPoints[0].y
            val heightStep = 0.4f
            
            // Proyectamos cada celda del cilindro a la pantalla para ver si es visible
            for (i in 0 until YAW_SECTORS) {
                val angle = Math.toRadians(i * 360.0 / YAW_SECTORS)
                val worldX = cylCenterWorldX + cylRadius * cos(angle).toFloat()
                val worldZ = cylCenterWorldZ + cylRadius * sin(angle).toFloat()
                
                for (j in 0 until PITCH_SECTORS) {
                    val worldY = baseY + (j - PITCH_SECTORS / 2f) * heightStep
                    
                    // Verificamos si este punto del cilindro cae dentro de la pantalla
                    val screenPt = projectWorldPoint(worldX, worldY, worldZ)
                    if (screenPt != null && screenPt.x >= 0 && screenPt.x <= width && screenPt.y >= 0 && screenPt.y <= height) {
                        // Opcional: Podríamos verificar si el punto está de frente a la cámara
                        coverage[i][j] = true
                    }
                }
            }
        } else {
            // Fallback si no hay cilindro definido aún
            coverage[yawToSector(yaw)][pitchToSector(pitch)] = true
        }
        invalidate()
    }

    fun reset() {
        for (row in coverage) row.fill(false)
        cylinderPoints.clear(); alignPoints.clear(); hasCylinder = false; invalidate()
    }

    fun coveragePercent(): Int {
        val total = YAW_SECTORS * PITCH_SECTORS
        val covered = coverage.sumOf { row -> row.count { it } }
        return (covered * 100 / total)
    }

    fun resetCylinderPoints() {
        cylinderPoints.clear()
        hasCylinder = false
    }

    fun addCylinderWorldPoint(x: Float, y: Float, z: Float) {
        if (cylinderPoints.size >= 3) {
            cylinderPoints.clear()
            hasCylinder = false
        }
        cylinderPoints.add(WorldPoint(x, y, z))
        if (cylinderPoints.size == 3) calculateCircle()
        invalidate()
    }

    private fun calculateCircle() {
        if (cylinderPoints.size < 3) return
        val x1 = cylinderPoints[0].x; val z1 = cylinderPoints[0].z
        val x2 = cylinderPoints[1].x; val z2 = cylinderPoints[1].z
        val x3 = cylinderPoints[2].x; val z3 = cylinderPoints[2].z

        val d = 2 * (x1 * (z2 - z3) + x2 * (z3 - z1) + x3 * (z1 - z2))
        if (abs(d) < 0.001f) return

        cylCenterWorldX = ((x1 * x1 + z1 * z1) * (z2 - z3) + (x2 * x2 + z2 * z2) * (z3 - z1) + (x3 * x3 + z3 * z3) * (z1 - z2)) / d
        cylCenterWorldZ = ((x1 * x1 + z1 * z1) * (x3 - x2) + (x2 * x2 + z2 * z2) * (x1 - x3) + (x3 * x3 + z3 * z3) * (x2 - x1)) / d
        cylRadius = sqrt((x1 - cylCenterWorldX).pow(2) + (z1 - cylCenterWorldZ).pow(2))
        hasCylinder = true
    }

    fun addAlignPoint(pt: CaptureFragment.AlignPoint) {
        alignPoints.removeAll { it.index == pt.index }; alignPoints.add(pt); invalidate()
    }

    fun setAlignPoints(pts: List<CaptureFragment.AlignPoint>) {
        alignPoints = pts.toMutableList(); invalidate()
    }

    // ── projection ────────────────────────────────────────────────────────────

    private fun projectDirection(yaw: Float, pitch: Float): PointF? {
        // Fallback or grid projection based on angles
        var dy = yaw - currentYaw
        while (dy > 180) dy -= 360; while (dy < -180) dy += 360
        val dp = pitch - currentPitch
        val pxPerDeg = width / 70f
        val rx = dy * pxPerDeg
        val ry = dp * pxPerDeg
        val rRad = Math.toRadians(-currentRoll.toDouble()).toFloat()
        val c = cos(rRad); val s = sin(rRad)
        val rotatedX = rx * c - ry * s
        val rotatedY = rx * s + ry * c
        return PointF(width / 2f + rotatedX, height / 2f + rotatedY)
    }

    private fun projectWorldPoint(x: Float, y: Float, z: Float): PointF? {
        val camera = arCamera
        // Si el tracking es bueno, usamos ARCore
        if (camera != null && camera.trackingState == TrackingState.TRACKING) {
            val projmtx = FloatArray(16)
            camera.getProjectionMatrix(projmtx, 0, 0.005f, 100.0f)
            val viewmtx = FloatArray(16)
            camera.getViewMatrix(viewmtx, 0)
            val vpMtx = FloatArray(16)
            android.opengl.Matrix.multiplyMM(vpMtx, 0, projmtx, 0, viewmtx, 0)
            val vertex = floatArrayOf(x, y, z, 1f)
            val screenPos = floatArrayOf(0f, 0f, 0f, 0f)
            android.opengl.Matrix.multiplyMV(screenPos, 0, vpMtx, 0, vertex, 0)
            if (screenPos[3] > 0) {
                val sx = (screenPos[0] / screenPos[3] + 1f) * 0.5f * width
                val sy = (1f - screenPos[1] / screenPos[3]) * 0.5f * height
                return PointF(sx, sy)
            }
        }
        
        // Si el tracking falla, usamos una proyección de respaldo basada en Sensores (IMU)
        // Esto evita que los puntos "salten" al techo
        return projectDirectionBasedOnSensors(x, y, z)
    }

    private fun projectDirectionBasedOnSensors(x: Float, y: Float, z: Float): PointF? {
        // Cálculo matemático para mantener el cilindro estable usando solo el Giroscopio
        val dx = x - cylCenterWorldX
        val dz = z - cylCenterWorldZ
        val targetYaw = Math.toDegrees(atan2(dz.toDouble(), dx.toDouble())).toFloat()
        
        var dy = targetYaw - currentYaw
        while (dy > 180) dy -= 360; while (dy < -180) dy += 360
        
        // Aproximación visual para que no desaparezca el cilindro
        val pxPerDeg = width / 70f
        val rx = dy * pxPerDeg
        val ry = (0f - currentPitch) * pxPerDeg // Asumimos nivel de ojos si no hay tracking
        
        return PointF(width / 2f + rx, height / 2f + ry)
    }

    // ── drawing ───────────────────────────────────────────────────────────────

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (width == 0 || height == 0) return
        drawGuideHint(canvas); drawCoveragePercent(canvas)
        drawCylinder(canvas); drawAlignPoints(canvas); drawCrosshair(canvas)
    }

    private fun drawCylinder(canvas: Canvas) {
        if (!hasCylinder) {
            for (pt in cylinderPoints) {
                projectWorldPoint(pt.x, pt.y, pt.z)?.let { canvas.drawCircle(it.x, it.y, 20f, paintCylPoint) }
            }
            return
        }

        val baseY = cylinderPoints[0].y
        val sectors = YAW_SECTORS
        val levels = PITCH_SECTORS // Usar la misma variable para consistencia
        val heightStep = 0.4f 

        for (i in 0 until sectors) {
            val angle1 = Math.toRadians(i * 360.0 / sectors)
            val angle2 = Math.toRadians((i + 1) * 360.0 / sectors)
            
            val cos1 = cos(angle1).toFloat(); val sin1 = sin(angle1).toFloat()
            val cos2 = cos(angle2).toFloat(); val sin2 = sin(angle2).toFloat()

            for (j in 0 until levels) {
                val yBottom = baseY + (j - levels / 2f) * heightStep
                val yTop = yBottom + heightStep
                
                val p1 = projectWorldPoint(cylCenterWorldX + cylRadius * cos1, yBottom, cylCenterWorldZ + cylRadius * sin1)
                val p2 = projectWorldPoint(cylCenterWorldX + cylRadius * cos2, yBottom, cylCenterWorldZ + cylRadius * sin2)
                val p3 = projectWorldPoint(cylCenterWorldX + cylRadius * cos2, yTop, cylCenterWorldZ + cylRadius * sin2)
                val p4 = projectWorldPoint(cylCenterWorldX + cylRadius * cos1, yTop, cylCenterWorldZ + cylRadius * sin1)

                if (p1 != null && p2 != null && p3 != null && p4 != null) {
                    val path = Path()
                    path.moveTo(p1.x, p1.y)
                    path.lineTo(p2.x, p2.y)
                    path.lineTo(p3.x, p3.y)
                    path.lineTo(p4.x, p4.y)
                    path.close()
                    
                    val paint = if (coverage[i][j]) paintCylCovered else paintCylEmpty
                    canvas.drawPath(path, paint)
                }
            }
        }
    }

    private fun drawAlignPoints(canvas: Canvas) {
        val pTxt = Paint(paintText).apply { textSize = 38f }
        for (pt in alignPoints) {
            projectWorldPoint(pt.worldX, pt.worldY, pt.worldZ)?.let { screen ->
                val p = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = alignColors.getOrElse(pt.index) { Color.GRAY } }
                canvas.drawCircle(screen.x, screen.y, 40f, p)
                canvas.drawText((pt.index + 1).toString(), screen.x, screen.y + 14f, pTxt)
            }
        }
    }

    private fun drawCrosshair(canvas: Canvas) {
        val cx = width / 2f; val cy = height / 2f; val r = 32f
        val isNear = alignPoints.any {
            projectWorldPoint(it.worldX, it.worldY, it.worldZ)?.let { screen -> 
                sqrt((screen.x - cx).pow(2) + (screen.y - cy).pow(2)) < 60f 
            } ?: false
        }
        val ringColor = if (isNear) Color.argb(220, 80, 220, 100) else Color.argb(160, 255, 255, 255)
        canvas.drawCircle(cx, cy, r, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = ringColor; style = Paint.Style.STROKE; strokeWidth = 3f })
        val pLine = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.argb(180, 255, 255, 255); strokeWidth = 2f }
        canvas.drawLine(cx - r*1.6f, cy, cx + r*1.6f, cy, pLine); canvas.drawLine(cx, cy - r*1.6f, cx, cy + r*1.6f, pLine)
        canvas.drawCircle(cx, cy, 4f, Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE })
    }

    private fun drawCoverageRing(canvas: Canvas) {
        val cx = width * 0.5f; val cy = height - width * 0.18f; val rO = width * 0.14f; val rI = width * 0.09f
        canvas.drawCircle(cx, cy, rO + 8f, paintBg)
        val sweep = 360f / YAW_SECTORS
        for (i in 0 until YAW_SECTORS) {
            val start = i * sweep - 90f; val paint = if (coverage[i].any { it }) paintCovered else paintEmpty
            val rectO = RectF(cx - rO, cy - rO, cx + rO, cy + rO); val rectI = RectF(cx - rI, cy - rI, cx + rI, cy + rI)
            val path = Path(); path.arcTo(rectO, start + 1f, sweep - 2f, false); path.arcTo(rectI, start + sweep - 1f, -(sweep - 2f), false)
            path.close(); canvas.drawPath(path, paint)
        }
        val angle = Math.toRadians((currentYaw - 90.0)).toFloat()
        canvas.drawLine(cx + rI * cos(angle), cy + rI * sin(angle), cx + (rO + 6f) * cos(angle), cy + (rO + 6f) * sin(angle), paintCurrent)
        val bestAngle = Math.toRadians((findBestSector() * sweep - 90.0)).toFloat()
        val ax = cx + (rO + 22f) * cos(bestAngle); val ay = cy + (rO + 22f) * sin(bestAngle); val sz = 18f
        val path = Path(); path.moveTo(ax, ay); path.lineTo(ax + sz * cos(bestAngle + 2.44f), ay + sz * sin(bestAngle + 2.44f)); path.lineTo(ax + sz * cos(bestAngle - 2.44f), ay + sz * sin(bestAngle - 2.44f)); path.close(); canvas.drawPath(path, paintArrow)
    }

    private fun drawHeightBar(canvas: Canvas) {
        val barW = width * 0.05f; val barH = height * 0.35f; val left = width * 0.04f; val top = height * 0.33f
        canvas.drawRoundRect(left - 4f, top - 4f, left + barW + 4f, top + barH + 4f, 8f, 8f, paintBg)
        val secH = barH / PITCH_SECTORS
        for (i in 0 until PITCH_SECTORS) {
            val p = if (coverage.any { it[i] }) paintCovered else paintEmpty
            canvas.drawRoundRect(left, top + i * secH + 2f, left + barW, top + (i+1) * secH - 2f, 4f, 4f, p)
        }
        val py = top + ((currentPitch + 60f) / 120f).coerceIn(0f, 1f) * barH
        canvas.drawLine(left - 6f, py, left + barW + 6f, py, paintCurrent)
    }

    private fun drawGuideHint(canvas: Canvas) {
        val hint = buildHint(); val cx = width * 0.5f; val ty = height * 0.10f
        val tw = paintText.measureText(hint) + 24f
        canvas.drawRoundRect(cx - tw / 2f, ty - 40f, cx + tw / 2f, ty + 10f, 12f, 12f, paintBg)
        canvas.drawText(hint, cx, ty, paintText)
    }

    private fun drawCoveragePercent(canvas: Canvas) {
        val label = "${coveragePercent()}%"; val cx = width * 0.86f; val ty = height * 0.08f
        canvas.drawCircle(cx, ty, width * 0.07f, paintBg); canvas.drawText(label, cx, ty + paintTextSmall.textSize / 3f, paintTextSmall)
    }

    private fun buildHint(): String {
        if (coveragePercent() >= 80) return "✓ Cobertura excelente"
        val pSec = pitchToSector(currentPitch); val ySec = yawToSector(currentYaw)
        val covered = IntArray(PITCH_SECTORS) { p -> coverage.count { it[p] } }
        val minP = covered.indexOf(covered.min())
        return when {
            covered[pSec] < YAW_SECTORS / 3 -> "Seguí girando en este nivel"
            minP < pSec -> "↑ Sube la cámara"; minP > pSec -> "↓ Baja la cámara"
            coverage[ySec].any { it } -> "→ Avanzá un poco más"; else -> "📸 Capturando…"
        }
    }

    private fun findBestSector(): Int {
        var best = 0; var min = Int.MAX_VALUE
        for (i in 0 until YAW_SECTORS) {
            val c = coverage[i].count { it }
            if (c < min) { min = c; best = i }
        }
        return best
    }

    private fun yawToSector(yaw: Float): Int = ((yaw % 360f + 360f) % 360f * YAW_SECTORS / 360f).toInt().coerceIn(0, YAW_SECTORS - 1)
    private fun pitchToSector(pitch: Float): Int = ((pitch + 60f) / 120f * PITCH_SECTORS).toInt().coerceIn(0, PITCH_SECTORS - 1)
}
