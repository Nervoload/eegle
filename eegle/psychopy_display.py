"""PsychoPy window creation and measured display-timing checks."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from statistics import median, pstdev
from time import monotonic, sleep
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
    if not full_screen and win_type.lower() == "pyglet":
        _position_window_on_configured_screen(win, int(display.get("screen_index", 0)))
    return win


def measure_psychopy_refresh_rate(win: Any, display: dict[str, Any]) -> dict[str, Any]:
    """Measure the active monitor with low-overhead VBlank-synchronised flips.

    PsychoPy's ``getActualFrameRate`` renders a TextBox2 splash on every flip.
    That measurement workload can itself miss alternate VBlanks on windowed
    Windows systems, especially with multiple displays or hybrid graphics, and
    produce the observed false 30 Hz result on a 60 Hz monitor.  Sampling blank
    flips matches the task's lightweight presentation path more closely.  A
    robust median tolerates isolated compositor stalls without requiring an
    unrealistically perfect run of consecutive Windows frame intervals.
    """

    expected = max(1.0, float(display.get("expected_refresh_rate_hz", 120.0)))
    supported = _positive_rates(display.get("supported_refresh_rates_hz", []))
    tolerance = max(0.0, float(display.get("refresh_rate_tolerance_hz", 10.0)))
    check_enabled = bool(display.get("check_refresh_rate", True))
    check_required = bool(display.get("require_refresh_rate_match", False))
    measured_value: float | None = None
    measurement_values: list[float | None] = []
    measurement_errors: list[str] = []
    attempt_details: list[dict[str, Any]] = []
    selected_attempt: int | None = None
    selected_measurement: dict[str, Any] | None = None
    selected_monitor: dict[str, Any] | None = None
    maximum_attempts = max(1, int(display.get("refresh_rate_measurement_attempts", 3)))
    warmup_frames = max(0, int(display.get("refresh_rate_warmup_frames", 20)))
    sample_frames = max(12, int(display.get("refresh_rate_sample_frames", 90)))
    settle_seconds = max(0.0, float(display.get("refresh_rate_window_settle_seconds", 0.0)))
    retry_settle_seconds = max(
        0.0,
        float(display.get("refresh_rate_retry_settle_seconds", settle_seconds)),
    )
    stability_threshold_ms = max(
        0.0,
        float(display.get("refresh_rate_stability_threshold_ms", 1.0)),
    )
    monitor_inventory = psychopy_monitor_inventory(win)
    if check_enabled:
        print(f"[display] {_monitor_inventory_summary(monitor_inventory)}", flush=True)
    if check_required and _windows_windowed_pyglet_vsync_unavailable(win, display):
        raise RuntimeError(
            "DSART display refresh check cannot validate a windowed PsychoPy/pyglet "
            "window on Windows: pyglet disables OpenGL swap-interval VSync while the "
            "Desktop Window Manager is composing the window, so flip cadence is not a "
            "physical-monitor VBlank clock; "
            f"{_monitor_inventory_summary(monitor_inventory)}. "
            "Rerun the Windows operator script in its default fullscreen mode and select "
            "the target with -ScreenIndex N (do not pass -Windowed)."
        )
    if settle_seconds:
        _wait_for_psychopy_window_settle(win, settle_seconds)
    if check_enabled:
        for attempt in range(1, maximum_attempts + 1):
            if attempt > 1 and retry_settle_seconds:
                _wait_for_psychopy_window_settle(win, retry_settle_seconds)
            monitor_before = active_psychopy_monitor(win)
            try:
                sample = _measure_blank_flip_rate(
                    win,
                    warmup_frames=warmup_frames,
                    sample_frames=sample_frames,
                )
            except Exception as exc:  # pragma: no cover - hardware/backend specific
                error = f"attempt {attempt}: {type(exc).__name__}: {exc}"
                measurement_errors.append(error)
                measurement_values.append(None)
                attempt_details.append(
                    {
                        "attempt": attempt,
                        "status": "error",
                        "error": error,
                        "active_monitor_before": monitor_before,
                    }
                )
                continue
            monitor_after = active_psychopy_monitor(win)
            candidate = _positive_float(sample.get("rate_hz"))
            measurement_values.append(candidate)
            detail = {
                "attempt": attempt,
                "status": "measured",
                **sample,
                "active_monitor_before": monitor_before,
                "active_monitor_after": monitor_after,
            }
            if _monitor_or_window_changed(monitor_before, monitor_after):
                detail["status"] = "discarded_window_moved"
                measurement_errors.append(
                    f"attempt {attempt}: window moved or changed monitors during refresh measurement"
                )
                attempt_details.append(detail)
                continue
            target_rates = _active_monitor_target_rates(
                monitor_after,
                supported=supported,
                expected=expected,
                tolerance=tolerance,
            )
            nearest_target = min(target_rates, key=lambda value: abs(value - (candidate or 0.0)))
            detail["target_rates_hz"] = target_rates
            detail["nearest_target_hz"] = nearest_target
            detail["within_tolerance"] = bool(
                candidate is not None and abs(candidate - nearest_target) <= tolerance
            )
            attempt_details.append(detail)
            if detail["within_tolerance"]:
                measured_value = candidate
                selected_attempt = attempt
                selected_measurement = sample
                selected_monitor = monitor_after
                break
    if measured_value is None:
        usable_measurements = [
            float(detail["rate_hz"])
            for detail in attempt_details
            if detail.get("status") == "measured"
            and _positive_float(detail.get("rate_hz")) is not None
        ]
        if usable_measurements:
            # Preserve the closest observed value for an actionable mismatch
            # report after all retries have failed.
            fallback_targets = supported or [expected]
            measured_value = min(
                usable_measurements,
                key=lambda candidate: min(abs(candidate - target) for target in fallback_targets),
            )
    active_monitor = selected_monitor or active_psychopy_monitor(win)
    target_rates = _active_monitor_target_rates(
        active_monitor,
        supported=supported,
        expected=expected,
        tolerance=tolerance,
    )
    nominal = (
        min(target_rates, key=lambda value: abs(value - measured_value))
        if measured_value is not None
        else target_rates[0]
    )
    deviation = None if measured_value is None else abs(measured_value - nominal)
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
                f"measured refresh {measured_value:.3f} Hz differs from the nearest supported "
                f"mode ({nominal:.3f} Hz) by more than {tolerance:.3f} Hz"
            )
        if measurement_errors:
            detail += f" ({'; '.join(measurement_errors)})"
        detail += f"; {_active_monitor_summary(active_monitor)}"
        if measurement_values:
            detail += f"; attempts={measurement_values}"
        detail += f"; {_monitor_inventory_summary(monitor_inventory)}"
        raise RuntimeError(
            f"DSART display refresh check failed: {detail}. "
            "Use a supported 60 Hz or 120 Hz Windows display mode, or correct the display settings."
        )
    effective = measured_value or nominal
    stimulus_frames = _whole_frame_count(0.25, nominal)
    soi_frames = _whole_frame_count(1.60, nominal)
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
    if check_enabled:
        measured_text = "unavailable" if measured_value is None else f"{measured_value:.3f} Hz"
        print(
            "[display] "
            f"{_active_monitor_summary(active_monitor)}; "
            f"measured={measured_text}; "
            f"attempt={selected_attempt}/{maximum_attempts}; "
            f"method=blank_flip_robust_median_v1",
            flush=True,
        )
    return {
        "status": status,
        "expected_refresh_rate_hz": expected,
        "supported_refresh_rates_hz": supported,
        "nominal_refresh_rate_hz": nominal,
        "measured_refresh_rate_hz": measured_value,
        "effective_refresh_rate_hz": effective,
        "refresh_rate_deviation_hz": deviation,
        "refresh_rate_tolerance_hz": tolerance,
        "refresh_rate_within_tolerance": within_tolerance,
        "refresh_rate_check_enabled": check_enabled,
        "refresh_rate_match_required": check_required,
        "refresh_rate_measurement_attempts": len(measurement_values),
        "refresh_rate_maximum_attempts": maximum_attempts,
        "refresh_rate_measurements_hz": measurement_values,
        "refresh_rate_selected_attempt": selected_attempt,
        "refresh_rate_attempt_details": attempt_details,
        "refresh_rate_measurement_method": "blank_flip_robust_median_v1",
        "refresh_rate_sample_frames": sample_frames,
        "refresh_rate_warmup_frames": warmup_frames,
        "refresh_rate_interval_mean_ms": (
            selected_measurement.get("interval_mean_ms") if selected_measurement else None
        ),
        "refresh_rate_interval_std_ms": (
            selected_measurement.get("interval_std_ms") if selected_measurement else None
        ),
        "refresh_rate_interval_median_ms": (
            selected_measurement.get("interval_median_ms") if selected_measurement else None
        ),
        "refresh_rate_interval_mad_ms": (
            selected_measurement.get("interval_mad_ms") if selected_measurement else None
        ),
        "refresh_rate_interval_stable": (
            bool(selected_measurement.get("interval_mad_ms", float("inf")) <= stability_threshold_ms)
            if selected_measurement
            else None
        ),
        "refresh_rate_stability_threshold_ms": stability_threshold_ms,
        "active_monitor": active_monitor,
        "monitor_inventory": monitor_inventory,
        "refresh_rate_measurement_error": (
            "; ".join(measurement_errors) if measurement_errors else None
        ),
        "wait_blanking": wait_blanking,
        "expected_frame_interval_ms": 1000.0 / effective,
        "stimulus_frame_count": stimulus_frames,
        "soi_frame_count": soi_frames,
        "fixation_frame_count": soi_frames - stimulus_frames,
        "frame_locked_timing": True,
        "fixed_display_latency_ms": float(display.get("fixed_display_latency_ms", 0.0)),
        "expected_visual_onset_uncertainty_ms": 500.0 / effective,
        "photodiode_verification_enabled": bool(display.get("photodiode_patch", False)),
    }


def redraw_psychopy_after_resize(win: Any, *stimuli: Any) -> bool:
    """Repaint the current screen after a native resize event.

    The pyglet resize callback only updates PsychoPy's viewport and records that
    a repaint is needed.  Flipping from inside the native callback can recurse
    into pyglet's event dispatch, so task polling loops consume the request at a
    safe point instead.  This repaint deliberately does not schedule task
    markers or change the logical task state.
    """

    if not bool(getattr(win, "_eegle_resize_redraw_pending", False)):
        return False
    # Clear before flipping.  If another resize arrives during the flip, its
    # repaint request remains pending for the next polling iteration.
    win._eegle_resize_redraw_pending = False
    for stimulus in stimuli:
        if stimulus is not None:
            stimulus.draw()
    win.flip()
    return True


def service_psychopy_static_window(win: Any, *stimuli: Any) -> bool:
    """Pump native GUI events while a non-trial screen waits for input.

    PTB keyboard polling is asynchronous and, unlike PsychoPy's legacy event
    backend, does not itself service pyglet's Windows message queue.  Static
    instruction, practice, break, and completion loops therefore call this
    helper; frame-locked experimental trial loops deliberately do not.
    """

    _dispatch_psychopy_window_events(win)
    return redraw_psychopy_after_resize(win, *stimuli)


def _dispatch_psychopy_window_events(win: Any) -> None:
    """Dispatch the live backend without PsychoPy's broken class dispatcher.

    PsychoPy 2026.1 implements ``Window.dispatchAllWindowEvents`` as a
    classmethod which reads ``Window.backend``.  ``backend`` is an instance
    attribute, so the method raises the exact Windows error seen after the
    baseline.  Dispatch the active instance backend (or pyglet handle) instead.
    """

    backend = getattr(win, "backend", None)
    backend_dispatcher = getattr(backend, "dispatchEvents", None)
    if callable(backend_dispatcher):
        backend_dispatcher()
        return
    handle = getattr(win, "winHandle", None)
    handle_dispatcher = getattr(handle, "dispatch_events", None)
    if callable(handle_dispatcher):
        handle_dispatcher()


def _wait_for_psychopy_window_settle(win: Any, seconds: float) -> None:
    """Give an operator move/resize time, restarting the clock after movement."""

    stable_for = max(0.0, float(seconds))
    if stable_for <= 0.0:
        return
    maximum_wait = max(stable_for, stable_for * 4.0)
    started = monotonic()
    stable_since = started
    signature = _window_geometry_signature(active_psychopy_monitor(win))
    while monotonic() - stable_since < stable_for and monotonic() - started < maximum_wait:
        _dispatch_psychopy_window_events(win)
        current = _window_geometry_signature(active_psychopy_monitor(win))
        if current != signature:
            signature = current
            stable_since = monotonic()
        sleep(min(0.05, stable_for))


def _measure_blank_flip_rate(
    win: Any,
    *,
    warmup_frames: int,
    sample_frames: int,
) -> dict[str, Any]:
    """Measure flip cadence without PsychoPy's expensive measurement splash."""

    for _ in range(max(0, int(warmup_frames))):
        win.flip()
    timestamps: list[float] = []
    for _ in range(max(12, int(sample_frames)) + 1):
        returned = _positive_float(win.flip())
        timestamps.append(returned if returned is not None else monotonic())
    intervals = [
        later - earlier
        for earlier, later in zip(timestamps, timestamps[1:])
        if later > earlier
    ]
    if len(intervals) < 3:
        raise RuntimeError("PsychoPy did not return enough increasing VBlank timestamps")
    period = median(intervals)
    if period <= 0.0:
        raise RuntimeError("PsychoPy returned a nonpositive median frame interval")
    deviations = [abs(value - period) for value in intervals]
    return {
        "rate_hz": 1.0 / period,
        "interval_count": len(intervals),
        "interval_mean_ms": (sum(intervals) / len(intervals)) * 1000.0,
        "interval_std_ms": pstdev(intervals) * 1000.0,
        "interval_median_ms": period * 1000.0,
        "interval_mad_ms": median(deviations) * 1000.0,
    }


