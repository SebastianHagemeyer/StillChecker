package com.purplevector.flamecam

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.NetworkInterface
import java.net.SocketTimeoutException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Finds the PC host on the LAN and keeps the video (and optional audio)
 * WebSockets alive, forever, until stop().
 *
 *  discover:  UDP broadcast "FLAMECAM_DISCOVER 1" -> host replies JSON with its ws port;
 *             the host IP is the reply's source address (survives DHCP changes).
 *  connect:   ws://host:port/ws    binary JPEG frames up, text JSON both ways
 *             ws://host:port/audio PCM16 mono (only when the host enables audio)
 *  recover:   any drop -> backoff (0.5 s .. 5 s) -> reconnect; after repeated
 *             failures re-discover, so a new PC IP is picked up automatically.
 *
 * Sockets are bound to the Wi-Fi network when one exists, so LAN traffic never
 * gets routed to mobile data even if Android prefers cellular for internet.
 * All state changes run on one executor thread; stale OkHttp callbacks from an
 * older connection are ignored via a generation counter.
 */
class HostLink(
    private val ctx: Context,
    private val prefs: Prefs,
    private val cb: Callback,
) {
    interface Callback {
        fun onStatus(text: String)
        fun onConnected(host: String)
        fun onDisconnected(reason: String)
        fun onConfig(cfg: HostConfig)
        /** Host said it is shutting down on purpose (Stop pressed on the PC). */
        fun onHostBye(reason: String)
    }

    companion object {
        const val TAG = "FlameLink"
        const val DISCOVERY_PORT = 47820
        const val DEFAULT_WS_PORT = 8080
        private const val MAX_QUEUE_BYTES = 1_500_000L
    }

    private val exec = Executors.newSingleThreadScheduledExecutor { r -> Thread(r, "flame-link") }
    private val baseClient = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .pingInterval(4, TimeUnit.SECONDS)   // dead link detected within ~8 s
        .build()

    @Volatile private var running = false
    @Volatile private var video: WebSocket? = null
    @Volatile private var audio: WebSocket? = null
    @Volatile var connected = false
        private set
    @Volatile var audioWanted = false
        private set
    @Volatile var audioSampleRate = 8000

    private var client: OkHttpClient = baseClient
    private var gen = 0
    private var failures = 0
    private var hostIp: String? = null
    private var hostPort = DEFAULT_WS_PORT

    fun start() {
        running = true
        exec.execute { attempt() }
    }

    fun stop() {
        running = false
        exec.execute { closeAll("stopped") }
        exec.shutdown()
    }

    /** Called when Wi-Fi comes back etc.: skip the backoff wait. */
    fun kick() {
        if (running && !connected) exec.execute { if (!connected && video == null) attempt() }
    }

    // ------------------------------------------------------------------ send
    fun sendFrame(jpeg: ByteArray): Boolean {
        val ws = video ?: return false
        if (!connected) return false
        if (ws.queueSize() > MAX_QUEUE_BYTES) return false   // backpressure: drop, never lag
        return ws.send(jpeg.toByteString())
    }

    fun sendText(json: String): Boolean {
        val ws = video ?: return false
        return connected && ws.send(json)
    }

    fun sendAudio(pcm: ByteArray, len: Int): Boolean {
        val ws = audio ?: return false
        if (ws.queueSize() > 256_000L) return false
        return ws.send(pcm.toByteString(0, len))
    }

    // ------------------------------------------------------------ connection
    private fun attempt() {
        if (!running || video != null) return
        val target = pickTarget()
        if (target == null) {
            cb.onStatus("Searching for the PC host on Wi-Fi...")
            exec.schedule({ attempt() }, 1500, TimeUnit.MILLISECONDS)
            return
        }
        hostIp = target.first
        hostPort = target.second
        connectVideo()
    }

    private fun pickTarget(): Pair<String, Int>? {
        val manual = prefs.manualHost
        if (manual.isNotEmpty()) {
            val parts = manual.split(":")
            return parts[0] to (parts.getOrNull(1)?.toIntOrNull() ?: DEFAULT_WS_PORT)
        }
        val wifi = wifiNetwork()
        client = if (wifi != null) baseClient.newBuilder().socketFactory(wifi.socketFactory).build() else baseClient

        val last = prefs.lastHost
        // Fast path: right after a drop, retry the known host a couple of times first.
        if (last != null && failures in 1..2) return last to prefs.lastPort
        cb.onStatus("Searching for the PC host on Wi-Fi...")
        val found = discover(wifi)
        if (found != null) {
            prefs.lastHost = found.first
            prefs.lastPort = found.second
            return found
        }
        // Broadcast blocked on this network? Fall back to the remembered host.
        return if (last != null) last to prefs.lastPort else null
    }

    private fun connectVideo() {
        val myGen = ++gen
        val url = "ws://$hostIp:$hostPort/ws"
        cb.onStatus("Connecting to $hostIp:$hostPort...")
        video = client.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = exec.execute {
                if (myGen != gen) return@execute
                failures = 0
                connected = true
                cb.onConnected("$hostIp:$hostPort")
            }

            override fun onMessage(webSocket: WebSocket, text: String) = exec.execute {
                if (myGen != gen) return@execute
                handleText(text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {}

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) =
                exec.execute { dropped(myGen, "closed ($code)") }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                exec.execute { dropped(myGen, t.message ?: t.javaClass.simpleName) }
        })
    }

    private fun handleText(text: String) {
        val o = try { JSONObject(text) } catch (e: Exception) { return }
        when (o.optString("type")) {
            "config" -> {
                val q = o.optDouble("quality", 0.8)
                val cfg = HostConfig(
                    fps = o.optInt("fps", 10).coerceIn(1, 30),
                    quality = (if (q <= 1.0) q * 100 else q).toInt().coerceIn(10, 100),
                    width = o.optInt("width", 1280),
                    height = o.optInt("height", 720),
                    audio = o.optBoolean("audio", false),
                )
                audioWanted = cfg.audio
                cb.onConfig(cfg)
                if (cfg.audio) openAudio(gen) else closeAudio()
            }
            "bye" -> cb.onHostBye(o.optString("reason", "host stopped"))
        }
    }

    private fun dropped(myGen: Int, reason: String) {
        if (myGen != gen) return
        val was = connected
        connected = false
        video = null
        closeAudio()
        failures++
        Log.i(TAG, "link dropped: $reason (failures=$failures)")
        if (was) cb.onDisconnected(reason)
        if (!running) return
        cb.onStatus("Lost the PC host ($reason). Reconnecting...")
        val delay = (500L * failures).coerceAtMost(5000L)
        exec.schedule({ attempt() }, delay, TimeUnit.MILLISECONDS)
    }

    private fun openAudio(myGen: Int) {
        if (audio != null || !connected || myGen != gen) return
        val url = "ws://$hostIp:$hostPort/audio"
        audio = client.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(JSONObject().put("type", "hello")
                    .put("sampleRate", audioSampleRate).put("channels", 1).toString())
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = exec.execute { audioDropped(myGen, webSocket) }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) = exec.execute { audioDropped(myGen, webSocket) }
        })
    }

    private fun audioDropped(myGen: Int, ws: WebSocket) {
        if (audio === ws) audio = null
        // Video still up and host still wants audio: retry shortly.
        if (running && connected && audioWanted && myGen == gen) {
            exec.schedule({ openAudio(myGen) }, 2, TimeUnit.SECONDS)
        }
    }

    private fun closeAudio() {
        audio?.close(1000, null)
        audio = null
    }

    private fun closeAll(reason: String) {
        gen++
        connected = false
        closeAudio()
        video?.close(1000, reason)
        video = null
    }

    // ------------------------------------------------------------- discovery
    private fun wifiNetwork(): Network? {
        val cm = ctx.getSystemService(ConnectivityManager::class.java) ?: return null
        @Suppress("DEPRECATION")
        return cm.allNetworks.firstOrNull { n ->
            cm.getNetworkCapabilities(n)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        }
    }

    private fun broadcastTargets(): List<InetAddress> {
        val out = LinkedHashSet<InetAddress>()
        try {
            for (ni in NetworkInterface.getNetworkInterfaces()) {
                if (!ni.isUp || ni.isLoopback) continue
                for (ia in ni.interfaceAddresses) {
                    if (ia.address is Inet4Address) ia.broadcast?.let { out.add(it) }
                }
            }
        } catch (_: Exception) {
        }
        out.add(InetAddress.getByName("255.255.255.255"))
        return out.toList()
    }

    private fun discover(wifi: Network?): Pair<String, Int>? {
        try {
            DatagramSocket().use { s ->
                try { wifi?.bindSocket(s) } catch (_: Exception) {}
                s.broadcast = true
                val msg = "FLAMECAM_DISCOVER 1".toByteArray()
                val buf = ByteArray(1024)
                repeat(2) {
                    for (addr in broadcastTargets()) {
                        try { s.send(DatagramPacket(msg, msg.size, addr, DISCOVERY_PORT)) } catch (_: Exception) {}
                    }
                    val deadline = System.currentTimeMillis() + 700
                    while (true) {
                        val left = deadline - System.currentTimeMillis()
                        if (left <= 0) break
                        s.soTimeout = left.toInt()
                        val p = DatagramPacket(buf, buf.size)
                        try {
                            s.receive(p)
                        } catch (e: SocketTimeoutException) {
                            break
                        }
                        try {
                            val o = JSONObject(String(p.data, 0, p.length))
                            if (o.optString("svc") == "flamecam") {
                                val ip = p.address.hostAddress ?: continue
                                return ip to o.optInt("port", DEFAULT_WS_PORT)
                            }
                        } catch (_: Exception) {
                        }
                    }
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "discovery failed: $e")
        }
        return null
    }
}
