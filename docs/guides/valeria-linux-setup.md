# Valeria - Linux setup

This guide is required for **`pymobiledevice3 valeria`** (iOS H.264
screen capture) on Linux. The distro `usbmuxd` package may not include
the `DEVICE_MODE` feature needed to enable QuickTime capture mode, so
we build upstream `usbmuxd` master from source.

## Why this is needed

The screen-capture path uses Apple's QuickTime alt-config protocol over
USB. On macOS the OS-level `usbmuxd` handles the lockdown handshake on
the device's USBMux interface (the one that's normally used for sync,
backups, etc.) - and the iDevice will not begin streaming H.264 until
that handshake has happened.

Distribution `usbmuxd` packages don't yet support driving a device into
QuickTime alt-config - the upstream feature for this (env var
`USBMUXD_DEFAULT_DEVICE_MODE`) was added to upstream master in 2022 but
predates the latest tagged release, so it hasn't shipped in any distro
package. We need to build upstream `usbmuxd` from source and run it
with that env var set.

## What this changes about your machine

- The distro `usbmuxd` is replaced with our fork under `/usr/local/`
  (the `dynamic-config-switch` branch on top of upstream master).
- Any iDevice you plug in starts in QuickTime mode (USB configuration
  5). Each `pmd3 valeria` or `pmd3 screen-mirror` invocation toggles
  it between config 5 (capture active) and config 4 (idle, USBMux only)
  for the duration of that capture, then back. Mirrors macOS's
  `iOSScreenCaptureAssistant` behaviour.
- Standard things - file transfer, lockdown queries, app installation,
  iDevice-as-network-modem tethering - keep working. They go through
  the same `/var/run/usbmuxd` socket and the same iface 1 USBMux
  endpoints, which are present in both configurations.

If you need to revert later, see [Reverting](#reverting) below.

## Build and install

Tested on Ubuntu 22.04. The build chain is **libplist → libimobiledevice-glue
→ usbmuxd**. Ubuntu's `libplist-dev` is too old (2.2.0; we need 2.3+),
which is why the chain starts there.

```bash
# 1. Build dependencies
sudo apt-get update && sudo apt-get install -y \
  autoconf automake libtool pkg-config build-essential git \
  libusb-1.0-0-dev

# 2. Remove distro usbmuxd (it'll conflict with upstream master)
sudo systemctl stop usbmuxd 2>/dev/null || true
sudo apt-get remove -y usbmuxd

# 3. Build the libimobiledevice stack from upstream
cd /tmp
for proj in libplist libimobiledevice-glue; do
  git clone --depth 1 https://github.com/libimobiledevice/$proj
  (cd $proj && ./autogen.sh && ./configure && make -j && sudo make install)
done
sudo ldconfig

# 3a. usbmuxd from our fork's dynamic-config-switch branch (adds the
# SetActiveConfiguration IPC that pmd3's per-capture config switch
# uses; pending upstream PR).
git clone -b dynamic-config-switch \
  https://github.com/renegadelink/usbmuxd
(cd usbmuxd && ./autogen.sh && ./configure --without-preflight \
   && make -j && sudo make install)

# 4. Configure systemd to start usbmuxd with QuickTime mode enabled
sudo mkdir -p /etc/systemd/system/usbmuxd.service.d
sudo tee /etc/systemd/system/usbmuxd.service.d/override.conf >/dev/null <<'EOF'
[Service]
Environment=USBMUXD_DEFAULT_DEVICE_MODE=2
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now usbmuxd
```

## Verify

```bash
# Should report a recent build SHA, not v1.0.x:
/usr/local/sbin/usbmuxd --version

# Should list "USBMUXD_DEFAULT_DEVICE_MODE":
strings /usr/local/sbin/usbmuxd | grep DEVICE_MODE

# Plug an iDevice, then confirm the daemon picked it up
# and switched to QuickTime mode (configuration value 5):
pymobiledevice3 usbmux list
cat /sys/bus/usb/devices/*/bConfigurationValue \
    /sys/bus/usb/devices/*/idVendor 2>/dev/null \
  | paste - - | awk '$2 == "05ac" {print "active config:", $1}'
```

If the active config is `5`, you're set.

## Run

```bash
# Capture raw H.264 to a file
pymobiledevice3 valeria -o capture.h264 --duration 10
```

The iDevice screen must be **on** (not asleep) for frames to actually
flow. The protocol completes its handshake either way, but a sleeping
display produces no encoder output.

## Troubleshooting

**`Valeria: capture started (0x0)` and an empty output file** - the
handshake reached the protocol thread but the device never sent a video
clock setup. Most often the iDevice screen is asleep. Tap the iDevice
to wake it and re-run.

**`failed to start capture: [Errno 16] Resource busy`** - another
process (often a stale `pmd3 valeria` from a prior run, or a separate
`usbmuxd` instance) is still holding the USBMux interface. Check
`ps aux | grep usbmuxd` - there should be exactly one instance.

**Multiple captures in a row** - each `pymobiledevice3 valeria`
invocation asks `usbmuxd` to switch the iDevice into the QT-enabled USB
configuration for the duration of the capture, and back to a QT-free
configuration when done (mirroring macOS's `iOSScreenCaptureAssistant`
pattern). This means each invocation is self-contained - no daemon, no
kept-alive USB iface claim - and back-to-back captures across long
idles work cleanly.

**Other `pymobiledevice3` commands stop working after install** -
double-check that the distro `usbmuxd` was removed cleanly. `which usbmuxd`
should return `/usr/local/sbin/usbmuxd`. `systemctl status usbmuxd`
should show the upstream binary.

## Reverting

To go back to the distro setup (no QuickTime alt-config):

```bash
sudo systemctl stop usbmuxd
sudo rm /etc/systemd/system/usbmuxd.service.d/override.conf
sudo apt-get install -y --reinstall usbmuxd
sudo systemctl daemon-reload
sudo systemctl restart usbmuxd
```

The upstream `libplist` / `libimobiledevice-glue` libraries you built
into `/usr/local/` are harmless to leave installed. Remove them too
with `(cd /tmp/<lib> && sudo make uninstall)` if you want a perfectly
clean revert.

## Background - what `USBMUXD_DEFAULT_DEVICE_MODE=2` does

Upstream `usbmuxd` since commit `6d0183dd` (2022-12-22) supports the
env var `USBMUXD_DEFAULT_DEVICE_MODE`. The values map to USB
configuration numbers Apple defines on iDevices:

| Mode | USB config | Description |
|------|------------|-------------|
| 1 | 4 | Default - usbmuxd only (sync, lockdown, tethering) |
| 2 | 5 | QuickTime alt-config - adds the AV streaming interface |
| 3 | 6 | Other internal mode |
| 4 | 7 | Other internal mode |
| 5 | 8 | Other internal mode |

Setting it to `2` tells `usbmuxd` to put the iDevice into a USB layout
that exposes BOTH a QT-capable configuration (5) and a USBMux-only
configuration (4). The `SetActiveConfiguration` IPC on our fork's
`dynamic-config-switch` branch then lets `pymobiledevice3 valeria`
toggle between them per-capture - matching macOS's
`iOSScreenCaptureAssistant`, which also keeps the iDevice in the
non-QT config when idle and switches into the QT config only while a
capture is active.

If you don't need that idle-when-not-capturing behaviour (e.g. a
multi-device farm where you'd rather avoid the per-capture lockdown
disconnect), skip our fork and use stock upstream `usbmuxd` with the
same `USBMUXD_DEFAULT_DEVICE_MODE=2`. The iDevice stays in config 5
permanently; the USBMux iface is always present so lockdown ops keep
working unaffected.
