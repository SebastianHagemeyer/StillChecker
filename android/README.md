# FlameCam (native Android camera for the flame monitor)

Replaces the Safari camera page with a real Android app. Streams with the
**screen off** (camera foreground service, wake + Wi-Fi locks), finds the PC by
itself, reconnects forever, and reports phone health to the host.

```
FlameCam app --UDP 47820 broadcast--> lancam_host  (discovery: "where are you?")
FlameCam app --ws://pc:8080/ws------> lancam_host  JPEG frames + telemetry
FlameCam app --ws://pc:8080/audio---> lancam_host  8 kHz PCM (only if Audio is ticked)
lancam_host  --shared memory--------> readnew2.py  (monitor, unchanged workflow)
lancam_host  --Telegram-------------> PurpleVector bot  (alerts + 30 min reports)
FlameCam app --Telegram-------------> PurpleVector bot  (backup: "PC host is down")
```

## Install (once, phone on USB with USB debugging on)

    install.bat            (or: python install.py)

Builds the APK, installs it, grants permissions, exempts it from battery
optimisation, copies the bot token into the app's private storage, opens it.
Upgrades: run it again (`--build` to force a rebuild).

## Use

1. PC: `manager2.bat` -> Start (Telegram alerts box ticked).
2. Phone: FlameCam -> Start streaming. Aim using the preview, then press the
   power button. The notification shows it is still streaming.

Rotate 90 degrees fixes a sideways picture. If the PC is never found (router
blocks broadcast), type `PC-IP:8080` into the host box and Save.

## Alerts (Telegram, PurpleVector bot)

From the PC host (`--telegram`):
- stream online / STREAM LOST (no frames 30 s) / stream back
- FLAME ALARM (readnew2 no-change alarm) / alarm cleared
- status report every 30 min (fps, phone battery + temp + Wi-Fi, monitor, rattle, cook timer)
- host stopped

From the phone (backup, when the PC itself is down):
- cannot reach the PC host for 3 min / reconnected

Intentional stops (Stop on the PC or the phone) are announced as such, not as alarms.

## Build by hand

    set JAVA_HOME=C:\Program Files\Eclipse Adoptium\jdk-21.0.6.7-hotspot
    gradlew assembleDebug
