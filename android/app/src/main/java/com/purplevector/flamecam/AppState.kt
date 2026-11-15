package com.purplevector.flamecam

import android.content.Context

/** Live state shared between StreamService (writer) and MainActivity (reader). */
object AppState {
    @Volatile var running = false
    @Volatile var status = "Idle"
    @Volatile var host = ""
    @Volatile var connected = false
    @Volatile var fps = 0.0
    @Volatile var resolution = ""
    @Volatile var audioOn = false
    @Volatile var cameraError = ""

    /** Latest JPEG for the on-screen aim preview (only produced while the UI is visible). */
    @Volatile var preview: ByteArray? = null
    @Volatile var previewStamp = 0L
    @Volatile var uiVisible = false
}

/** Host-pushed settings (lancam_host sends {"type":"config",...} on connect). */
data class HostConfig(
    val fps: Int = 10,
    val quality: Int = 80,
    val width: Int = 1280,
    val height: Int = 720,
    val audio: Boolean = false,
)

class Prefs(ctx: Context) {
    private val sp = ctx.getSharedPreferences("flamecam", Context.MODE_PRIVATE)

    var lastHost: String?
        get() = sp.getString("last_host", null)
        set(v) = sp.edit().putString("last_host", v).apply()

    var lastPort: Int
        get() = sp.getInt("last_port", 8080)
        set(v) = sp.edit().putInt("last_port", v).apply()

    /** Optional "ip" or "ip:port" override for networks that block broadcast. */
    var manualHost: String
        get() = sp.getString("manual_host", "") ?: ""
        set(v) = sp.edit().putString("manual_host", v.trim()).apply()

    /** Extra rotation in quarter turns (0..3) on top of how the phone is held at start. */
    var rotationSteps: Int
        get() = sp.getInt("rotation_steps", 0)
        set(v) = sp.edit().putInt("rotation_steps", ((v % 4) + 4) % 4).apply()

    var tgToken: String
        get() = sp.getString("tg_token", "") ?: ""
        set(v) = sp.edit().putString("tg_token", v).apply()

    var tgChat: String
        get() = sp.getString("tg_chat", "") ?: ""
        set(v) = sp.edit().putString("tg_chat", v).apply()
}
