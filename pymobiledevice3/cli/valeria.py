"""``pymobiledevice3 valeria`` -- H.264 screen capture over USB.

Exposes the unified :class:`pymobiledevice3.services.valeria.ValeriaScreenCapture`
service: enable iOS screen capture, write Annex-B-framed H.264 to a file or
stdout. Decode/render is the consumer's job; pipe the output to ``ffmpeg``
to render or transcode.

On Linux this requires our usbmuxd fork's ``dynamic-config-switch``
branch (https://github.com/renegadelink/usbmuxd) which adds the
``SetActiveConfiguration`` IPC, plus ``USBMUXD_DEFAULT_DEVICE_MODE=2``.
Each ``pymobiledevice3 valeria`` call
asks usbmuxd to switch the iDevice into a QT-enabled USB configuration
for the duration of the capture, then back to a QT-free configuration
when done - mirroring macOS's ``iOSScreenCaptureAssistant``. See
``docs/guides/valeria-linux-setup.md`` for one-time setup.

Examples
--------

    pymobiledevice3 valeria -o /tmp/out.h264
    pymobiledevice3 valeria -o - --duration 10 | ffplay -f h264 -
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Annotated, Optional

import typer
from typer_injector import InjectingTyper

from pymobiledevice3.services.valeria import (
    BackendUnavailableError,
    DeviceNotFoundError,
    ValeriaScreenCapture,
    MultipleDevicesError,
    ScreenRecordingPermissionError,
)

logger = logging.getLogger(__name__)


cli = InjectingTyper(
    name="valeria",
    help="iOS screen capture (H.264 over USB).",
    no_args_is_help=True,
)


def _resolve_backend(backend: str) -> str:
    if backend == "auto":
        return "cmio" if sys.platform == "darwin" else "libusb"
    return backend


def _open_sink(output: str) -> tuple:
    if output == "-":
        return sys.stdout.buffer, False
    return open(output, "wb"), True


def _capture_direct(udid: Optional[str], backend: str,
                    output: str, duration: int) -> None:
    """In-process capture - one short-lived ValeriaScreenCapture instance."""
    try:
        cap = ValeriaScreenCapture.create(udid=udid, backend=backend)
    except (BackendUnavailableError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    try:
        cap.start()
    except (DeviceNotFoundError, MultipleDevicesError,
            ScreenRecordingPermissionError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"error: failed to start capture: {exc}", err=True)
        raise typer.Exit(code=1)

    sink, close_sink = _open_sink(output)
    n_frames = 0
    n_bytes = 0

    def consume() -> None:
        nonlocal n_frames, n_bytes
        deadline = time.monotonic() + duration if duration > 0 else None
        try:
            for frame in cap.frames():
                data = frame.to_annex_b()
                sink.write(data)
                sink.flush()
                n_frames += 1
                n_bytes += len(data)
                if deadline is not None and time.monotonic() >= deadline:
                    break
        except KeyboardInterrupt:
            pass

    try:
        # cap.run() drives the main thread's CFRunLoop on the macOS CMIO
        # backend so callbacks dispatch event-driven to *consume* on a
        # worker thread. On Linux/Windows it's just a passthrough call.
        cap.run(consume)
    except KeyboardInterrupt:
        pass
    finally:
        if close_sink:
            sink.close()
        cap.stop()
        typer.echo(
            f"wrote {n_frames} frames ({n_bytes / 1024:.1f} KiB) "
            f"from {cap.device_name} ({cap.width}x{cap.height})",
            err=True,
        )


@cli.callback(invoke_without_command=True)
def capture(
    output: Annotated[str, typer.Option(
        "--output", "-o",
        help="Output file path, or '-' for stdout (e.g. for piping into ffmpeg).",
    )],
    udid: Annotated[Optional[str], typer.Option(
        "--udid",
        help="Match a specific device by UDID (required when multiple devices "
             "are attached).",
    )] = None,
    backend: Annotated[str, typer.Option(
        "--backend",
        help="auto (default; cmio on macOS, libusb elsewhere), cmio, or libusb.",
    )] = "auto",
    duration: Annotated[int, typer.Option(
        "--duration",
        help="Stop after N seconds (0 = run until interrupted).",
    )] = 0,
) -> None:
    """Capture the iOS screen as Annex-B H.264 and write to OUTPUT."""
    resolved = _resolve_backend(backend)
    if resolved == "cmio":
        _capture_direct(udid, backend, output, duration)
        return

    try:
        from pymobiledevice3.services.valeria_mode_switch import (
            find_idevices, qt_capture_mode,
        )
    except ImportError as exc:
        typer.echo(f"error: libusb backend not available: {exc}", err=True)
        raise typer.Exit(code=2)

    if udid is None:
        devs = find_idevices()
        if len(devs) == 1:
            udid = devs[0].serial_number
        elif len(devs) > 1:
            typer.echo("error: multiple iDevices, specify --udid", err=True)
            raise typer.Exit(code=1)
        else:
            typer.echo("error: no iDevice attached", err=True)
            raise typer.Exit(code=1)

    try:
        with qt_capture_mode(udid):
            time.sleep(1.0)  # iDevice QT pipeline settle
            _capture_direct(udid, backend, output, duration)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1)