def active_psychopy_monitor(win: Any) -> dict[str, Any] | None:
    """Describe the monitor containing the largest part of the live window.

    Pyglet screen geometry is expressed in the same virtual-desktop coordinate
    system as its window location, so this follows a window moved between
    monitors instead of trusting the screen index used at construction.
    """

    handle = getattr(win, "winHandle", None)
    display = getattr(handle, "display", None)
    get_screens = getattr(display, "get_screens", None)
    if not callable(get_screens):
        screen = getattr(handle, "screen", None)
        display = getattr(screen, "display", None)
        get_screens = getattr(display, "get_screens", None)
    if not callable(get_screens):
        return None
    try:
        screens = list(get_screens())
    except Exception:
        return None
    if not screens:
        return None
    window_bounds = _pyglet_window_bounds(handle)
    current_screen = None
    get_window_screen = getattr(handle, "get_window_screen", None)
    if callable(get_window_screen):
        try:
            current_screen = get_window_screen()
        except Exception:
            current_screen = None
    ranked: list[tuple[int, int, Any]] = []
    for index, screen in enumerate(screens):
        bounds = _screen_bounds(screen)
        overlap = _rectangle_overlap_area(window_bounds, bounds) if window_bounds else 0
        ranked.append((overlap, -index, screen))
    if current_screen in screens:
        index = screens.index(current_screen)
        selected = current_screen
        bounds = _screen_bounds(selected)
        overlap = _rectangle_overlap_area(window_bounds, bounds) if window_bounds else 0
    elif window_bounds and any(row[0] > 0 for row in ranked):
        overlap, negative_index, selected = max(ranked, key=lambda row: (row[0], row[1]))
        index = -negative_index
    else:
        configured = int(getattr(win, "screen", 0) or 0)
        index = configured if 0 <= configured < len(screens) else 0
        selected = screens[index]
        overlap = 0
    bounds = _screen_bounds(selected)
    mode = None
    getter = getattr(selected, "get_mode", None)
    if callable(getter):
        try:
            mode = getter()
        except Exception:
            mode = None
    rate = _positive_float(getattr(mode, "rate", None))
    return {
        "source": (
            "pyglet_get_window_screen"
            if current_screen in screens
            else "pyglet_window_overlap"
        ),
        "index": index,
        "bounds": list(bounds) if bounds else None,
        "window_bounds": list(window_bounds) if window_bounds else None,
        "window_overlap_pixels": overlap,
        "refresh_rate_hz": rate,
        "mode_size": (
            [int(getattr(mode, "width")), int(getattr(mode, "height"))]
            if mode is not None
            and getattr(mode, "width", None) is not None
            and getattr(mode, "height", None) is not None
            else None
        ),
        "mode_depth_bits": (
            int(getattr(mode, "depth"))
            if mode is not None and getattr(mode, "depth", None) is not None
            else None
        ),
        "device_name": _pyglet_screen_device_name(selected),
    }


