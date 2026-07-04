#!/usr/bin/env python3
"""
lancam_host.py - Phone camera -> LAN -> shared-memory bridge for the
Raspberry.Ninja frame monitor (readnew2.py).

Flow:

    Phone Safari  --https-->  this host          (serves a camera page)
    Phone camera  --wss--->   this host          (full JPEG per frame)
                              this host  -->  'psm_raspininja_streamid' shm
                                          -->  readnew2.py monitor (unchanged)

No VDO.Ninja, no internet. Each frame is a self-contained JPEG, so the picture
can never tear or go green: there are no reference frames to lose. If the
phone's Wi-Fi is slow the frame rate drops, but every delivered frame is a
whole, full-resolution image. That is exactly the "fps can drop, frames stay
clean" behaviour you asked for, as a property of the transport.

Usage:
    1. Run this with the SAME Python you use for readnew2.py (your venv):
           python lancam_host.py
    2. Run the monitor:
           python readnew2.py
    3. On the phone, open the printed  https://<lan-ip>:8443  in Safari.
       Accept the certificate warning once (self-signed), tap "Start camera",
       and point the phone at what you're watching.

Do NOT run publish.py / manager.py at the same time: this host owns the same
shared memory segment they use.

Requirements (all already in the project venv): aiohttp, opencv-python, numpy,
cryptography.
"""

import argparse
import asyncio
import datetime
import ipaddress
import json
import os
import socket
import ssl
import sys
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import shared_memory

try:
    from multiprocessing.resource_tracker import unregister as _shm_unregister
except Exception:
    _shm_unregister = None

import numpy as np
import cv2
from aiohttp import web, WSMsgType


# ------------------------------------------------------------------ config ---
SHM_NAME = "psm_raspininja_streamid"     # must match readnew2.py's default
MAX_W, MAX_H = 1920, 1080                 # largest frame we size the buffer for
SHM_SIZE = MAX_W * MAX_H * 3 + 5          # BGR pixels + 5-byte header
MAX_PIXELS = MAX_W * MAX_H
DEFAULT_PORT = 8443
CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lancam_certs")


# ------------------------------------------------------------ shared memory ---
class FrameWriter:
    """Writes decoded BGR frames into the shared memory readnew2.py reads.

    Layout (identical to publish.py's --framebuffer new_sample):
        byte 0: width  // 255
        byte 1: width  %  255
        byte 2: height // 255
        byte 3: height %  255
        byte 4: frame counter (% 255)
        byte 5..: width*height*3 bytes of BGR
    Pixels are written first and the counter byte last, so a reader that sees a
    new counter is guaranteed the pixels for that frame are already in place.
    """

    def __init__(self):
        self.shm = self._create_shm()
        self.buf = np.ndarray(self.shm.size, dtype=np.uint8, buffer=self.shm.buf)
        self.counter = 0
        self.frames = 0

    @staticmethod
    def _create_shm():
        # Drop any stale segment first (mirrors manager.py._clear_stale_shm).
        try:
            old = shared_memory.SharedMemory(name=SHM_NAME)
            try:
                if _shm_unregister is not None:
                    try:
                        _shm_unregister(old._name, "shared_memory")
                    except Exception:
                        pass
                old.close()
                old.unlink()
            except Exception:
                pass
        except FileNotFoundError:
            pass
        except Exception:
            pass

        try:
            return shared_memory.SharedMemory(create=True, size=SHM_SIZE, name=SHM_NAME)
        except FileExistsError:
            # Windows can keep a segment alive while a handle is open; reuse it
            # if it is at least as large as we need.
            shm = shared_memory.SharedMemory(name=SHM_NAME)
            if shm.size < SHM_SIZE:
                shm.close()
                raise RuntimeError(
                    "A '%s' shared memory segment already exists and is too "
                    "small. Stop publish.py / manager.py and try again." % SHM_NAME
                )
            return shm

    def write(self, jpeg_bytes):
        """Decode a JPEG and publish it. Returns (w, h) or None on failure.

        Runs on a worker thread so JPEG decode never blocks the event loop.
        """
        arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)  # BGR, or None if not a JPEG
        if img is None:
            return None

        h, w = img.shape[:2]
        # Guard against anything bigger than the buffer (e.g. a 1080p+ phone).
        if w * h > MAX_PIXELS:
            scale = (MAX_PIXELS / float(w * h)) ** 0.5
            w, h = max(2, int(w * scale)), max(2, int(h * scale))
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            h, w = img.shape[:2]

        img = np.ascontiguousarray(img)
        n = w * h * 3

        self.buf[5:5 + n] = img.reshape(-1)      # pixels first
        self.buf[0] = w // 255
        self.buf[1] = w % 255
        self.buf[2] = h // 255
        self.buf[3] = h % 255
        self.counter = (self.counter + 1) % 255
        self.buf[4] = self.counter               # counter last -> signals "new"
        self.frames += 1
        return (w, h)

    def close(self):
        try:
            self.shm.close()
        except Exception:
            pass
        try:
            self.shm.unlink()
        except Exception:
            pass


