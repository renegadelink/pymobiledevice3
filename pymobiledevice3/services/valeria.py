"""Public Valeria capture service - unified iOS H.264 screen capture over USB.

Two implementations (selected by :py:meth:`ValeriaScreenCapture.create`):

- ``valeria_cmio`` - macOS via CoreMediaIO (pure ctypes, no PyObjC).
- ``valeria_libusb`` - Linux/Windows via libusb claiming the QuickTime alt-config.

Both speak the same QuickTime USB protocol the device exposes and emit identical
:class:`H264Frame` objects. Decode/render is the consumer's responsibility.
"""
from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Callable, Iterator, Literal, Optional

from pymobiledevice3.exceptions import (
    BackendUnavailableError,
    DeviceNotFoundError,
    MultipleDevicesError,
    ScreenRecordingPermissionError,
)

# Re-exported for callers that import them via the service module.
__all__ = [
    "BackendUnavailableError",
    "DeviceNotFoundError",
    "H264Frame",
    "ValeriaScreenCapture",
    "MultipleDevicesError",
    "ScreenRecordingPermissionError",
]


class H264Frame:
    """One H.264 access unit as it came off the device.

    Storage matches the existing ``valeria_libusb`` backend: ``nalu_data`` is
    a concatenation of AVCC-framed NAL units (each prefixed with a 4-byte
    big-endian length). ``sps`` / ``pps`` are carried on keyframes so the
    decoder can re-init mid-stream.
    """

    __slots__ = ("nalu_data", "sps", "pps", "width", "height",
                 "pts_value", "pts_scale")

    def __init__(self) -> None:
        self.nalu_data: bytes = b""
        self.sps: bytes = b""
        self.pps: bytes = b""
        self.width: int = 0
        self.height: int = 0
        self.pts_value: int = 0
        self.pts_scale: int = 0

    @property
    def is_keyframe(self) -> bool:
        """A frame is treated as a keyframe iff both SPS and PPS are present
        (the iOS encoder emits parameter sets only with IDR frames)."""
        return bool(self.sps and self.pps)

    @property
    def pts_ns(self) -> int:
        """Presentation timestamp in nanoseconds, derived from the CMTime
        value/scale pair. Returns 0 if scale is unset."""
        if self.pts_scale == 0:
            return 0
        return self.pts_value * 1_000_000_000 // self.pts_scale

    def to_annex_b(self) -> bytes:
        """Return Annex-B-framed bytes (``0x00000001`` start codes), with
        SPS/PPS prepended when present. Suitable for piping to FFmpeg or
        feeding a hardware decoder."""
        start_code = b"\x00\x00\x00\x01"
        parts: list[bytes] = []
        if self.sps:
            parts.append(start_code + self.sps)
        if self.pps:
            parts.append(start_code + self.pps)
        pos = 0
        data = self.nalu_data
        while pos + 4 <= len(data):
            nalu_len = int.from_bytes(data[pos:pos + 4], "big")
            pos += 4
            if nalu_len <= 0 or pos + nalu_len > len(data):
                break
            parts.append(start_code + data[pos:pos + nalu_len])
            pos += nalu_len
        return b"".join(parts)


Backend = Literal["auto", "cmio", "libusb"]


