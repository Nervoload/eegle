"""Runtime helpers for the EEG experiment environment."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CACHE_ROOT_ENV_VARS = ("EEGLE_RUNTIME_CACHE_ROOT", "CLOSEDLOOP_RUNTIME_CACHE_ROOT")
SESSION_ROOT_ENV_VARS = ("EEGLE_SESSION_ROOT", "CLOSEDLOOP_SESSION_ROOT")


def resolve_runtime_cache_root(cache_dir: str | Path = ".runtime") -> Path:
    """Resolve caches without requiring shell-side JSON rewriting.

    Absolute config paths remain authoritative. Relative paths may be moved by
    a dedicated cache-root environment variable, then by the selected session
    root. This keeps constrained Windows shells to simple environment-variable
    assignments.
    """

    candidate = Path(os.path.expandvars(str(cache_dir))).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    for name in RUNTIME_CACHE_ROOT_ENV_VARS:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return Path(os.path.expandvars(value)).expanduser().resolve()
    for name in SESSION_ROOT_ENV_VARS:
        value = os.environ.get(name)
        if value is not None and value.strip():
            session_root = Path(os.path.expandvars(value)).expanduser()
            return (session_root / candidate).resolve()
    return (PROJECT_ROOT / candidate).resolve()


def ensure_runtime_environment(cache_dir: str | Path = ".runtime") -> Path:
    """Create local writable runtime cache dirs and export useful env vars."""
    cache_root = resolve_runtime_cache_root(cache_dir)
    matplotlib_dir = cache_root / "matplotlib"
    psychopy_home = cache_root / "psychopy_home"
    psychopy_dir = psychopy_home / ".psychopy3"
    appdata_dir = cache_root / "appdata"
    local_appdata_dir = cache_root / "local_appdata"
    lsl_config = cache_root / "lsl_api.cfg"
    matplotlib_dir.mkdir(parents=True, exist_ok=True)
    psychopy_dir.mkdir(parents=True, exist_ok=True)
    appdata_dir.mkdir(parents=True, exist_ok=True)
    local_appdata_dir.mkdir(parents=True, exist_ok=True)
    if not lsl_config.exists():
        lsl_config.write_text(
            "\n".join(
                [
                    "[ports]",
                    "MulticastPort = 16571",
                    "BasePort = 16572",
                    "PortRange = 128",
                    "",
                    "[log]",
                    "level = -2",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_dir))
    os.environ.setdefault("CLOSEDLOOP_ORIGINAL_HOME", os.environ.get("HOME", ""))
    os.environ["HOME"] = str(psychopy_home)
    if sys.platform == "win32":
        os.environ.setdefault("CLOSEDLOOP_ORIGINAL_USERPROFILE", os.environ.get("USERPROFILE", ""))
        os.environ.setdefault("CLOSEDLOOP_ORIGINAL_APPDATA", os.environ.get("APPDATA", ""))
        os.environ.setdefault("CLOSEDLOOP_ORIGINAL_LOCALAPPDATA", os.environ.get("LOCALAPPDATA", ""))
        os.environ["USERPROFILE"] = str(psychopy_home)
        os.environ["APPDATA"] = str(appdata_dir)
        os.environ["LOCALAPPDATA"] = str(local_appdata_dir)
    os.environ.setdefault("PSYCHOPY_USERAPPDATA", str(psychopy_dir))
    os.environ.setdefault("LSLAPICFG", str(lsl_config))
    _disable_psychopy_glfw()
    return cache_root


def _disable_psychopy_glfw() -> None:
    """Prevent PsychoPy from importing GLFW in this runner by default.

    PsychoPy's event module eagerly imports ``glfw`` and calls ``glfw.init()``
    when the package is importable. In some macOS environments that native
    init can abort the interpreter before our task code runs. The experiment
    configs use pyglet, so hiding glfw keeps PsychoPy on the known backend.
    Set CLOSEDLOOP_ALLOW_PSYCHOPY_GLFW=1 before launch to opt back in.
    """
    if sys.platform != "darwin":
        return
    if os.environ.get("CLOSEDLOOP_ALLOW_PSYCHOPY_GLFW", "").lower() in {"1", "true", "yes"}:
        return
    if "glfw" not in sys.modules:
        sys.modules["glfw"] = None


def prepare_psychopy_runtime(cache_dir: str | Path = ".runtime") -> None:
    """Prepare writable PsychoPy prefs and patch macOS pyglet event handling."""
    ensure_runtime_environment(cache_dir)
    apply_pyglet_macos_notification_patch()


def apply_pyglet_macos_notification_patch() -> None:
    """Skip macOS notifications which pyglet 1.5 may treat like NSEvents.

    On some macOS/Python/PsychoPy combinations, Cocoa can return an
    NSConcreteNotification from nextEventMatchingMask. Pyglet 1.5 assumes every
    object has event.type(), which crashes before keyboard events can be read.
    """
    if sys.platform != "darwin":
        return
    try:
        from pyglet.event import EventDispatcher
        from pyglet.libs.darwin import cocoapy
        import pyglet.window.cocoa as cocoa
    except Exception:
        return

    if getattr(cocoa.CocoaWindow.dispatch_events, "_eegle_patched", False):
        return

    def dispatch_events(self):  # type: ignore[no-untyped-def]
        self._allow_dispatch_event = True
        while self._event_queue:
            event = self._event_queue.pop(0)
            EventDispatcher.dispatch_event(self, *event)

        event = True
        pool = cocoa.NSAutoreleasePool.new()
        NSApp = cocoa.NSApplication.sharedApplication()
        while event and self._nswindow and self._context:
            event = NSApp.nextEventMatchingMask_untilDate_inMode_dequeue_(
                cocoapy.NSAnyEventMask,
                None,
                cocoapy.NSEventTrackingRunLoopMode,
                True,
            )
            if not event:
                continue
            try:
                event_type = event.type()
            except AttributeError:
                continue
            NSApp.sendEvent_(event)
            if event_type == cocoapy.NSKeyDown and not event.isARepeat():
                NSApp.sendAction_to_from_(cocoapy.get_selector("pygletKeyDown:"), None, event)
            elif event_type == cocoapy.NSKeyUp:
                NSApp.sendAction_to_from_(cocoapy.get_selector("pygletKeyUp:"), None, event)
            elif event_type == cocoapy.NSFlagsChanged:
                NSApp.sendAction_to_from_(cocoapy.get_selector("pygletFlagsChanged:"), None, event)
            NSApp.updateWindows()

        pool.drain()
        self._allow_dispatch_event = False

    dispatch_events._eegle_patched = True  # type: ignore[attr-defined]
    cocoa.CocoaWindow.dispatch_events = dispatch_events