# ------------------------------------------------------------------ audio ---
# The phone streams microphone audio (mono PCM); we detect a rattle (e.g. a
# pressure-cooker weight rocking) by comparing recent energy to a slow-adapting
# quiet baseline. A faint rattle sits ~2-3x above the noise floor even when its
# absolute level is tiny, so relative energy is the reliable test, not loudness.
# The result is written to rattle_cadence.json for the UI to pick up.
AUDIO_RING_SECONDS = 60
RATTLE_SNR = 1.4          # recent energy vs quiet baseline to call it "rattling"
RATTLE_ABS_MIN = 1.5e-4   # tiny absolute floor so dead silence never trips it

AUDIO_WORKLET_JS = r"""
class PCMDown extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.factor = Math.max(1, o.factor || 6);
    this.acc = 0; this.count = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) {
      const out = [];
      for (let i = 0; i < ch.length; i++) {
        this.acc += ch[i]; this.count++;
        if (this.count >= this.factor) { out.push(this.acc / this.count); this.acc = 0; this.count = 0; }
      }
      if (out.length) {
        const b = new Int16Array(out.length);
        for (let i = 0; i < out.length; i++) {
          let s = Math.max(-1, Math.min(1, out[i]));
          b[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage(b.buffer, [b.buffer]);
      }
    }
    return true;
  }
}
registerProcessor('pcm-down', PCMDown);
"""


