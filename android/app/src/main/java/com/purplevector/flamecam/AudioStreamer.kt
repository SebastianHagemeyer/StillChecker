package com.purplevector.flamecam

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Mic -> 8 kHz mono PCM16 -> HostLink audio socket, for lancam_host's rattle
 * detector. Uses the least-processed source available (no AGC / noise
 * suppression), matching what the Safari page asked the browser for.
 */
class AudioStreamer(private val ctx: Context, private val link: HostLink) {
    @Volatile private var running = false
    private var thread: Thread? = null

    val sampleRate: Int get() = chosenRate
    private var chosenRate = 8000

    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (running) return true
        val source = pickSource()
        var rec: AudioRecord? = null
        for (sr in intArrayOf(8000, 16000, 48000)) {
            try {
                val min = AudioRecord.getMinBufferSize(sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                if (min <= 0) continue
                val r = AudioRecord(source, sr, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(min, sr))
                if (r.state == AudioRecord.STATE_INITIALIZED) {
                    rec = r
                    chosenRate = sr
                    break
                }
                r.release()
            } catch (e: Exception) {
                Log.w(TAG, "AudioRecord $sr failed: $e")
            }
        }
        if (rec == null) return false
        link.audioSampleRate = chosenRate
        running = true
        val r = rec
        thread = Thread({ loop(r) }, "flame-audio").also { it.start() }
        AppState.audioOn = true
        return true
    }

    fun stop() {
        running = false
        thread?.join(500)
        thread = null
        AppState.audioOn = false
    }

    private fun loop(rec: AudioRecord) {
        val chunk = ShortArray(chosenRate / 8)        // ~125 ms
        val bytes = ByteBuffer.allocate(chunk.size * 2).order(ByteOrder.LITTLE_ENDIAN)
        try {
            rec.startRecording()
            while (running) {
                val n = rec.read(chunk, 0, chunk.size)
                if (n <= 0) continue
                bytes.clear()
                bytes.asShortBuffer().put(chunk, 0, n)
                link.sendAudio(bytes.array(), n * 2)
            }
        } catch (e: Exception) {
            Log.w(TAG, "audio loop ended: $e")
        } finally {
            try { rec.stop() } catch (_: Exception) {}
            rec.release()
        }
    }

    private fun pickSource(): Int {
        val am = ctx.getSystemService(AudioManager::class.java)
        val unprocessed = am?.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED) == "true"
        return if (unprocessed) MediaRecorder.AudioSource.UNPROCESSED else MediaRecorder.AudioSource.VOICE_RECOGNITION
    }

    companion object { const val TAG = "FlameAudio" }
}
