"""
OLEDTaskManager — system tray launcher

Runs sender.py silently in the background with a system tray icon.
Right-click the tray icon to stop.

Requirements:
    pip install pystray pillow

Run (no CMD window):
    pythonw run_tray.pyw [COM_PORT] [BAUD]
    e.g.  pythonw run_tray.pyw COM3
          pythonw run_tray.pyw COM3 115200

Or double-click run_tray.pyw from Explorer (Windows associates .pyw with pythonw).
"""

import sys
import threading
import sender  # sender.py must be in the same folder

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    import tkinter as tk
    import tkinter.messagebox as mb
    root = tk.Tk()
    root.withdraw()
    mb.showerror(
        "Missing dependencies",
        "pystray and Pillow are required.\n\nRun:\n  pip install pystray pillow"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Tray icon image — draw a small OLED-style monitor glyph
# ---------------------------------------------------------------------------
def _make_icon(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    m   = size // 8  # margin

    # Outer bezel
    d.rounded_rectangle([m, m, size - m, size - m * 3],
                        radius=size // 10, fill=(40, 40, 40))
    # Screen (green)
    s = m + size // 8
    d.rectangle([s, s, size - s, size - m * 3 - size // 10],
                fill=(0, 200, 80))
    # Stand base
    bx = size // 4
    d.rectangle([bx, size - m * 2, size - bx, size - m], fill=(40, 40, 40))
    # Stand neck
    nx = size // 2 - size // 10
    d.rectangle([nx, size - m * 3, nx + size // 5, size - m * 2],
                fill=(40, 40, 40))
    return img


# ---------------------------------------------------------------------------
# Run sender in a daemon thread
# ---------------------------------------------------------------------------
_stop_event = threading.Event()


def _run_sender() -> None:
    try:
        sender.main(stop_event=_stop_event)
    except SystemExit:
        pass
    except Exception as exc:
        # Surface errors via a tray notification if possible
        print(f"[ERROR] sender crashed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Tray callbacks
# ---------------------------------------------------------------------------
def _on_stop(icon: pystray.Icon, _item) -> None:
    _stop_event.set()
    icon.stop()


def _tray_title() -> str:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    return f"OLEDTaskManager — {port}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    thread = threading.Thread(target=_run_sender, daemon=True, name="sender")
    thread.start()

    icon = pystray.Icon(
        name="OLEDTaskManager",
        icon=_make_icon(),
        title=_tray_title(),
        menu=pystray.Menu(
            pystray.MenuItem(_tray_title(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Stop", _on_stop),
        ),
    )
    icon.run()  # blocks until icon.stop() is called


if __name__ == "__main__":
    main()
