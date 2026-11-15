package com.purplevector.flamecam

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * One screen: status, an aim preview (only while this screen is open), and
 * Start/Stop. Everything real happens in StreamService; this just shows it.
 */
class MainActivity : Activity() {
    private lateinit var prefs: Prefs
    private lateinit var statusTv: TextView
    private lateinit var detailTv: TextView
    private lateinit var errorTv: TextView
    private lateinit var preview: ImageView
    private lateinit var startBtn: Button
    private lateinit var batteryBtn: Button
    private lateinit var hostEdit: EditText
    private val ui = Handler(Looper.getMainLooper())
    private var shownStamp = 0L

    private val purple = Color.parseColor("#7C3AED")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        setContentView(buildUi())
    }

    override fun onResume() {
        super.onResume()
        AppState.uiVisible = true
        ui.post(refresher)
    }

    override fun onPause() {
        AppState.uiVisible = false
        ui.removeCallbacks(refresher)
        super.onPause()
    }

    // ------------------------------------------------------------------ UI
    private fun buildUi(): View {
        val dp = resources.displayMetrics.density
        fun px(v: Int) = (v * dp).toInt()

        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(px(16), px(16), px(16), px(24))
        }
        col.addView(TextView(this).apply {
            text = "🔥 FlameCam"
            textSize = 26f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(Color.WHITE)
        })
        statusTv = TextView(this).apply {
            textSize = 18f
            setTextColor(Color.WHITE)
            setPadding(0, px(8), 0, 0)
        }
        col.addView(statusTv)
        detailTv = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.parseColor("#BBBBBB"))
        }
        col.addView(detailTv)
        errorTv = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.parseColor("#FF8A8A"))
        }
        col.addView(errorTv)

        preview = ImageView(this).apply {
            setBackgroundColor(Color.BLACK)
            scaleType = ImageView.ScaleType.FIT_CENTER
            adjustViewBounds = true
        }
        col.addView(preview, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, px(260)).apply {
            topMargin = px(12)
        })

        startBtn = Button(this).apply {
            textSize = 20f
            setTextColor(Color.WHITE)
            setBackgroundColor(purple)
            setOnClickListener { onStartStop() }
        }
        col.addView(startBtn, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, px(64)).apply {
            topMargin = px(12)
        })

        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row.addView(Button(this).apply {
            text = "Rotate 90°"
            setOnClickListener {
                prefs.rotationSteps = prefs.rotationSteps + 1
                StreamService.rotate(this@MainActivity)
            }
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        batteryBtn = Button(this).apply {
            text = "Allow background"
            setOnClickListener { askBatteryExemption() }
        }
        row.addView(batteryBtn, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        col.addView(row, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = px(8) })

        col.addView(TextView(this).apply {
            text = "When it says Streaming, press the power button. It keeps streaming with the " +
                "screen off. Keep the phone on a charger for long cooks."
            textSize = 14f
            setTextColor(Color.parseColor("#BBBBBB"))
            setPadding(0, px(14), 0, px(14))
        })

        col.addView(TextView(this).apply {
            text = "PC host (leave empty = find automatically)"
            textSize = 13f
            setTextColor(Color.parseColor("#888888"))
        })
        val hostRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        hostEdit = EditText(this).apply {
            hint = "auto (e.g. 192.168.1.20:8080)"
            setText(prefs.manualHost)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setTextColor(Color.WHITE)
            setHintTextColor(Color.parseColor("#666666"))
            isSingleLine = true
        }
        hostRow.addView(hostEdit, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        hostRow.addView(Button(this).apply {
            text = "Save"
            setOnClickListener { prefs.manualHost = hostEdit.text.toString() }
        })
        col.addView(hostRow)

        return ScrollView(this).apply {
            setBackgroundColor(Color.parseColor("#0B0B10"))
            fitsSystemWindows = true
            addView(col)
        }
    }

    private val refresher = object : Runnable {
        override fun run() {
            refresh()
            ui.postDelayed(this, 500)
        }
    }

    private fun refresh() {
        val running = AppState.running
        startBtn.text = if (running) "Stop streaming" else "▶  Start streaming"
        statusTv.text = if (running) AppState.status else "Not streaming"
        val bits = mutableListOf<String>()
        if (running && AppState.connected) {
            bits.add(AppState.host)
            bits.add(String.format("%.1f fps", AppState.fps))
            if (AppState.resolution.isNotEmpty()) bits.add(AppState.resolution)
            if (AppState.audioOn) bits.add("mic on")
        }
        detailTv.text = bits.joinToString("  ·  ")
        errorTv.text = AppState.cameraError
        errorTv.visibility = if (AppState.cameraError.isEmpty()) View.GONE else View.VISIBLE

        val ignoring = getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
        batteryBtn.visibility = if (ignoring) View.GONE else View.VISIBLE

        val stamp = AppState.previewStamp
        val jpeg = AppState.preview
        if (running && jpeg != null && stamp != shownStamp) {
            shownStamp = stamp
            val opts = BitmapFactory.Options().apply { inSampleSize = 2 }
            BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, opts)?.let { preview.setImageBitmap(it) }
        } else if (!running) {
            preview.setImageDrawable(null)
        }
    }

    // ------------------------------------------------------------ actions
    private fun onStartStop() {
        if (AppState.running) {
            StreamService.stop(this)
            return
        }
        val need = mutableListOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) need.add(Manifest.permission.POST_NOTIFICATIONS)
        val missing = need.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isEmpty()) {
            StreamService.start(this)
        } else {
            requestPermissions(missing.toTypedArray(), 1)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            StreamService.start(this)
        } else {
            AppState.status = "Camera permission is needed to stream."
            statusTv.text = AppState.status
        }
    }

    private fun askBatteryExemption() {
        try {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")))
        } catch (_: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }
}
