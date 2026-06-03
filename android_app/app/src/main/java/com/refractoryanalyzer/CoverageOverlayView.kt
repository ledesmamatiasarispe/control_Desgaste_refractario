package com.refractoryanalyzer

import android.content.Context
import android.graphics.*
import android.util.AttributeSet
import android.view.View
import kotlin.math.*

/**
 * AR overlay drawn on top of the camera preview.
 *
 * Shows:
 *   - Coverage ring (bottom centre): which yaw sectors (0-360°) have photos
 *   - Height bar  (left edge):       which pitch levels have photos
 *   - Guide arrow:                   points toward the most-needed direction
 *   - Text hint:                     "Sube", "Baja", "Rota", etc.
 */
class CoverageOverlayView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null
) : View(context, attrs) {

    // ── coverage grid ─────────────────────────────────────────────────────────
    // 36 yaw sectors (10° each) × 5 pitch sectors (-60° to +60°, 24° each)
    private val YAW_SECTORS   = 36
    private val PITCH_SECTORS = 5
    private val coverage = Array(YAW_SECTORS) { BooleanArray(PITCH_SECTORS) }

    private var currentYaw   = 0f
    private var currentPitch = 0f

    // ── paints ────────────────────────────────────────────────────────────────
    private val paintCovered = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(200, 80, 220, 100)
        style = Paint.Style.FILL
    }
    private val paintEmpty = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(100, 180, 180, 180)
        style = Paint.Style.FILL
    }
    private val paintCurrent = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(230, 255, 220, 0)
        style = Paint.Style.STROKE
        strokeWidth = 4f
    }
    private val paintArrow = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(220, 255, 100, 60)
        style = Paint.Style.FILL
    }
    private val paintText = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = 42f
        typeface = Typeface.DEFAULT_BOLD
        textAlign = Paint.Align.CENTER
        setShadowLayer(4f, 2f, 2f, Color.BLACK)
    }
    private val paintTextSmall = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(220, 220, 220, 220)
        textSize = 30f
        typeface = Typeface.DEFAULT_BOLD
        textAlign = Paint.Align.CENTER
        setShadowLayer(3f, 1f, 1f, Color.BLACK)
    }
    private val paintBg = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(120, 0, 0, 0)
        style = Paint.Style.FILL
    }

    // ── public API ────────────────────────────────────────────────────────────

    /** Call on every sensor update (does NOT mark as captured). */
    fun updateOrientation(yaw: Float, pitch: Float) {
        currentYaw   = yaw
        currentPitch = pitch
        invalidate()
    }

    /** Call when a photo is captured at the current orientation. */
    fun markCaptured(yaw: Float, pitch: Float) {
        coverage[yawToSector(yaw)][pitchToSector(pitch)] = true
        invalidate()
    }

    /** Reset all coverage data. */
    fun reset() {
        for (row in coverage) row.fill(false)
        invalidate()
    }

    fun coveragePercent(): Int {
        val total   = YAW_SECTORS * PITCH_SECTORS
        val covered = coverage.sumOf { row -> row.count { it } }
        return (covered * 100 / total)
    }

    // ── drawing ───────────────────────────────────────────────────────────────

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (width == 0 || height == 0) return

        drawCoverageRing(canvas)
        drawHeightBar(canvas)
        drawGuideHint(canvas)
        drawCoveragePercent(canvas)
    }

    /** Donut/arc ring at the bottom showing 360° yaw coverage. */
    private fun drawCoverageRing(canvas: Canvas) {
        val cx     = width * 0.5f
        val cy     = height - width * 0.18f   // near bottom
        val rOuter = width * 0.14f
        val rInner = width * 0.09f

        // Background circle
        canvas.drawCircle(cx, cy, rOuter + 8f, paintBg)

        // Draw each yaw sector
        val sweepDeg = 360f / YAW_SECTORS
        for (i in 0 until YAW_SECTORS) {
            val startAngle = i * sweepDeg - 90f   // start at top
            val covered = coverage[i].any { it }
            val paint = if (covered) paintCovered else paintEmpty

            val path = Path()
            val rectOuter = RectF(cx - rOuter, cy - rOuter, cx + rOuter, cy + rOuter)
            val rectInner = RectF(cx - rInner, cy - rInner, cx + rInner, cy + rInner)
            path.arcTo(rectOuter, startAngle + 1f, sweepDeg - 2f, false)
            path.arcTo(rectInner, startAngle + sweepDeg - 1f, -(sweepDeg - 2f), false)
            path.close()
            canvas.drawPath(path, paint)
        }

        // Current direction indicator (yellow line)
        val angle = Math.toRadians((currentYaw - 90.0)).toFloat()
        val x1 = cx + rInner * cos(angle)
        val y1 = cy + rInner * sin(angle)
        val x2 = cx + (rOuter + 6f) * cos(angle)
        val y2 = cy + (rOuter + 6f) * sin(angle)
        canvas.drawLine(x1, y1, x2, y2, paintCurrent)

        // Arrow pointing to the most-needed sector
        val bestSector = findBestSector()
        val bestAngle = Math.toRadians((bestSector * (360f / YAW_SECTORS) - 90.0)).toFloat()
        drawArrow(canvas, cx, cy, rOuter + 22f, bestAngle)
    }

    /** Vertical bar on the left showing pitch coverage. */
    private fun drawHeightBar(canvas: Canvas) {
        val barW = width * 0.05f
        val barH = height * 0.35f
        val left = width * 0.04f
        val top  = height * 0.33f

        canvas.drawRoundRect(
            left - 4f, top - 4f, left + barW + 4f, top + barH + 4f,
            8f, 8f, paintBg
        )

        val sectorH = barH / PITCH_SECTORS
        for (i in 0 until PITCH_SECTORS) {
            val covered = coverage.any { row -> row[i] }
            val paint   = if (covered) paintCovered else paintEmpty
            val sTop    = top + i * sectorH + 2f
            canvas.drawRoundRect(left, sTop, left + barW, sTop + sectorH - 4f, 4f, 4f, paint)
        }

        // Current pitch indicator
        val pitchNorm = ((currentPitch + 60f) / 120f).coerceIn(0f, 1f)
        val py = top + pitchNorm * barH
        canvas.drawLine(left - 6f, py, left + barW + 6f, py, paintCurrent)

        // Labels
        paintTextSmall.textAlign = Paint.Align.LEFT
        canvas.drawText("↑", left + barW * 0.1f, top - 6f, paintTextSmall)
        canvas.drawText("↓", left + barW * 0.1f, top + barH + 22f, paintTextSmall)
        paintTextSmall.textAlign = Paint.Align.CENTER
    }

    /** Text hint at the top guiding the user. */
    private fun drawGuideHint(canvas: Canvas) {
        val hint = buildHint()
        val cx = width * 0.5f
        val ty = height * 0.10f

        val tw = paintText.measureText(hint) + 24f
        canvas.drawRoundRect(cx - tw / 2f, ty - 40f, cx + tw / 2f, ty + 10f, 12f, 12f, paintBg)
        canvas.drawText(hint, cx, ty, paintText)
    }

    /** Coverage % in top-right corner. */
    private fun drawCoveragePercent(canvas: Canvas) {
        val pct   = coveragePercent()
        val label = "$pct%"
        val cx = width * 0.86f
        val ty = height * 0.08f
        val r  = width * 0.07f
        canvas.drawCircle(cx, ty, r, paintBg)
        canvas.drawText(label, cx, ty + paintTextSmall.textSize / 3f, paintTextSmall)
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private fun buildHint(): String {
        val pct = coveragePercent()
        if (pct >= 80) return "✓ Cobertura excelente"

        val pitchSec = pitchToSector(currentPitch)
        val yawSec   = yawToSector(currentYaw)

        // Count covered sectors at each pitch level
        val covered  = IntArray(PITCH_SECTORS) { p -> coverage.count { row -> row[p] } }
        val minPitch = covered.indexOf(covered.min())

        return when {
            covered[pitchSec] < YAW_SECTORS / 3 -> "Seguí girando en este nivel"
            minPitch < pitchSec                  -> "↑ Sube la cámara"
            minPitch > pitchSec                  -> "↓ Baja la cámara"
            coverage[yawSec].any { it }          -> "→ Avanzá un poco más"
            else                                 -> "📸 Capturando…"
        }
    }

    private fun findBestSector(): Int {
        var best = 0
        var minCov = Int.MAX_VALUE
        for (i in 0 until YAW_SECTORS) {
            val cov = coverage[i].count { it }
            if (cov < minCov) { minCov = cov; best = i }
        }
        return best
    }

    private fun drawArrow(canvas: Canvas, cx: Float, cy: Float, r: Float, angle: Float) {
        val ax = cx + r * cos(angle)
        val ay = cy + r * sin(angle)
        val size = 18f
        val a1 = angle + Math.toRadians(140.0).toFloat()
        val a2 = angle - Math.toRadians(140.0).toFloat()
        val path = Path()
        path.moveTo(ax, ay)
        path.lineTo(ax + size * cos(a1), ay + size * sin(a1))
        path.lineTo(ax + size * cos(a2), ay + size * sin(a2))
        path.close()
        canvas.drawPath(path, paintArrow)
    }

    private fun yawToSector(yaw: Float): Int =
        ((yaw % 360f + 360f) % 360f * YAW_SECTORS / 360f).toInt().coerceIn(0, YAW_SECTORS - 1)

    private fun pitchToSector(pitch: Float): Int =
        ((pitch + 60f) / 120f * PITCH_SECTORS).toInt().coerceIn(0, PITCH_SECTORS - 1)
}
