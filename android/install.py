#!/usr/bin/env python3
"""
One-shot FlameCam installer: phone plugged in over USB (or `adb connect`ed).

    python install.py              build if needed, install, set up everything
    python install.py --build      force a fresh build first
    python install.py --serial X   pick a device when several are attached
    python install.py --telegram   also enable the phone's backup Telegram alert

What it does:
  1. builds app-debug.apk with the Gradle wrapper (if missing or --build)
  2. adb install -r (keeps the app's settings on upgrades)
  3. grants camera / mic / notification permissions (no prompts on the phone)
  4. exempts the app from battery optimisation + background restrictions,
     so Android never kills the stream while the screen is off
  5. Telegram backup alert: OFF by default (clears any stored token).
     With --telegram it copies the Empire bot token into the app's PRIVATE
     storage via run-as; never committed, never on shared storage
  6. launches the app
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = "com.purplevector.flamecam"
APK = os.path.join(HERE, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
JDK_CANDIDATES = [
    r"C:\Program Files\Eclipse Adoptium\jdk-21.0.6.7-hotspot",
    r"C:\Program Files\Android\Android Studio\jbr",
]


def find_adb():
    for base in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")):
        if base:
            p = os.path.join(base, "platform-tools", "adb.exe" if os.name == "nt" else "adb")
            if os.path.exists(p):
                return p
    return shutil.which("adb")


def build():
    env = dict(os.environ)
    for jdk in JDK_CANDIDATES:
        if os.path.exists(os.path.join(jdk, "bin")):
            env["JAVA_HOME"] = jdk
            break
    gradlew = os.path.join(HERE, "gradlew.bat" if os.name == "nt" else "gradlew")
    print("Building APK (first build takes a minute or two)...")
    subprocess.run([gradlew, "assembleDebug", "--console=plain", "-q"], cwd=HERE, env=env, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--serial")
    ap.add_argument("--telegram", action="store_true",
                    help="give the phone the bot token for its 'PC host is down' backup alert")
    a = ap.parse_args()

    adb = find_adb()
    if not adb:
        sys.exit("adb not found (expected in the Android SDK platform-tools).")
    base = [adb] + (["-s", a.serial] if a.serial else [])

    def sh(*args, check=False, **kw):
        return subprocess.run(base + list(args), capture_output=True, text=True, check=check, **kw)

    devs = [l.split()[0] for l in subprocess.run([adb, "devices"], capture_output=True, text=True)
            .stdout.splitlines()[1:] if l.strip().endswith("device")]
    if not devs:
        sys.exit("No phone found. Enable USB debugging, plug in, and accept the 'Allow USB debugging' prompt.")
    if len(devs) > 1 and not a.serial:
        sys.exit(f"Several devices attached ({', '.join(devs)}); pass --serial.")
    model = sh("shell", "getprop", "ro.product.model").stdout.strip()
    sdk = int(sh("shell", "getprop", "ro.build.version.sdk").stdout.strip() or "0")
    print(f"Phone: {model} (API {sdk})")

    if a.build or not os.path.exists(APK):
        build()

    print("Installing...")
    r = sh("install", "-r", APK)
    if "Success" not in r.stdout:
        sys.exit("Install failed:\n" + r.stdout + r.stderr)

    perms = ["android.permission.CAMERA", "android.permission.RECORD_AUDIO"]
    if sdk >= 33:
        perms.append("android.permission.POST_NOTIFICATIONS")
    for p in perms:
        sh("shell", "pm", "grant", PKG, p)
    print("Permissions granted.")

    sh("shell", "dumpsys", "deviceidle", "whitelist", "+" + PKG)
    sh("shell", "cmd", "appops", "set", PKG, "RUN_ANY_IN_BACKGROUND", "allow")
    sh("shell", "cmd", "appops", "set", PKG, "RUN_IN_BACKGROUND", "allow")
    print("Battery optimisation exemption set.")

    def provision(payload):
        cmd = f"run-as {PKG} sh -c 'mkdir -p files && cat > files/telegram.json'"
        r = subprocess.run(base + ["exec-in", cmd], input=payload, capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(base + ["shell", cmd], input=payload, capture_output=True, text=True)
        return r

    if a.telegram:
        sys.path.insert(0, os.path.dirname(HERE))
        try:
            from flame_telegram import load_credentials
            tok, chat, src = load_credentials()
        except Exception as exc:
            tok = chat = None
            print(f"(could not load Telegram credentials: {exc})")
        if tok:
            r = provision(json.dumps({"token": tok, "chat_id": chat}))
            if r.returncode == 0:
                print("Telegram backup alerts configured on the phone.")
            else:
                print("Could not copy Telegram credentials (phone backup alerts off):", r.stderr.strip())
        else:
            print("No Telegram credentials found; phone backup alerts off.")
    else:
        # Default: make sure the phone holds no bot token (clears an older install's).
        provision(json.dumps({"disable": True}))
        print("Telegram backup alerts off (run with --telegram to turn them on).")

    sh("shell", "am", "start", "-n", f"{PKG}/.MainActivity")
    print("\nDone. On the phone: tap Start streaming, then press the power button.")
    print("On the PC: run manager2.bat and press Start (the app finds the PC by itself).")


if __name__ == "__main__":
    main()