def audio_features(samples, sr):
    """Envelope features from a mono PCM window: current activity level, this
    window's quiet baseline, and a (fuzzy) jiggle cadence. Returns None until
    there is at least ~1 s of audio. The rattling decision is made by AudioSink,
    which tracks the baseline across time."""
    x = samples.astype(np.float32) / 32768.0
    if x.size < sr:
        return None
    hop = max(1, sr // 40)                       # ~40 Hz envelope
    k = (x.size // hop) * hop
    env = np.sqrt(np.mean(x[:k].reshape(-1, hop) ** 2, axis=1) + 1e-12)
    esr = sr / hop
    window_floor = float(np.percentile(env, 20))          # quiet-gap baseline
    recent = float(np.mean(env[-max(1, int(esr * 4)):]))  # current activity
    seg = env[-int(esr * 6):] if env.size > int(esr * 6) else env
    e = seg - seg.mean()
    per_min, conf = 0.0, 0.0
    if e.size > 16:
        ac = np.correlate(e, e, "full")[e.size - 1:]
        lo, hi = max(1, int(esr * 0.15)), min(int(esr * 3.0), ac.size - 1)  # 0.33..6.7 Hz
        if hi > lo + 1 and ac[0] > 0:
            lag = lo + int(np.argmax(ac[lo:hi]))
            conf = float(ac[lag] / ac[0])
            per_min = round(esr / lag * 60.0, 1)
    return {"recent": recent, "window_floor": window_floor,
            "per_min": per_min, "confidence": round(conf, 3)}


class WavRecorder:
    """Minimal incremental mono 16-bit WAV writer. RIFF sizes are rewritten on
    each flush so the file stays playable even if the host is killed mid-run."""

    def __init__(self, path, sample_rate):
        import struct
        self._struct = struct
        self.path = path
        self.sr = int(sample_rate)
        self.data_bytes = 0
        self.f = open(path, "wb")
        self._write_header()

    def _write_header(self):
        st = self._struct
        self.f.seek(0)
        self.f.write(b"RIFF")
        self.f.write(st.pack("<I", 36 + self.data_bytes))
        self.f.write(b"WAVEfmt ")
        self.f.write(st.pack("<IHHIIHH", 16, 1, 1, self.sr, self.sr * 2, 2, 16))
        self.f.write(b"data")
        self.f.write(st.pack("<I", self.data_bytes))

    def write(self, pcm_int16):
        self.f.seek(0, 2)
        b = pcm_int16.astype("<i2").tobytes()
        self.f.write(b)
        self.data_bytes += len(b)

    def flush(self):
        try:
            self._write_header()
            self.f.seek(0, 2)
            self.f.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.flush()
            self.f.close()
        except Exception:
            pass


class AudioSink:
    """Receives mono PCM chunks from the phone, keeps a rolling buffer, and
    estimates the rattle cadence about once a second. The latest result is kept
    in .latest and written to rattle_cadence.json for other tools to read.
    """

    def __init__(self, out_dir, record=False):
        self.sr = 8000
        self.chunks = deque()
        self.buffered = 0
        self.since_analysis = 0
        self.latest = {"rattling": False, "reason": "no audio yet"}
        self.out_dir = out_dir
        self.json_path = os.path.join(out_dir, "rattle_cadence.json")
        self.frames = 0
        self.record = record
        self.recorder = None
        self.slow_floor = None

    def set_format(self, sample_rate, channels=1):
        try:
            sr = int(sample_rate)
            if 2000 <= sr <= 96000:
                self.sr = sr
        except Exception:
            pass

    def feed(self, pcm_int16):
        if pcm_int16.size == 0:
            return
        if self.record:
            if self.recorder is None:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                path = os.path.join(self.out_dir, f"rattle_audio_{ts}.wav")
                try:
                    self.recorder = WavRecorder(path, self.sr)
                    print(f"[audio] recording to {path}", flush=True)
                except Exception as exc:
                    print(f"[audio] could not start recording: {exc}", flush=True)
                    self.record = False
            if self.recorder is not None:
                self.recorder.write(pcm_int16)
        self.chunks.append(pcm_int16)
        self.buffered += pcm_int16.size
        self.since_analysis += pcm_int16.size
        self.frames += 1
        cap = AUDIO_RING_SECONDS * self.sr
        while self.buffered > cap and len(self.chunks) > 1:
            self.buffered -= self.chunks.popleft().size
        if self.since_analysis >= self.sr:        # roughly once per second
            self.since_analysis = 0
            self._analyse()

    def _analyse(self):
        if self.recorder is not None:
            self.recorder.flush()
        try:
            if self.buffered < self.sr * 6:
                self.latest = {"rattling": False, "reason": "warming up"}
                self._write_latest()
                return
            samples = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.int16)
            feats = audio_features(samples, self.sr)
            if feats is None:
                self.latest = {"rattling": False, "reason": "warming up"}
                self._write_latest()
                return
            cand = feats["window_floor"]
            if self.slow_floor is None:
                self.slow_floor = cand
            elif cand < self.slow_floor:
                self.slow_floor = 0.5 * self.slow_floor + 0.5 * cand    # follow quiet down
            else:
                self.slow_floor = 0.98 * self.slow_floor + 0.02 * cand  # rise slowly
            snr = feats["recent"] / (self.slow_floor + 1e-6)
            rattling = bool(snr >= RATTLE_SNR and feats["recent"] > RATTLE_ABS_MIN)
            self.latest = {
                "rattling": rattling,
                "snr": round(snr, 2),
                "per_min": feats["per_min"],
                "confidence": feats["confidence"],
                "level": round(feats["recent"], 5),
                "floor": round(self.slow_floor, 5),
                "sample_rate": int(self.sr),
                "utc": datetime.datetime.utcnow().isoformat() + "Z",
            }
            self._write_latest()
            if rattling:
                print(f"[audio] rattling  SNR {self.latest['snr']}  ~{self.latest['per_min']}/min", flush=True)
        except Exception as exc:
            print(f"[audio] analysis error: {exc}", flush=True)

    def _write_latest(self):
        try:
            with open(self.json_path, "w") as f:
                json.dump(self.latest, f)
        except Exception:
            pass

    def close(self):
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None


# --------------------------------------------------------------- networking ---
def primary_lan_ip():
    """Best-effort outbound LAN IPv4 (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def all_local_ipv4():
    ips = {primary_lan_ip()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    ips.discard("0.0.0.0")
    return sorted(i for i in ips if i)


# ---------------------------------------------------------------------- TLS ---
def get_or_create_cert():
    """Return (certfile, keyfile), generating a self-signed pair if needed.

    Validity is kept under iOS's 398-day server-cert limit so Safari will let
    you proceed past the self-signed warning.
    """
    os.makedirs(CERT_DIR, exist_ok=True)
    certfile = os.path.join(CERT_DIR, "cert.pem")
    keyfile = os.path.join(CERT_DIR, "key.pem")
    if os.path.exists(certfile) and os.path.exists(keyfile):
        return certfile, keyfile

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    san = [x509.DNSName("localhost")]
    for ip in ["127.0.0.1"] + all_local_ipv4():
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except Exception:
            pass

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lancam-host")])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(keyfile, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return certfile, keyfile


# -------------------------------------------------------------- the web page ---
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>LAN Cam</title>
<link rel="icon" href="/favicon.svg">
<style>
  html,body{margin:0;height:100%;background:#000;color:#eee;font-family:-apple-system,system-ui,sans-serif;overflow:hidden}
  #v{position:fixed;inset:0;width:100%;height:100%;object-fit:contain;background:#000}
  #ui{position:fixed;left:0;right:0;bottom:0;padding:16px;display:flex;flex-direction:column;gap:10px;align-items:center;
      background:linear-gradient(to top,rgba(0,0,0,.75),rgba(0,0,0,0))}
  #start{font-size:22px;padding:16px 28px;border:0;border-radius:14px;background:#e5484d;color:#fff;font-weight:600}
  #start:active{background:#c93b3f}
  #status{font-size:15px;opacity:.9;text-align:center}
  #err{font-size:14px;color:#ff9a9a;text-align:center;max-width:90vw}
  .badge{position:fixed;top:10px;left:10px;font-size:12px;background:rgba(0,0,0,.5);padding:4px 8px;border-radius:8px}
  #blackout{position:fixed;inset:0;background:#000;z-index:9999;display:none;align-items:center;justify-content:center}
  #blackout.on{display:flex}
  #blackout .hint{color:rgba(255,255,255,.14);font-size:15px;line-height:1.6;text-align:center;padding:0 28px}
</style>
</head>
<body>
  <video id="v" autoplay muted playsinline webkit-playsinline></video>
  <div class="badge" id="badge">LAN Cam</div>
  <div id="ui">
    <div id="err"></div>
    <button id="start">&#9654;&nbsp; Start camera</button>
    <div id="status">Tap start, then point the phone at what you're watching.</div>
  </div>
  <div id="blackout"><div class="hint">Screen dimmed to save battery.<br>Streaming is still running.<br>Tap anywhere to show the picture.</div></div>
<script>
const CFG = __CFG__;
const v = document.getElementById('v');
const c = document.createElement('canvas');
const ctx = c.getContext('2d');
const startBtn = document.getElementById('start');
const statusEl = document.getElementById('status');
const errEl = document.getElementById('err');
const blackout = document.getElementById('blackout');
let ws = null, timer = null, stream = null, wakeLock = null;
let sent = 0, lastReport = performance.now();
let idleTimer = null, streaming = false;

// After CFG.idleMs of no touch, cover the preview with black (OLED pixels off)
// to save battery. Capture + upload keep running underneath. Tap to restore.
function goDark(){ if (streaming && CFG.idleMs > 0) blackout.classList.add('on'); }
function resetIdle(){
  if (idleTimer) clearTimeout(idleTimer);
  if (streaming && CFG.idleMs > 0) idleTimer = setTimeout(goDark, CFG.idleMs);
}
function onActivity(){
  if (blackout.classList.contains('on')) blackout.classList.remove('on');
  resetIdle();
}
['touchstart','pointerdown','keydown'].forEach(function(ev){
  document.addEventListener(ev, onActivity, {passive:true});
});

const setStatus = t => statusEl.textContent = t;
const setErr = t => errEl.textContent = t;

function wsURL(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return proto + '://' + location.host + '/ws';
}

function connectWS(){
  ws = new WebSocket(wsURL());
  ws.binaryType = 'arraybuffer';
  ws.onopen  = () => setStatus('streaming...');
  ws.onclose = () => { setStatus('reconnecting...'); ws = null; setTimeout(connectWS, 1000); };
  ws.onerror = () => {};
}

async function acquireWakeLock(){
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch(e){}
}

async function start(){
  setErr('');
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    setErr('This browser blocked camera access. Make sure the address is https://');
    return;
  }
  try{
    stream = await navigator.mediaDevices.getUserMedia({
      audio:false,
      video:{ facingMode:{ ideal:'environment' },
              width:{ ideal:1280 }, height:{ ideal:720 } }
    });
  }catch(e){
    setErr('Camera error: ' + e.name + ' - ' + e.message);
    return;
  }
  v.srcObject = stream;
  try { await v.play(); } catch(e){}
  startBtn.style.display = 'none';
  acquireWakeLock();
  connectWS();
  const interval = Math.max(33, Math.round(1000 / CFG.fps));
  timer = setInterval(grab, interval);
  streaming = true;
  resetIdle();
  if (CFG.audio) startAudio();
}

function grab(){
  if (!ws || ws.readyState !== 1) return;
  const w = v.videoWidth, h = v.videoHeight;
  if (!w || !h) return;
  // Backpressure: if the socket is backed up, skip this frame. The frame rate
  // dips on a slow link but each frame that goes out is complete.
  if (ws.bufferedAmount > 2 * 1024 * 1024) return;
  if (c.width !== w)  c.width = w;
  if (c.height !== h) c.height = h;
  ctx.drawImage(v, 0, 0, w, h);
  c.toBlob(b => {
    if (b && ws && ws.readyState === 1){ ws.send(b); sent++; }
  }, 'image/jpeg', CFG.quality);

  const now = performance.now();
  if (now - lastReport > 1000){
    const fps = Math.round(sent * 1000 / (now - lastReport));
    sent = 0; lastReport = now;
    setStatus('streaming ' + w + '×' + h + '  ~' + fps + ' fps');
  }
}

// ---- optional microphone -> PCM -> /audio (for rattle-cadence detection) ----
let audioWS = null, audioCtx = null;
function wsAudioURL(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  return proto + '://' + location.host + '/audio';
}
async function startAudio(){
  let astream;
  try {
    astream = await navigator.mediaDevices.getUserMedia({
      audio:{ echoCancellation:false, noiseSuppression:false, autoGainControl:false },
      video:false
    });
  } catch(e){ setStatus('mic off (' + e.name + ')'); return; }
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return;
  audioCtx = new AC();
  try { await audioCtx.resume(); } catch(e){}
  const nativeSR = audioCtx.sampleRate || 48000;
  const factor = Math.max(1, Math.round(nativeSR / 8000));
  const outSR = Math.round(nativeSR / factor);
  audioWS = new WebSocket(wsAudioURL());
  audioWS.binaryType = 'arraybuffer';
  audioWS.onopen = () => audioWS.send(JSON.stringify({ type:'hello', sampleRate: outSR, channels: 1 }));
  audioWS.onclose = () => { audioWS = null; };
  const srcNode = audioCtx.createMediaStreamSource(astream);
  try {
    await audioCtx.audioWorklet.addModule('/audio-worklet.js');
    const node = new AudioWorkletNode(audioCtx, 'pcm-down', { processorOptions:{ factor: factor } });
    node.port.onmessage = (ev) => { if (audioWS && audioWS.readyState === 1) audioWS.send(ev.data); };
    srcNode.connect(node);
    const mute = audioCtx.createGain(); mute.gain.value = 0;
    node.connect(mute); mute.connect(audioCtx.destination);
  } catch(e){
    // Fallback for browsers without AudioWorklet
    const sp = audioCtx.createScriptProcessor(4096, 1, 1);
    let acc = 0, cnt = 0, out = [];
    sp.onaudioprocess = (ev) => {
      const chd = ev.inputBuffer.getChannelData(0);
      for (let i = 0; i < chd.length; i++){ acc += chd[i]; cnt++; if (cnt >= factor){ out.push(acc/cnt); acc=0; cnt=0; } }
      if (out.length >= 256 && audioWS && audioWS.readyState === 1){
        const b = new Int16Array(out.length);
        for (let i = 0; i < out.length; i++){ let s = Math.max(-1, Math.min(1, out[i])); b[i] = s<0 ? s*0x8000 : s*0x7fff; }
        audioWS.send(b.buffer); out = [];
      }
    };
    srcNode.connect(sp);
    const mute = audioCtx.createGain(); mute.gain.value = 0;
    sp.connect(mute); mute.connect(audioCtx.destination);
  }
}

startBtn.addEventListener('click', start);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && stream) acquireWakeLock();
});
</script>
</body>
</html>
"""


async def handle_index(request):
    cfg = request.app["cfg"]
    html = INDEX_HTML.replace("__CFG__", json.dumps(cfg))
    return web.Response(text=html, content_type="text/html")


async def handle_ws(request):
    ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    writer = request.app["writer"]
    pool = request.app["pool"]
    loop = asyncio.get_event_loop()
    peer = request.remote
    print(f"[lancam] phone connected: {peer}", flush=True)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                # Await the decode+write so TCP flow control throttles the phone
                # instead of letting a backlog build up here.
                await loop.run_in_executor(pool, writer.write, msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        print(f"[lancam] phone disconnected: {peer} "
              f"(total frames written: {writer.frames})", flush=True)
    return ws


async def handle_audio_ws(request):
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)
    sink = request.app.get("audio")
    peer = request.remote
    print(f"[audio] mic connected: {peer}", flush=True)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if sink is not None:
                    sink.feed(np.frombuffer(msg.data, dtype=np.int16))
            elif msg.type == WSMsgType.TEXT:
                try:
                    info = json.loads(msg.data)
                except Exception:
                    info = None
                if isinstance(info, dict) and info.get("type") == "hello" and sink is not None:
                    sink.set_format(info.get("sampleRate", 8000), info.get("channels", 1))
                    print(f"[audio] format: {sink.sr} Hz mono", flush=True)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        print(f"[audio] mic disconnected: {peer}", flush=True)
    return ws


async def handle_worklet(request):
    return web.Response(text=AUDIO_WORKLET_JS, content_type="application/javascript")


async def handle_favicon(request):
    svg = request.app.get("favicon_svg") or ""
    if not svg:
        return web.Response(status=404)
    return web.Response(text=svg, content_type="image/svg+xml")


def build_app(fps, quality, idle_ms, audio=False):
    app = web.Application()
    app["writer"] = FrameWriter()
    app["pool"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frame")
    app["cfg"] = {"fps": fps, "quality": quality, "idleMs": idle_ms, "audio": bool(audio)}
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/favicon.svg", handle_favicon)

    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "assets", "flame.svg"), "r", encoding="utf-8") as f:
            app["favicon_svg"] = f.read()
    except Exception:
        app["favicon_svg"] = ""

    if audio:
        app["audio"] = AudioSink(here, record=True)
        app.router.add_get("/audio", handle_audio_ws)
        app.router.add_get("/audio-worklet.js", handle_worklet)

    async def _on_cleanup(app):
        app["pool"].shutdown(wait=False)
        app["writer"].close()
        if app.get("audio") is not None:
            app["audio"].close()

    app.on_cleanup.append(_on_cleanup)
    return app