class ValeriaScreenCapture(ABC):
    """Abstract base class. Concrete implementations live in
    ``valeria_cmio`` (macOS) and ``valeria_libusb`` (Linux/Windows).

    Concrete implementations push :class:`H264Frame` objects onto an
    internal bounded queue (capacity 90) and drain the queue on overflow
    so consumers resync at the next IDR.
    """

    @classmethod
    def create(cls, udid: Optional[str] = None,
               backend: Backend = "auto") -> "ValeriaScreenCapture":
        """Construct the appropriate backend.

        ``backend='auto'`` picks ``cmio`` on macOS and ``libusb`` everywhere
        else. Explicit values raise :class:`BackendUnavailableError` when the
        platform doesn't support them; there is no silent fallback.
        """
        if backend not in ("auto", "cmio", "libusb"):
            raise ValueError(
                f"backend must be one of 'auto', 'cmio', 'libusb'; got {backend!r}"
            )

        is_mac = sys.platform == "darwin"
        if backend == "auto":
            backend = "cmio" if is_mac else "libusb"

        if backend == "cmio":
            if not is_mac:
                raise BackendUnavailableError(
                    "backend='cmio' requires macOS (CoreMediaIO is Apple-only)"
                )
            try:
                from pymobiledevice3.services.valeria_cmio import ValeriaCMIO
            except ImportError as exc:
                raise BackendUnavailableError(
                    f"cmio backend module not available: {exc}"
                ) from exc
            return ValeriaCMIO(udid=udid)

        # libusb
        if is_mac:
            raise BackendUnavailableError(
                "backend='libusb' is not usable on macOS - libusb cannot claim "
                "Apple USB interfaces on macOS. Use backend='cmio' instead."
            )
        try:
            from pymobiledevice3.services.valeria_libusb import ValeriaLibusb
        except ImportError as exc:
            raise BackendUnavailableError(
                f"libusb backend module not available: {exc}"
            ) from exc
        return ValeriaLibusb(udid=udid)

    @abstractmethod
    def start(self) -> None:
        """Open the device, negotiate the H.264 stream, begin capture.

        Raises :class:`DeviceNotFoundError`, :class:`MultipleDevicesError`,
        or :class:`ScreenRecordingPermissionError` on the relevant failures.

        On macOS the CoreMediaIO backend blocks the calling thread for up to
        ~10 s the first time it runs (DAL plugin load). When calling from
        inside an asyncio context, use :meth:`astart` so the event loop
        stays responsive during the wait."""

    async def astart(self) -> None:
        """Async-friendly :meth:`start`. The default just calls
        :meth:`start` synchronously - backends with a long blocking startup
        (CMIO) override this to yield to the event loop between internal
        waits so other coroutines (lockdown keep-alives etc.) keep running."""
        self.start()

    @abstractmethod
    def stop(self) -> None:
        """Close the stream and release device resources."""

    def pause(self) -> None:
        """Pause the AV stream without releasing the device.

        Sends HPA0/HPD0 to the iDevice's AV pipeline and waits for the
        corresponding RELS acknowledgement, but keeps the USB interface
        claimed and the protocol handler running. Call :meth:`resume` to
        restart frame production for the same instance.

        On the macOS CoreMediaIO backend this is a no-op - Apple's
        iOSScreenCaptureAssistant manages pause/resume internally and
        callers see only the start/stop boundary."""

    def resume(self) -> None:
        """Resume a paused AV stream. No-op if the stream isn't paused.

        Sends HPD1 to make the iDevice re-initiate the SYNC CWPA / CVRP
        handshake, then waits briefly for the first frame so callers can
        rely on :attr:`width`/:attr:`height` being populated on return."""

    @property
    @abstractmethod
    def width(self) -> int:
        """Stream width in pixels. 0 until the first frame is parsed."""

    @property
    @abstractmethod
    def height(self) -> int:
        """Stream height in pixels. 0 until the first frame is parsed."""

    @property
    @abstractmethod
    def device_name(self) -> str:
        """Human-readable device name."""

    @abstractmethod
    def frames(self) -> Iterator[H264Frame]:
        """Blocking iterator that yields frames until :meth:`stop` is called.

        ``frames()`` and :meth:`aframes` drain the same internal queue - use
        one or the other per session, not both."""

    @abstractmethod
    def aframes(self) -> AsyncIterator[H264Frame]:
        """Async iterator counterpart of :meth:`frames`. Same queue rules apply."""

    def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Invoke ``fn(*args, **kwargs)`` under whatever threading context
        this backend prefers, returning ``fn``'s return value.

        For backends with no thread requirements (libusb on Linux/Win) this
        is a transparent passthrough. The CoreMediaIO backend on macOS
        commandeers the calling thread to drive ``CFRunLoopRun()``
        continuously while ``fn`` runs on a worker thread - required because
        the iOS DAL plugin dispatches its callbacks only via the main
        thread's CFRunLoop. ``fn`` can iterate :meth:`frames` /
        :meth:`aframes` event-driven (no polling) inside this wrapper.

        Use it any time you'd loop over frames in a long-running program::

            cap.start()
            try:
                cap.run(lambda: [process(f) for f in cap.frames()])
            finally:
                cap.stop()
        """
        return fn(*args, **kwargs)