def psychopy_monitor_inventory(win: Any) -> list[dict[str, Any]]:
    """Return every Pyglet monitor mode visible to the live window.

    The inventory is intentionally derived from the same display object and
    ordering used by PsychoPy. This makes a Windows ``-ScreenIndex`` mismatch
    visible in the terminal without introducing a second enumeration scheme.
    """

    handle = getattr(win, "winHandle", None)
    display = getattr(handle, "display", None)
    get_screens = getattr(display, "get_screens", None)
    if not callable(get_screens):
        return []
    try:
        screens = list(get_screens())
    except Exception:
        return []
    current = None
    get_window_screen = getattr(handle, "get_window_screen", None)
    if callable(get_window_screen):
        try:
            current = get_window_screen()
        except Exception:
            current = None
    inventory: list[dict[str, Any]] = []
    for index, screen in enumerate(screens):
        mode = None
        get_mode = getattr(screen, "get_mode", None)
        if callable(get_mode):
            try:
                mode = get_mode()
            except Exception:
                mode = None
        bounds = _screen_bounds(screen)
        inventory.append(
            {
                "index": index,
                "active": screen is current,
                "bounds": list(bounds) if bounds else None,
                "mode_size": (
                    [int(getattr(mode, "width")), int(getattr(mode, "height"))]
                    if mode is not None
                    and getattr(mode, "width", None) is not None
                    and getattr(mode, "height", None) is not None
                    else None
                ),
                "refresh_rate_hz": _positive_float(getattr(mode, "rate", None)),
                "mode_depth_bits": (
                    int(getattr(mode, "depth"))
                    if mode is not None and getattr(mode, "depth", None) is not None
                    else None
                ),
                "device_name": _pyglet_screen_device_name(screen),
            }
        )
    return inventory


