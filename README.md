# DroidFPS

Live performance monitor for Android phones over ADB. It shows a real-time web dashboard of what your phone is doing while you game, without root or anything installed on the phone, over a **USB cable or Wi-Fi**.

## What it shows

- **FPS** of the foreground app (games, video, UI), from SurfaceFlinger's frame present times
- **Average, 1% low and 0.1% low FPS** for the session, plus a rolling 30-second 1% low
- **Frame-time graph** (every frame, in ms), worst frame and stutter count
- **Temperatures**: CPU, GPU, SoC, skin, battery and every other sensor the thermal HAL exposes, plus Android's throttle status
- **CPU** clock and load per core
- **GPU** clock and load where the phone allows reading them; otherwise the GPU/CPU throttle (cooling) level from the thermal service
- **Battery** level, charge state, current (mA), voltage and power (W)
- **RAM** and the top processes by CPU
- **Record to CSV** for comparing sessions later

## Requirements

- Python 3.9+ (standard library only, no `pip install`)
- `adb` from [Android platform-tools](https://developer.android.com/tools/releases/platform-tools) in your PATH
- On the phone: **Developer options → USB debugging** on. Xiaomi/POCO phones also need **USB debugging (Security settings)** for some readings.

## Run

```bash
git clone https://github.com/Mpradeep-dev/droidfps
cd droidfps
python3 droidfps.py
```

The dashboard opens at <http://127.0.0.1:8765>. Pick your phone from the device list, open a game, and the numbers start moving.

Options: `--port 9000`, `--no-browser`, `--host 0.0.0.0` (to open the dashboard from another device on your LAN; there's no login, so only do this on a trusted network).

## Wi-Fi

Open **Wi-Fi / connect** in the dashboard. Phones with Wireless debugging on are found automatically (mDNS) with a one-click Connect button. Or:

1. **USB → Wi-Fi**: with the cable plugged in, switch adb to Wi-Fi (port 5555), then unplug.
2. **Connect by IP**: enter `phone-ip:port`.
3. **Pair (Android 11+, no cable)**: Developer options → Wireless debugging → *Pair device with pairing code*. Enter the pairing IP:port and code, then connect using the IP:port shown on the Wireless debugging screen.

Wi-Fi adds a little latency but doesn't change the FPS numbers: frame timestamps come from the phone's own clock.

## How it works

One persistent `adb shell` is polled every 0.5 s:

| Stat | Source |
|---|---|
| FPS / frame times | `dumpsys SurfaceFlinger --latency <layer>` (actual present timestamps) for the focused app's busiest layer |
| Temps / throttling | `dumpsys thermalservice` (falls back to `/sys/class/thermal`) |
| CPU | `/sys/devices/system/cpu/*/cpufreq`, `/proc/stat`, `top` |
| GPU | vendor sysfs nodes, when readable |
| Battery | `dumpsys battery` + the first readable `power_supply/*/current_now` |

**1% low** = the average FPS of the slowest 1% of frames, the same definition as PC tools like CapFrameX. 0.1% low is the same for the slowest 0.1%.

## Notes

- Polling costs a little CPU on the phone (a few dumpsys calls per second). For the most accurate thermal readings, unplug from charging, since charging heats the battery and changes throttling.
- Some phones block parts of sysfs from the `shell` user, so GPU or per-core load can show `--`. That's the phone's permissions, not a bug.
- Built around a MediaTek Dimensity / HyperOS phone. Other vendors may expose different sensor names.

## License

MIT
