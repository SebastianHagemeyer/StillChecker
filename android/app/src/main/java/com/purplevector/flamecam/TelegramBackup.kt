package com.purplevector.flamecam

import android.content.Context
import android.util.Log
import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Phone-side backup alerts through the Empire bot, for when the PC host itself
 * is down (so the PC cannot send anything). Send-only: never getUpdates, which
 * would fight uload's poller for the same bot.
 *
 * Credentials arrive once via `install.py`, which pipes telegram.json into this
 * app's private files dir (adb run-as, never shared storage). We move them into
 * private prefs and delete the file. Uses the default network (Wi-Fi or mobile data), not the LAN binding.
 */
class TelegramBackup(private val ctx: Context, private val prefs: Prefs) {
    private val exec = Executors.newSingleThreadExecutor { r -> Thread(r, "flame-telegram") }
    private val http = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    val configured: Boolean get() = prefs.tgToken.isNotEmpty() && prefs.tgChat.isNotEmpty()

    fun importProvisionFile() {
        // install.py pipes it into the private files dir via `run-as`; the
        // external dir is a fallback for manual `adb push`.
        val f = listOf(File(ctx.filesDir, "telegram.json"), File(ctx.getExternalFilesDir(null), "telegram.json"))
            .firstOrNull { it.exists() } ?: return
        try {
            val o = JSONObject(f.readText())
            val tok = o.optString("token")
            val chat = o.optString("chat_id")
            if (tok.isNotEmpty() && chat.isNotEmpty()) {
                prefs.tgToken = tok
                prefs.tgChat = chat
                Log.i(TAG, "telegram backup credentials imported")
            }
        } catch (e: Exception) {
            Log.w(TAG, "bad telegram.json: $e")
        } finally {
            f.delete()
        }
    }

    fun send(text: String) {
        if (!configured) return
        val token = prefs.tgToken
        val chat = prefs.tgChat
        exec.execute {
            val body = FormBody.Builder()
                .add("chat_id", chat)
                .add("text", "🔥 $text")
                .add("disable_web_page_preview", "true")
                .build()
            val req = Request.Builder().url("https://api.telegram.org/bot$token/sendMessage").post(body).build()
            for (attempt in 1..5) {
                try {
                    http.newCall(req).execute().use { r ->
                        if (r.isSuccessful) return@execute
                        if (r.code in 400..499 && r.code != 429) {
                            Log.w(TAG, "telegram rejected ${r.code}")
                            return@execute
                        }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "telegram send failed ($attempt/5): $e")
                }
                Thread.sleep(10_000L * attempt)
            }
        }
    }

    fun shutdown() = exec.shutdown()

    companion object { const val TAG = "FlameTelegram" }
}
