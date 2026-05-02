"""Talks to our ``libimobiledevice/usbmuxd`` fork (the
``dynamic-config-switch`` branch at
https://github.com/renegadelink/usbmuxd, which adds a
``SetActiveConfiguration`` plist IPC) to switch the iDevice's active
USB configuration around each screen capture.

Apple's iOSScreenCaptureAssistant on macOS uses the same pattern - the
iDevice sits in a non-QT configuration when idle and is switched into a
QT-enabled config only while a capture is active. Without this dance the
QT pipeline drifts into a wedged state during long idles.

Required usbmuxd setup: the fork branch above + ``USBMUXD_DEFAULT_DEVICE_MODE=2``
(so the iDevice exposes both the QT-enabled config and a QT-free config
to toggle between).
"""
from __future__ import annotations

import asyncio
import logging
import plistlib
import socket
import struct
import threading
from contextlib import contextmanager
from typing import Iterator

from pymobiledevice3.lockdown import create_using_usbmux

logger = logging.getLogger(__name__)

USBMUXD_SOCK = "/var/run/usbmuxd"
PLIST_MESSAGE = 8

QT_SUBCLASS = 0x2A
USBMUX_SUBCLASS = 0xFE

_locks: dict = {}
_locks_guard = threading.Lock()


def _get_udid_lock(udid: str) -> threading.Lock:
    with _locks_guard:
        if udid not in _locks:
            _locks[udid] = threading.Lock()
        return _locks[udid]


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("usbmuxd closed during reply")
        buf += chunk
    return buf


def _send_plist(d: dict, sock_path: str = USBMUXD_SOCK) -> dict:
    payload = plistlib.dumps(d)
    header = struct.pack("<IIII", 16 + len(payload), 1, PLIST_MESSAGE, 1)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15)
    try:
        s.connect(sock_path)
        s.sendall(header + payload)
        rh = _recv_exact(s, 16)
        length, _ver, msg, _tag = struct.unpack("<IIII", rh)
        data = _recv_exact(s, length - 16) if length > 16 else b""
        if msg != PLIST_MESSAGE:
            raise IOError(f"unexpected reply message type {msg}")
        return plistlib.loads(data)
    finally:
        s.close()


def set_active_configuration(udid: str, config: int) -> None:
    """Ask usbmuxd to switch the iDevice's active USB configuration."""
    reply = _send_plist({
        "MessageType": "SetActiveConfiguration",
        "ProgName": "pymobiledevice3-valeria",
        "ClientVersionString": "1.0",
        "SerialNumber": udid,
        "Configuration": config,
    })
    err = reply.get("Number", -1)
    if err != 0:
        raise RuntimeError(
            f"SetActiveConfiguration failed (number={err}). "
            f"Make sure usbmuxd is the patched build that supports this IPC."
        )
    logger.debug("usbmuxd switched %s to config %d", udid[:8], config)


def find_idevices(udid: str | None = None) -> list:
    """Return all attached iDevices matching *udid* (or all if None).

    Strips embedded NULs and pairing dashes from the USB serial for
    comparison, so callers can pass UDIDs in either form."""
    import usb.core
    out = []
    for d in usb.core.find(find_all=True, idVendor=0x05ac):
        try:
            s = d.serial_number
            if s is None:
                continue
            s_clean = s.replace("\x00", "").replace("-", "")
            if udid is None or s_clean == udid.replace("-", ""):
                out.append(d)
        except Exception:
            pass
    return out


def _discover_configs(udid: str) -> tuple:
    """Return ``(qt_cfg, idle_cfg)`` for *udid*.

    Picks the lowest-numbered USBMux-only config as idle (deterministic
    across mode-2/3/4 layouts that may expose multiple non-QT options)."""
    matches = find_idevices(udid)
    if not matches:
        raise RuntimeError(f"no iDevice with udid {udid[:8]}")
    dev = matches[0]
    qt_cfgs = []
    idle_cfgs = []
    for cfg in dev:
        has_qt = any(i.bInterfaceSubClass == QT_SUBCLASS
                     for i in cfg if i.bInterfaceClass == 0xFF)
        has_mux = any(i.bInterfaceSubClass == USBMUX_SUBCLASS
                      for i in cfg if i.bInterfaceClass == 0xFF)
        if has_qt and has_mux:
            qt_cfgs.append(cfg.bConfigurationValue)
        elif has_mux:
            idle_cfgs.append(cfg.bConfigurationValue)
    if not qt_cfgs:
        raise RuntimeError(
            "no QT-capable config (need usbmuxd-master with "
            "USBMUXD_DEFAULT_DEVICE_MODE=2 to expose mode-2 layout)"
        )
    if not idle_cfgs:
        raise RuntimeError(
            "no QT-free USBMux config available; iDevice must be in mode 2+"
        )
    return min(qt_cfgs), min(idle_cfgs)


def _poke_lockdown(udid: str) -> None:
    """Open and immediately close a lockdown session to trigger iDevice
    QT init (mirrors macOS Apple's per-capture AMDeviceConnect dance).

    Runs in a worker thread with its own event loop so it works whether
    or not the caller already has one (e.g. screen-mirror's aiohttp loop).
    The thread is named for diagnostics and the join timeout is logged
    visibly so a hung lockdown doesn't silently leak a worker."""
    async def _do() -> None:
        ld = await create_using_usbmux(serial=udid)
        async with ld:
            pass

    result: dict = {}

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_do())
            result["ok"] = True
        except Exception as e:
            result["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=_worker, daemon=True,
                         name=f"valeria-lockdown-poke-{udid[:8]}")
    t.start()
    t.join(timeout=10)
    if t.is_alive():
        logger.warning(
            "lockdown poke for %s timed out (worker thread leaked; "
            "capture will likely fail with 'capture started (0x0)')",
            udid[:8])
    elif "error" in result:
        logger.warning("lockdown poke for %s failed: %s",
                       udid[:8], result["error"])
    else:
        logger.debug("lockdown poke for %s ok", udid[:8])


@contextmanager
def qt_capture_mode(udid: str) -> Iterator[None]:
    """Switch iDevice to QT config for the with-block, restore idle on exit.

    Always forces an idle->qt transition (even if the iDevice is already
    in QT) so the iDevice-side QT pipeline reinitialises cleanly. The
    lockdown poke after the switch nudges the iDevice into rebuilding
    its capture pipeline state; without it captures intermittently come
    up with width=height=0 ('capture started (0x0)').

    A per-UDID in-process lock protects against accidental concurrent
    invocations against the same device (the second exit would yank the
    config out from under the first capture)."""
    lock = _get_udid_lock(udid)
    if not lock.acquire(blocking=False):
        raise RuntimeError(
            f"qt_capture_mode already active for {udid[:8]} in this process"
        )
    try:
        qt_cfg, idle_cfg = _discover_configs(udid)
        logger.debug("discovered configs: qt=%d, idle=%d", qt_cfg, idle_cfg)
        set_active_configuration(udid, idle_cfg)
        try:
            set_active_configuration(udid, qt_cfg)
            _poke_lockdown(udid)
            yield
        finally:
            try:
                set_active_configuration(udid, idle_cfg)
            except Exception as e:
                logger.warning("failed to restore idle config: %s", e)
    finally:
        lock.release()
