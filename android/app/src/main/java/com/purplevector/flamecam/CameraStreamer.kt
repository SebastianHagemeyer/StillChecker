package com.purplevector.flamecam

import android.content.Context
import android.graphics.Bitmap
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.os.SystemClock
import android.util.Log
import android.util.Range
import android.util.Size
import androidx.annotation.OptIn
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.CameraState
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.util.concurrent.Executors

/**
 * Back camera -> RGBA frames (CameraX ImageAnalysis, no preview surface, so it
 * keeps running with the screen off inside the foreground service) -> one
 * self-contained JPEG per frame -> HostLink.
 *
 * Power: the sensor's auto-exposure fps range is lowered to the slowest range
 * that still covers the target fps, so the camera itself runs slow instead of
 * producing 30 fps that we throw away. Frames are only JPEG-encoded when the
 * host link is up (or the aim preview is on screen).
 */
class CameraStreamer(
    private val ctx: Context,
    private val owner: LifecycleOwner,
    private val link: HostLink,
) {
    private val analysisExec = Executors.newSingleThreadExecutor { r -> Thread(r, "flame-encode") }
    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var cfg: HostConfig? = null
    private var rotation = 0

    @Volatile var targetFps = 10
    @Volatile private var quality = 80
    @Volatile var lastFrameAt = 0L            // elapsedRealtime of last frame sent
        private set
    @Volatile var lastCameraFrameAt = 0L      // elapsedRealtime of last frame from the camera
        private set
    @Volatile var measuredFps = 0.0
        private set

    // encoder state (encode thread only)
    private var lastSent = 0L
    private var lastPreview = 0L
    private var bitmap: Bitmap? = null
    private var tight: ByteBuffer? = null
    private val jpegOut = ByteArrayOutputStream(256 * 1024)
    private var fpsWindowStart = 0L
    private var fpsWindowCount = 0

    /** (Re)bind the camera. Main thread. No-op if nothing changed. */
    fun start(newCfg: HostConfig, surfaceRotation: Int, force: Boolean = false) {
        targetFps = newCfg.fps
        quality = newCfg.quality
        val old = cfg
        if (!force && old != null && provider != null && camera != null &&
            old.width == newCfg.width && old.height == newCfg.height &&
            old.fps == newCfg.fps && rotation == surfaceRotation) {
            return
        }
        cfg = newCfg
        rotation = surfaceRotation
        val future = ProcessCameraProvider.getInstance(ctx)
        future.addListener({
            try {
                val p = future.get()
                provider = p
                bind(p, newCfg)
            } catch (e: Exception) {
                Log.e(TAG, "camera provider failed", e)
                AppState.cameraError = "Camera unavailable: ${e.message}"
            }
        }, ContextCompat.getMainExecutor(ctx))
    }

    fun stop() {
        try { provider?.unbindAll() } catch (_: Exception) {}
        camera = null
        analysisExec.shutdown()
    }

    @OptIn(ExperimentalCamera2Interop::class)
    private fun bind(p: ProcessCameraProvider, c: HostConfig) {
        p.unbindAll()
        val selector = ResolutionSelector.Builder()
            .setResolutionStrategy(
                ResolutionStrategy(Size(c.width, c.height),
                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER))
            .build()
        val builder = ImageAnalysis.Builder()
            .setResolutionSelector(selector)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
            .setOutputImageRotationEnabled(true)
            .setTargetRotation(rotation)
        val ext = Camera2Interop.Extender(builder)
        aeRangeFor(c.fps)?.let { ext.setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, it) }
        // Fixed scene: continuous video AF is gentler than picture AF (less hunting).
        ext.setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE,
            CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO)

        val analysis = builder.build()
        analysis.setAnalyzer(analysisExec) { img -> analyze(img) }
        try {
            camera = p.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, analysis)
            AppState.cameraError = ""
            camera?.cameraInfo?.cameraState?.observe(owner) { st ->
                val err = st.error
                if (err != null) {
                    AppState.cameraError = "Camera error ${err.code}" +
                        if (st.type == CameraState.Type.CLOSED) " (closed)" else ""
                    Log.w(TAG, "camera state ${st.type} error ${err.code}")
                } else if (st.type == CameraState.Type.OPEN) {
                    AppState.cameraError = ""
                }
            }
            Log.i(TAG, "camera bound ${c.width}x${c.height} @${c.fps} rot=$rotation")
        } catch (e: Exception) {
            Log.e(TAG, "bind failed", e)
            AppState.cameraError = "Camera bind failed: ${e.message}"
        }
    }

    /** Slowest supported AE range whose upper bound still reaches the target fps. */
    private fun aeRangeFor(fps: Int): Range<Int>? = try {
        val cm = ctx.getSystemService(CameraManager::class.java)
        val id = cm.cameraIdList.firstOrNull {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.LENS_FACING) ==
                CameraCharacteristics.LENS_FACING_BACK
        }
        val ranges = id?.let {
            cm.getCameraCharacteristics(it).get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
        }
        ranges?.filter { it.upper >= fps }
            ?.sortedWith(compareBy<Range<Int>>({ it.upper }, { it.lower }))
            ?.firstOrNull()
    } catch (e: Exception) {
        null
    }

    // ------------------------------------------------------------- encoder
    private fun analyze(img: ImageProxy) {
        try {
            val now = SystemClock.elapsedRealtime()
            lastCameraFrameAt = now
            val sendDue = link.connected && now - lastSent >= (1000L / targetFps.coerceAtLeast(1)) - 5
            val previewDue = AppState.uiVisible && now - lastPreview >= 250
            if (!sendDue && !previewDue) return

            val jpeg = encode(img) ?: return
            if (sendDue && link.sendFrame(jpeg)) {
                lastSent = now
                lastFrameAt = now
                countFps(now)
            }
            if (previewDue) {
                lastPreview = now
                AppState.preview = jpeg
                AppState.previewStamp = now
            }
            AppState.resolution = "${img.width}x${img.height}"
        } catch (e: Exception) {
            Log.w(TAG, "encode failed: $e")
        } finally {
            img.close()
        }
    }

    private fun encode(img: ImageProxy): ByteArray? {
        val w = img.width
        val h = img.height
        val plane = img.planes[0]
        val src = plane.buffer
        val rowBytes = w * 4
        var bmp = bitmap
        if (bmp == null || bmp.width != w || bmp.height != h) {
            bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
            bitmap = bmp
        }
        src.rewind()
        if (plane.rowStride == rowBytes && plane.pixelStride == 4) {
            bmp.copyPixelsFromBuffer(src)
        } else {
            // Padded rows: pack them tightly first.
            var t = tight
            if (t == null || t.capacity() != rowBytes * h) {
                t = ByteBuffer.allocateDirect(rowBytes * h)
                tight = t
            }
            t!!.clear()
            for (row in 0 until h) {
                val start = row * plane.rowStride
                src.limit(start + rowBytes)
                src.position(start)
                t.put(src)
                src.limit(src.capacity())
            }
            t.rewind()
            bmp.copyPixelsFromBuffer(t)
        }
        jpegOut.reset()
        if (!bmp.compress(Bitmap.CompressFormat.JPEG, quality, jpegOut)) return null
        return jpegOut.toByteArray()
    }

    private fun countFps(now: Long) {
        if (fpsWindowStart == 0L) fpsWindowStart = now
        fpsWindowCount++
        val dt = now - fpsWindowStart
        if (dt >= 2000) {
            measuredFps = fpsWindowCount * 1000.0 / dt
            AppState.fps = measuredFps
            fpsWindowStart = now
            fpsWindowCount = 0
        }
    }

    companion object { const val TAG = "FlameCamera" }
}
