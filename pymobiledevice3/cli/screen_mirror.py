from __future__ import annotations

import sys
from typing import Annotated

import typer
from typer_injector import InjectingTyper

from pymobiledevice3.cli.cli_common import ServiceProviderDep, async_command

cli = InjectingTyper(
    name="screen-mirror",
    help="Mirror the device screen to a browser via the unified Valeria service.",
)


@cli.command("screen-mirror")
@async_command
async def screen_mirror(
    service_provider: ServiceProviderDep,
    host: Annotated[str, typer.Option(help="Bind address (use 0.0.0.0 to expose on the LAN).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port for the browser viewer.")] = 8080,
    backend: Annotated[str, typer.Option(
        help="Force a specific capture backend. "
             "auto = pick by platform (cmio on macOS, libusb elsewhere). "
             "One of: auto, cmio, libusb."
    )] = "auto",
    redact: Annotated[bool, typer.Option(
        "--redact",
        help="Scrub hostname, UDIDs, AVFoundation UUIDs, and user-set device "
             "names from log output so the log is safe to share publicly.",
    )] = False,
) -> None:
    """
    Mirror the device screen to a browser via the unified Valeria capture
    service (CoreMediaIO on macOS, libusb on Linux/Windows).

    \b
    Prerequisites
    * Device paired and trusted
    * macOS: Screen Recording TCC granted to your terminal app
    * Linux: upstream usbmuxd master with USBMUXD_DEFAULT_DEVICE_MODE=2.
      See docs/guides/valeria-linux-setup.md
    * pip install 'pymobiledevice3\\[screen-mirror]'
    """
    from pymobiledevice3.services.screen_mirror import ScreenMirrorService, install_pii_log_filter

    if redact:
        install_pii_log_filter()

    resolved_backend = backend if backend != "auto" else (
        "cmio" if sys.platform == "darwin" else "libusb"
    )

    if resolved_backend == "libusb":
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.valeria_mode_switch import qt_capture_mode
        # qt_capture_mode triggers usbmuxd device_remove/add internally,
        # so the typer-injected lockdown's socket dies during the switch.
        # Close it explicitly before the switch so usbmuxd's per-client
        # ref count for this device drops to zero cleanly, then create a
        # fresh one inside the with-block.
        udid = service_provider.identifier
        await service_provider.close()
        with qt_capture_mode(udid):
            lockdown = await create_using_usbmux(serial=udid)
            async with ScreenMirrorService(
                lockdown, host=host, port=port, backend=backend,
            ) as svc:
                await svc.serve()
    else:
        async with ScreenMirrorService(
            service_provider, host=host, port=port, backend=backend,
        ) as svc:
            await svc.serve()
