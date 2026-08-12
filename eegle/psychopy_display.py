"""PsychoPy window creation and measured display-timing checks."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


def create_psychopy_window(visual: Any, display: dict[str, Any], *, title: str = "EEGle") -> Any:
    """Create a VBlank-synchronized PsychoPy window from the shared display contract.

    PsychoPy's pyglet backend does not expose pyglet's ``resizable`` constructor
    option.  For a windowed operator display we pass that option only while the
    backend creates its native window, then install a resize callback which keeps
    PsychoPy's framebuffer, viewport, and height-based coordinate system aligned.
    """

    full_screen = bool(display.get("full_screen", False))
    win_type = str(display.get("win_type", "pyglet"))
    resizable = bool(display.get("resizable", True)) and not full_screen
    allow_gui = bool(display.get("allow_gui", True))
    kwargs = {
        "fullscr": full_screen,
        "screen": int(display.get("screen_index", 0)),
        "size": tuple(display.get("size", [1000, 700])),
        "winType": win_type,
        "units": str(display.get("units", "height")),
        "color": display.get("background_color", "black"),
        "allowGUI": allow_gui,
        "waitBlanking": bool(display.get("wait_blanking", True)),
        # Refresh is measured explicitly after construction so failure cannot be
        # confused with PsychoPy's internal 60 Hz fallback.
        "checkTiming": False,
        "title": title,
    }
    if resizable and win_type.lower() == "pyglet" and allow_gui:
        try:
            import pyglet  # noqa: F401
        except ModuleNotFoundError:
            # Lightweight test doubles may provide PsychoPy's public visual
            # surface without importing its pyglet runtime.
            win = visual.Window(**kwargs)
        else:
            with _pyglet_resizable_constructor():
                win = visual.Window(**kwargs)
        _install_pyglet_resize_handler(win)
    else:
        win = visual.Window(**kwargs)
    return win


def measure_psychopy_refresh_rate(win: Any, display: dict[str, Any]) -> dict[str, Any]:
    """Measure and validate display refresh rather than accepting a modeled rate."""

    expected = max(1.0, float(display.get("expected_refresh_rate_hz", 60.0)))
    tolerance = max(0.0, float(display.get("refresh_rate_tolerance_hz", 2.0)))
    check_enabled = bool(display.get("check_refresh_rate", True))
    check_required = bool(display.get("require_refresh_rate_match", False))
    measured = None
    error = None
    if check_enabled:
        getter = getattr(win, "getActualFrameRate", None)
        if callable(getter):
            try:
                measured = getter(
                    nIdentical=int(display.get("refresh_rate_identical_frames", 10)),
                    nMaxFrames=int(display.get("refresh_rate_max_frames", 120)),
                    nWarmUpFrames=int(display.get("refresh_rate_warmup_frames", 10)),
                    threshold=float(display.get("refresh_rate_stability_threshold_ms", 1.0)),
                    infoMsg=str(display.get("refresh_rate_check_message", "Checking display refresh rate...")),
                )
            except Exception as exc:  # pragma: no cover - hardware/backend specific
                error = f"{type(exc).__name__}: {exc}"
    measured_value = _positive_float(measured)
    deviation = None if measured_value is None else abs(measured_value - expected)
    within_tolerance = measured_value is not None and deviation <= tolerance
    if check_enabled and measured_value is None:
        status = "measurement_failed"
    elif check_enabled and not within_tolerance:
        status = "mismatch"
    elif check_enabled:
        status = "measured"
    else:
        status = "disabled"
    if check_required and status != "measured":
        if measured_value is None:
            detail = "PsychoPy could not obtain a stable measured refresh rate"
        else:
            detail = (
                f"measured refresh {measured_value:.3f} Hz differs from expected "
                f"{expected:.3f} Hz by more than {tolerance:.3f} Hz"
            )
        if error:
            detail += f" ({error})"
        raise RuntimeError(
            f"DSART display refresh check failed: {detail}. "
            "Set hardware.display.expected_refresh_rate_hz to the Windows display mode or correct the display settings."
        )
    effective = measured_value or expected
    if measured_value is not None:
        try:
            win._monitorFrameRate = measured_value
            win.monitorFramePeriod = 1.0 / measured_value
            win.refreshThreshold = win.monitorFramePeriod * 1.2
        except Exception:
            pass
    wait_blanking = bool(getattr(win, "waitBlanking", display.get("wait_blanking", True)))
    if check_required and not wait_blanking:
        raise RuntimeError("DSART display synchronization failed: PsychoPy waitBlanking is disabled")
    return {
        "status": status,
        "expected_refresh_rate_hz": expected,
        "measured_refresh_rate_hz": measured_value,
        "effective_refresh_rate_hz": effective,
        "refresh_rate_deviation_hz": deviation,
        "refresh_rate_tolerance_hz": tolerance,
        "refresh_rate_within_tolerance": within_tolerance,
        "refresh_rate_check_enabled": check_enabled,
        "refresh_rate_match_required": check_required,
        "refresh_rate_measurement_error": error,
        "wait_blanking": wait_blanking,
        "expected_frame_interval_ms": 1000.0 / effective,
        "fixed_display_latency_ms": float(display.get("fixed_display_latency_ms", 0.0)),
        "expected_visual_onset_uncertainty_ms": 500.0 / effective,
        "photodiode_verification_enabled": bool(display.get("photodiode_patch", False)),
    }


@contextmanager
def _pyglet_resizable_constructor() -> Iterator[None]:
    """Temporarily opt PsychoPy's pyglet window into native resizing."""

    import pyglet

    window_class = pyglet.window.Window
    original_init = window_class.__init__

    def resizable_init(instance: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("resizable", True)
        original_init(instance, *args, **kwargs)

    window_class.__init__ = resizable_init
    try:
        yield
    finally:
        window_class.__init__ = original_init


def _install_pyglet_resize_handler(win: Any) -> None:
    handle = getattr(win, "winHandle", None)
    backend = getattr(win, "backend", None)
    original = getattr(handle, "on_resize", None)
    if handle is None or backend is None or not callable(original):
        return

    def on_resize(width: int, height: int) -> Any:
        width = max(1, int(width))
        height = max(1, int(height))
        result = original(width, height)
        _replace_pair(getattr(win, "clientSize", None), width, height)
        if not bool(getattr(win, "fullscr", False)):
            try:
                win.windowedSize = (width, height)
            except Exception:
                pass
        framebuffer_width, framebuffer_height = width, height
        get_framebuffer_size = getattr(handle, "get_framebuffer_size", None)
        if callable(get_framebuffer_size):
            try:
                framebuffer_width, framebuffer_height = get_framebuffer_size()
            except Exception:
                pass
        _replace_pair(
            getattr(backend, "_frameBufferSize", None),
            max(1, int(framebuffer_width)),
            max(1, int(framebuffer_height)),
        )
        try:
            win.viewport = win.scissor = (0, 0, int(framebuffer_width), int(framebuffer_height))
            win.resetEyeTransform()
        except Exception:
            pass
        return result

    handle.on_resize = on_resize


def _replace_pair(target: Any, first: int, second: int) -> None:
    try:
        target[:] = (first, second)
    except Exception:
        pass


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0.0 else None