def main():
    ap = argparse.ArgumentParser(description="Phone-camera to shared-memory LAN host.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTPS port (default 8443)")
    ap.add_argument("--fps", type=int, default=10, help="target capture fps on the phone (default 10)")
    ap.add_argument("--quality", type=float, default=0.8, help="JPEG quality 0..1 (default 0.8)")
    ap.add_argument("--idle-seconds", type=int, default=30,
                    help="black out the phone screen after N seconds with no touch to save "
                         "battery; streaming keeps running. 0 disables (default 30)")
    ap.add_argument("--audio", action="store_true",
                    help="also accept phone microphone audio and estimate rattle cadence "
                         "(writes rattle_cadence.json); off by default")
    args = ap.parse_args()

    certfile, keyfile = get_or_create_cert()
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile, keyfile)

    app = build_app(args.fps, max(0.1, min(1.0, args.quality)),
                    max(0, args.idle_seconds) * 1000, audio=args.audio)

    ips = all_local_ipv4()
    print("=" * 60, flush=True)
    print(" LAN Cam host ready. Open ONE of these in Safari on the phone:", flush=True)
    for ip in ips:
        print(f"     https://{ip}:{args.port}", flush=True)
    print("", flush=True)
    print(" - Accept the self-signed certificate warning once.", flush=True)
    print(" - Tap 'Start camera' and allow camera access.", flush=True)
    print(" - Then run the monitor:  python readnew2.py", flush=True)
    print("   (do not run publish.py/manager.py at the same time)", flush=True)
    if args.audio:
        print(" - Audio ON: phone mic -> rattle_cadence.json + rattle_audio_*.wav recording", flush=True)
    print("=" * 60, flush=True)

    try:
        web.run_app(app, host="0.0.0.0", port=args.port, ssl_context=ssl_ctx, print=None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