def _position_window_on_configured_screen(win: Any, screen_index: int) -> None:
    """Centre a windowed pyglet window on the selected physical monitor.

    Pyglet's ``screen`` constructor argument selects a monitor for fullscreen
    windows but does not position a normal window.  PsychoPy still exposes that
    argument in windowed mode, so make the operator's screen choice effective.
    """

    if bool(getattr(win, "fullscr", False)):
        return
    handle = getattr(win, "winHandle", None)
    display = getattr(handle, "display", None)
    get_screens = getattr(display, "get_screens", None)
    get_size = getattr(handle, "get_size", None)
    set_location = getattr(handle, "set_location", None)
    if not callable(get_screens) or not callable(get_size) or not callable(set_location):
        return
    try:
        screens = list(get_screens())
        index = int(screen_index)
        if index < 0 or index >= len(screens):
            return
        screen = screens[index]
        bounds = _screen_bounds(screen)
        if bounds is None:
            return
        screen_x, screen_y, screen_width, screen_height = bounds
        window_width, window_height = get_size()
        set_location(
            screen_x + max(0, (screen_width - int(window_width)) // 2),
            screen_y + max(0, (screen_height - int(window_height)) // 2),
        )
    except Exception:
        # The active-monitor measurement still follows the actual live window;
        # an unavailable platform positioning API is not a timing failure.
        return


def _active_monitor_target_rates(
    monitor: dict[str, Any] | None,
    *,
    supported: list[float],
    expected: float,
    tolerance: float,
) -> list[float]:
    available = supported or [expected]
    monitor_rate = _positive_float((monitor or {}).get("refresh_rate_hz"))
    if monitor_rate is None:
        return available
    nearest = min(available, key=lambda value: abs(value - monitor_rate))
    return [nearest] if abs(nearest - monitor_rate) <= max(tolerance, 1.0) else available


def _active_monitor_summary(monitor: dict[str, Any] | None) -> str:
    if monitor is None:
        return "active monitor unavailable"
    index = monitor.get("index")
    rate = _positive_float(monitor.get("refresh_rate_hz"))
    rate_text = "unknown mode" if rate is None else f"mode={rate:.3f} Hz"
    size = monitor.get("mode_size")
    size_text = "" if not size else f" {size[0]}x{size[1]}"
    device = monitor.get("device_name")
    device_text = "" if not device else f" device={device}"
    return f"active monitor index={index}{device_text}{size_text} {rate_text}"


def _monitor_inventory_summary(inventory: list[dict[str, Any]]) -> str:
    if not inventory:
        return "monitor inventory unavailable"
    rows = []
    for monitor in inventory:
        index = monitor.get("index")
        active = " active" if monitor.get("active") else ""
        device = monitor.get("device_name")
        device_text = "" if not device else f" {device}"
        size = monitor.get("mode_size") or []
        size_text = "unknown-size" if len(size) != 2 else f"{size[0]}x{size[1]}"
        rate = _positive_float(monitor.get("refresh_rate_hz"))
        rate_text = "unknown-Hz" if rate is None else f"{rate:.3f}Hz"
        bounds = monitor.get("bounds")
        rows.append(
            f"#{index}{active}{device_text} {size_text}@{rate_text} bounds={bounds}"
        )
    return "monitors=[" + "; ".join(rows) + "]"


def _pyglet_screen_device_name(screen: Any) -> str | None:
    for attribute in ("device_name", "_device_name", "name", "_name"):
        value = getattr(screen, attribute, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _windows_windowed_pyglet_vsync_unavailable(
    win: Any,
    display: dict[str, Any],
) -> bool:
    """Identify Pyglet's intentionally unpaced Windows/DWM windowed path."""

    if sys.platform != "win32":
        return False
    if str(display.get("win_type", "pyglet")).lower() != "pyglet":
        return False
    return not bool(getattr(win, "fullscr", display.get("full_screen", False)))


def _monitor_or_window_changed(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    if before is None or after is None:
        return False
    return _window_geometry_signature(before) != _window_geometry_signature(after)


def _window_geometry_signature(monitor: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if monitor is None:
        return None
    return (
        monitor.get("index"),
        tuple(monitor.get("bounds") or []),
        tuple(monitor.get("window_bounds") or []),
        monitor.get("refresh_rate_hz"),
    )


def _pyglet_window_bounds(handle: Any) -> tuple[int, int, int, int] | None:
    get_location = getattr(handle, "get_location", None)
    get_size = getattr(handle, "get_size", None)
    if not callable(get_location) or not callable(get_size):
        return None
    try:
        x, y = get_location()
        width, height = get_size()
        return int(x), int(y), max(1, int(width)), max(1, int(height))
    except Exception:
        return None


def _screen_bounds(screen: Any) -> tuple[int, int, int, int] | None:
    try:
        return (
            int(getattr(screen, "x")),
            int(getattr(screen, "y")),
            max(1, int(getattr(screen, "width"))),
            max(1, int(getattr(screen, "height"))),
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _rectangle_overlap_area(
    first: tuple[int, int, int, int] | None,
    second: tuple[int, int, int, int] | None,
) -> int:
    if first is None or second is None:
        return 0
    first_x, first_y, first_width, first_height = first
    second_x, second_y, second_width, second_height = second
    overlap_width = max(
        0,
        min(first_x + first_width, second_x + second_width) - max(first_x, second_x),
    )
    overlap_height = max(
        0,
        min(first_y + first_height, second_y + second_height) - max(first_y, second_y),
    )
    return overlap_width * overlap_height


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
    win._eegle_resize_redraw_pending = False

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
        win._eegle_resize_redraw_pending = True
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


def _positive_rates(values: Any) -> list[float]:
    rates = []
    for value in values or []:
        parsed = _positive_float(value)
        if parsed is not None and parsed not in rates:
            rates.append(parsed)
    return rates


def _whole_frame_count(seconds: float, refresh_rate_hz: float) -> int:
    frames = int(round(float(seconds) * float(refresh_rate_hz)))
    if frames <= 0 or abs((frames / float(refresh_rate_hz)) - float(seconds)) > 1e-9:
        raise RuntimeError(
            f"DSART timing {seconds:.3f}s is not representable as a whole number of frames "
            f"at {refresh_rate_hz:.3f} Hz"
        )
    return frames


def probe_psychopy_display_and_keyboard(config: dict[str, Any]) -> dict[str, Any]:
    """Probe the live display and keyboard in a disposable interpreter.

    PsychoPy's PTB backend keeps process-global native keyboard state.  The
    recording-suite parent also owns the baseline window, so constructing the
    preflight keyboard there can leave a queue competing with the later visual
    task.  A short-lived worker makes process exit the final resource boundary.
    """

    import json
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    display = dict(config.get("hardware", {}).get("display", {}) or {})
    timeout_seconds = float(display.get("preflight_probe_timeout_seconds", 60.0))
    if timeout_seconds <= 0.0:
        raise ValueError("display.preflight_probe_timeout_seconds must be positive")

    with tempfile.TemporaryDirectory(prefix="eegle-psychopy-probe-") as temporary_dir:
        directory = Path(temporary_dir)
        request_path = directory / "request.json"
        result_path = directory / "result.json"
        request_path.write_text(json.dumps({"config": config}), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "eegle.workers.psychopy_probe",
            "--request",
            str(request_path),
            "--result",
            str(result_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"PsychoPy display/input probe did not finish within {timeout_seconds:g} seconds"
            ) from exc

        if not result_path.exists():
            raise RuntimeError(
                f"PsychoPy display/input probe exited with code {completed.returncode} "
                "without a result artifact"
            )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"PsychoPy display/input probe returned an unreadable result: {exc}") from exc
        if not isinstance(result, dict):
            raise RuntimeError("PsychoPy display/input probe result is not a JSON object")
        if completed.returncode != 0 or result.get("status") != "ok":
            detail = result.get("error") or f"worker exit code {completed.returncode}"
            raise RuntimeError(f"PsychoPy display/input probe failed: {detail}")
        timing = result.get("timing")
        if not isinstance(timing, dict):
            raise RuntimeError("PsychoPy display/input probe did not return timing metadata")
        return timing


def _probe_psychopy_display_and_keyboard_inline(config: dict[str, Any]) -> dict[str, Any]:
    """Open the real task window and PTB queue inside the disposable worker."""

    from psychopy import visual
    from psychopy.hardware import keyboard as keyboard_module

    from eegle.psychopy_input import (
        create_hardware_keyboard,
        poll_hardware_keyboard,
        stop_hardware_keyboard,
    )

    display = dict(config.get("hardware", {}).get("display", {}) or {})
    win = None
    keyboard = None
    try:
        win = create_psychopy_window(visual, display, title="EEGle preflight")
        timing = measure_psychopy_refresh_rate(win, display)
        keyboard = create_hardware_keyboard(
            keyboard_module,
            backend=str(display.get("keyboard_backend", "ptb")),
        )
        # Exercise the queue once.  A backend/configuration failure therefore
        # occurs before LabRecorder creates a partial XDF.
        poll_hardware_keyboard(keyboard)
        timing["keyboard_backend"] = str(display.get("keyboard_backend", "ptb"))
        timing["window_opened"] = True
        return timing
    finally:
        try:
            if keyboard is not None:
                stop_hardware_keyboard(keyboard)
        finally:
            if win is not None:
                win.close()
