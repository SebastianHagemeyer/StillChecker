package com.purplevector.flamecam

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.display.DisplayManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import android.view.Display
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import org.json.JSONObject

/**
 * The whole streaming pipeline lives here, in a camera|microphone foreground
 * service, so it keeps running with the screen off and the app in the
 * background. Holds a partial wake lock (CPU) and Wi-Fi locks (radio stays in
 * high-performance mode) for the duration.
 *
 * Watchdogs, every 5 s:
 *   - telemetry to the host (battery, temperature, Wi-Fi, fps)
 *   - camera stalled > 15 s -> rebind the camera
 *   - phone running hot -> halve fps until it cools down
 *   - PC host unreachable for 3 min (and it did not say "bye") -> backup
 *     Telegram alert straight from the phone, plus a "reconnected" line later
 */
class StreamService : LifecycleService(), HostLink.Callback {

    companion object {
        const val TAG = "FlameService"
        const val ACTION_START = "com.purplevector.flamecam.START"
        const val ACTION_STOP = "com.purplevector.flamecam.STOP"
        const val ACTION_ROTATE = "com.purplevector.flamecam.ROTATE"
        private const val CHANNEL = "stream"
        private const val NOTIF_ID = 1
        private const val TICK_MS = 5000L
        private const val CAMERA_STALL_MS = 15_000L
        private const val BACKUP_ALERT_MS = 3 * 60_000L

        fun start(ctx: Context) = ContextCompat.startForegroundService(
            ctx, Intent(ctx, StreamService::class.java).setAction(ACTION_START))

        fun rotate(ctx: Context) {
            if (AppState.running) ctx.startService(Intent(ctx, StreamService::class.java).setAction(ACTION_ROTATE))
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, StreamService::class.java))
        }
    }

    private lateinit var prefs: Prefs
    private lateinit var link: HostLink
    private lateinit var camera: CameraStreamer
    private lateinit var audio: AudioStreamer
    private lateinit var telegram: TelegramBackup
    private val main = Handler(Looper.getMainLooper())

    @Volatile private var cfg = HostConfig()
    private var started = false
    private var wakeLock: PowerManager.WakeLock? = null
    private val wifiLocks = mutableListOf<WifiManager.WifiLock>()
    private var netCallback: ConnectivityManager.NetworkCallback? = null

    // backup-alert state (main thread)
    @Volatile private var everConnected = false
    @Volatile private var hostByeAt = 0L
    @Volatile private var lastHost = ""
    private var downSince = 0L
    private var backupAlerted = false
    private var lastCameraRebind = 0L
    private var throttled = false
    private var lastNotifText = ""

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        link = HostLink(this, prefs, this)
        camera = CameraStreamer(this, this, link)
        audio = AudioStreamer(this, link)
        telegram = TelegramBackup(this, prefs)
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, "Streaming", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shown while FlameCam streams to the PC"
                setShowBadge(false)
            })
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_ROTATE -> {
                if (started) camera.start(cfg, surfaceRotation())
                return START_NOT_STICKY
            }
        }
        if (started) return START_NOT_STICKY
        try {
            startForegroundWithTypes()
        } catch (e: Exception) {
            // Android refuses camera FGS starts from the background; needs a tap in the app.
            Log.e(TAG, "startForeground refused", e)
            AppState.status = "Android blocked the start. Open FlameCam and tap Start."
            stopSelf()
            return START_NOT_STICKY
        }
        started = true
        AppState.running = true
        acquireLocks()
        watchWifi()
        telegram.importProvisionFile()
        link.start()
        camera.start(cfg, surfaceRotation())
        main.postDelayed(tick, TICK_MS)
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        main.removeCallbacksAndMessages(null)
        if (started) {
            // Tell the host this is on purpose, so it reports "phone stopped" not "STREAM LOST".
            link.sendText(JSONObject().put("type", "bye").put("reason", "Stop pressed on the phone").toString())
        }
        link.stop()
        audio.stop()
        camera.stop()
        telegram.shutdown()
        netCallback?.let {
            try { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) } catch (_: Exception) {}
        }
        releaseLocks()
        AppState.running = false
        AppState.connected = false
        AppState.fps = 0.0
        AppState.status = "Stopped"
        super.onDestroy()
    }

    // ------------------------------------------------------- HostLink.Callback
    override fun onStatus(text: String) {
        AppState.status = text
        updateNotification(text)
    }

    override fun onConnected(host: String) {
        everConnected = true
        hostByeAt = 0L
        lastHost = host
        AppState.connected = true
        AppState.host = host
        link.sendText(JSONObject()
            .put("type", "hello")
            .put("client", "android")
            .put("model", Build.MODEL)
            .put("manufacturer", Build.MANUFACTURER)
            .put("android", Build.VERSION.RELEASE)
            .put("app", BuildConfig.VERSION_NAME)
            .toString())
        link.sendText(telemetryJson())
        onStatus("Streaming to $host")
    }

    override fun onDisconnected(reason: String) {
        AppState.connected = false
        AppState.fps = 0.0
    }

    override fun onConfig(cfg: HostConfig) {
        this.cfg = cfg
        // Audio first and on this thread, so the sample rate is known before
        // HostLink opens the audio socket and sends its hello.
        if (cfg.audio && hasPermission(Manifest.permission.RECORD_AUDIO)) audio.start() else audio.stop()
        main.post {
            throttled = false
            camera.start(cfg, surfaceRotation())
        }
    }

    override fun onHostBye(reason: String) {
        hostByeAt = SystemClock.elapsedRealtime()
        onStatus("PC host stopped ($reason). Waiting for it to come back...")
    }

    // ------------------------------------------------------------- watchdogs
    private val tick = object : Runnable {
        override fun run() {
            try {
                watchdogs()
            } catch (e: Exception) {
                Log.w(TAG, "tick failed: $e")
            }
            main.postDelayed(this, TICK_MS)
        }
    }

    private fun watchdogs() {
        val now = SystemClock.elapsedRealtime()

        if (link.connected) link.sendText(telemetryJson())

        // Camera watchdog: no frames from the sensor for a while -> rebind.
        val lastCam = camera.lastCameraFrameAt
        if (lastCam != 0L && now - lastCam > CAMERA_STALL_MS && now - lastCameraRebind > 30_000L) {
            Log.w(TAG, "camera stalled, rebinding")
            lastCameraRebind = now
            camera.start(cfg, surfaceRotation(), force = true)
        }

        // Thermal: halve fps when the phone runs hot, restore when it cools.
        if (Build.VERSION.SDK_INT >= 29) {
            val ts = getSystemService(PowerManager::class.java).currentThermalStatus
            if (!throttled && ts >= PowerManager.THERMAL_STATUS_SEVERE) {
                throttled = true
                camera.targetFps = (cfg.fps / 2).coerceAtLeast(2)
            } else if (throttled && ts <= PowerManager.THERMAL_STATUS_LIGHT) {
                throttled = false
                camera.targetFps = cfg.fps
            }
        }

        // Backup alert: the PC side cannot report its own death.
        if (link.connected) {
            if (backupAlerted) {
                telegram.send("FlameCam phone: reconnected to the PC host after ${fmt(now - downSince)}.")
                backupAlerted = false
            }
            downSince = 0L
        } else if (everConnected && hostByeAt == 0L) {
            if (downSince == 0L) downSince = now
            if (!backupAlerted && now - downSince >= BACKUP_ALERT_MS) {
                backupAlerted = true
                val wifi = if (wifiConnected()) "phone Wi-Fi is up" else "phone Wi-Fi is DOWN"
                telegram.send("FlameCam phone (backup alert): cannot reach the PC host $lastHost for " +
                    "${fmt(now - downSince)}. The PC, the LAN Cam host or the router may be down ($wifi, " +
                    "battery ${batteryLevel()}%). The phone keeps retrying.")
            }
        }

        if (!link.connected) AppState.fps = 0.0
        val camErr = AppState.cameraError
        updateNotification(if (camErr.isNotEmpty()) camErr else AppState.status)
    }

    // ------------------------------------------------------------- telemetry
    private fun telemetryJson(): String {
        val o = JSONObject().put("type", "telemetry")
        o.put("battery", batteryLevel())
        o.put("charging", getSystemService(BatteryManager::class.java).isCharging)
        val bi = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val t = bi?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE) ?: Int.MIN_VALUE
        if (t != Int.MIN_VALUE) o.put("battery_temp_c", t / 10.0)
        if (Build.VERSION.SDK_INT >= 29) {
            o.put("thermal", getSystemService(PowerManager::class.java).currentThermalStatus)
        }
        try {
            @Suppress("DEPRECATION")
            val rssi = applicationContext.getSystemService(WifiManager::class.java).connectionInfo.rssi
            if (rssi in -127..0) o.put("wifi_rssi", rssi)
        } catch (_: Exception) {}
        o.put("fps", Math.round(camera.measuredFps * 10) / 10.0)
        o.put("target_fps", camera.targetFps)
        o.put("res", AppState.resolution)
        o.put("audio", AppState.audioOn)
        o.put("screen_on", getSystemService(PowerManager::class.java).isInteractive)
        o.put("throttled", throttled)
        if (AppState.cameraError.isNotEmpty()) o.put("camera_error", AppState.cameraError)
        return o.toString()
    }

    private fun batteryLevel(): Int =
        getSystemService(BatteryManager::class.java).getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)

    private fun wifiConnected(): Boolean {
        val cm = getSystemService(ConnectivityManager::class.java)
        @Suppress("DEPRECATION")
        return cm.allNetworks.any { cm.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true }
    }

    // ----------------------------------------------------------------- setup
    private fun startForegroundWithTypes() {
        var types = 0
        if (Build.VERSION.SDK_INT >= 30) {
            types = ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
            if (hasPermission(Manifest.permission.RECORD_AUDIO)) {
                types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            }
        }
        ServiceCompat.startForeground(this, NOTIF_ID, buildNotification("Starting..."), types)
    }

    private fun buildNotification(text: String): Notification {
        val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), flags)
        val stop = PendingIntent.getService(this, 1,
            Intent(this, StreamService::class.java).setAction(ACTION_STOP), flags)
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_flame)
            .setContentTitle("FlameCam")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun updateNotification(text: String) {
        if (!started || text == lastNotifText) return
        lastNotifText = text
        try {
            getSystemService(NotificationManager::class.java).notify(NOTIF_ID, buildNotification(text))
        } catch (_: Exception) {}
    }

    @Suppress("DEPRECATION")
    private fun acquireLocks() {
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "flamecam:stream")
            .apply { setReferenceCounted(false); acquire() }
        val wm = applicationContext.getSystemService(WifiManager::class.java)
        val modes = mutableListOf(WifiManager.WIFI_MODE_FULL_HIGH_PERF)
        if (Build.VERSION.SDK_INT >= 29) modes.add(WifiManager.WIFI_MODE_FULL_LOW_LATENCY)
        for (m in modes) {
            try {
                wifiLocks.add(wm.createWifiLock(m, "flamecam:wifi$m").apply { setReferenceCounted(false); acquire() })
            } catch (e: Exception) {
                Log.w(TAG, "wifi lock $m failed: $e")
            }
        }
    }

    private fun releaseLocks() {
        try { wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
        for (l in wifiLocks) try { l.release() } catch (_: Exception) {}
        wifiLocks.clear()
    }

    /** Wi-Fi back -> reconnect right away instead of waiting out the backoff. */
    private fun watchWifi() {
        val cm = getSystemService(ConnectivityManager::class.java)
        val cbk = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = link.kick()
        }
        try {
            cm.registerNetworkCallback(
                NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build(), cbk)
            netCallback = cbk
        } catch (e: Exception) {
            Log.w(TAG, "network callback failed: $e")
        }
    }

    private fun surfaceRotation(): Int {
        val display = getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
        return ((display?.rotation ?: 0) + prefs.rotationSteps) % 4
    }

    private fun hasPermission(p: String) =
        ContextCompat.checkSelfPermission(this, p) == PackageManager.PERMISSION_GRANTED

    private fun fmt(ms: Long): String {
        val s = ms / 1000
        return if (s >= 3600) "${s / 3600}h ${(s % 3600) / 60}m" else if (s >= 60) "${s / 60}m ${s % 60}s" else "${s}s"
    }
}
