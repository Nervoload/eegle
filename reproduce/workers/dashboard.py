"""Localhost-only live dashboard for classifier predictions."""

from __future__ import annotations

import argparse
import heapq
import json
import random
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import numpy as np

from reproduce.config import load_config
from reproduce.lsl import inlet_time_correction, lsl_processing_flags
from reproduce.realtime.demo_classifier import DEMO_DISCLOSURE, demo_config_from, demo_prediction_from_marker
from reproduce.realtime.epoching import parse_marker_label
from reproduce.realtime.models import binary_classification_metrics
from reproduce.session import paths_for_existing_session
from reproduce.telemetry import Telemetry
from reproduce.workers.common import StatusWriter, append_jsonl, install_stop_signal_handlers


def dashboard_snapshot(session_dir: str | Path, demo_state: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    config = load_config(root / "parameters.json") if (root / "parameters.json").exists() else {}
    demo_config = demo_config_from(config)
    statuses = _process_statuses(root)
    if demo_config["enabled"]:
        return _demo_dashboard_snapshot(root, demo_config, statuses, demo_state or {})

    predictions = _load_jsonl(root / "realtime" / "model_predictions.jsonl")
    manifest = _load_json(root / "events" / "stimulus_manifest.json") or {}
    truth = {
        int(row["trial"]): int(bool(dict(row.get("stimulus") or {}).get("is_no_go")))
        for row in manifest.get("trials", [])
        if int(row.get("trial", 0) or 0) >= 1
    }
    model_ids = sorted({str(row.get("model_id")) for row in predictions if row.get("model_id")})
    metrics: dict[str, Any] = {}
    latest: dict[str, Any] = {}
    for model_id in model_ids:
        rows = [row for row in predictions if row.get("model_id") == model_id and row.get("status") == "predicted"]
        if rows:
            latest[model_id] = rows[-1]
        scored = [row for row in rows if int(row.get("trial", -1) or -1) in truth]
        if scored:
            metrics[model_id] = binary_classification_metrics(
                np.asarray([truth[int(row["trial"])] for row in scored], dtype=int),
                np.asarray([float(row["probability_no_go"]) for row in scored], dtype=float),
            )
            metrics[model_id]["coverage"] = len(scored) / max(1, len(truth))
        latencies = [float(row["processing_latency_ms"]) for row in rows if row.get("processing_latency_ms") is not None]
        metrics.setdefault(model_id, {})["mean_latency_ms"] = float(np.mean(latencies)) if latencies else None
    predicted = [row for row in predictions if row.get("status") == "predicted"]
    rejected = [row for row in predictions if row.get("status") == "rejected"]
    agreement = _primary_shadow_agreement(predicted)
    return {
        "schema_version": 1,
        "mode": "classifier",
        "title": "Realtime EEG Classifier",
        "disclosure": "Live classifier mode: predictions come from the configured EEG model pipeline.",
        "session_dir": str(root),
        "model_ids": model_ids,
        "latest": latest,
        "metrics": metrics,
        "predictions": predicted[-500:],
        "prediction_count": len(predicted),
        "rejected_epoch_count": len(rejected),
        "truth_count": len(truth),
        "primary_shadow_agreement": agreement,
        "processes": statuses,
    }


def _demo_dashboard_snapshot(
    root: Path,
    config: dict[str, Any],
    statuses: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    rows = [
        row
        for row in _load_jsonl(root / "realtime" / "demo_predictions.jsonl")
        if row.get("status") == "predicted"
    ]
    correct = sum(bool(row.get("is_correct")) for row in rows)
    latest = rows[-1] if rows else None
    return {
        "schema_version": 1,
        "mode": "demo",
        "title": "Brain Signal Guessing Demo",
        "disclosure": DEMO_DISCLOSURE,
        "session_dir": str(root),
        "latest": latest,
        "predictions": rows[-200:],
        "prediction_count": len(rows),
        "correct_count": correct,
        "accuracy": correct / len(rows) if rows else None,
        "prediction_delay_seconds": config["prediction_delay_seconds"],
        "configured_error_rate": config["error_rate"],
        "marker_status": state.get("marker_status", "starting"),
        "marker_stream": state.get("marker_stream"),
        "pending": list(state.get("pending", [])),
        "received_marker_count": int(state.get("received_marker_count", 0)),
        "processes": statuses,
    }


def _process_statuses(root: Path) -> dict[str, Any]:
    statuses = {}
    for path in (root / "logs" / "processes").glob("*.status.json"):
        statuses[path.stem.removesuffix(".status")] = _load_json(path)
    return statuses


def _primary_shadow_agreement(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    primary = {
        int(row["trial"]): row
        for row in predictions
        if row.get("model_role") == "primary" and row.get("trial") is not None
    }
    comparisons = 0
    agreements = 0
    by_shadow: dict[str, dict[str, Any]] = {}
    for row in predictions:
        if row.get("model_role") != "shadow" or row.get("trial") is None:
            continue
        reference = primary.get(int(row["trial"]))
        if reference is None:
            continue
        model_id = str(row.get("model_id"))
        item = by_shadow.setdefault(model_id, {"comparisons": 0, "agreements": 0})
        item["comparisons"] += 1
        comparisons += 1
        if row.get("predicted_condition") == reference.get("predicted_condition"):
            item["agreements"] += 1
            agreements += 1
    for item in by_shadow.values():
        item["rate"] = item["agreements"] / max(1, item["comparisons"])
    return {
        "comparisons": comparisons,
        "agreements": agreements,
        "rate": agreements / max(1, comparisons) if comparisons else None,
        "by_shadow": by_shadow,
    }


class DemoPredictionBridge:
    """Subscribe to the session marker outlet and emit delayed toy guesses."""

    def __init__(
        self,
        config: dict[str, Any],
        output_path: Path,
        telemetry: Telemetry,
        stop_event: threading.Event,
    ) -> None:
        self.config = config
        self.demo_config = demo_config_from(config)
        self.output_path = output_path
        self.telemetry = telemetry
        self.stop_event = stop_event
        self.rng = random.Random(self.demo_config["seed"])
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending_heap: list[tuple[float, int, str, float]] = []
        self._pending: dict[int, dict[str, Any]] = {}
        self._seen: set[tuple[str, float]] = set()
        self._marker_status = "starting"
        self._marker_stream: dict[str, Any] | None = None
        self._received_marker_count = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="dashboard-demo-lsl", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        now = monotonic()
        with self._lock:
            pending = [
                {**value, "remaining_seconds": max(0.0, float(value["due_at_monotonic"]) - now)}
                for value in sorted(self._pending.values(), key=lambda item: int(item["trial"]))
            ]
            return {
                "marker_status": self._marker_status,
                "marker_stream": self._marker_stream,
                "pending": pending,
                "received_marker_count": self._received_marker_count,
            }

    def _run(self) -> None:
        try:
            import pylsl
        except Exception as exc:
            self._set_marker_status("failed")
            self.telemetry.emit(
                "demo.marker_bridge_failed",
                level="default",
                message=f"Demo marker bridge could not import pylsl: {type(exc).__name__}: {exc}",
            )
            return

        inlet = None
        while not self.stop_event.is_set():
            try:
                if inlet is None:
                    inlet, stream = _open_session_marker_inlet(pylsl, self.config)
                    if inlet is None:
                        self._set_marker_status("waiting_for_marker_stream")
                        sleep(0.2)
                        self._emit_due_predictions()
                        continue
                    with self._lock:
                        self._marker_status = "connected"
                        self._marker_stream = stream
                    self.telemetry.emit(
                        "demo.marker_stream_connected",
                        level="default",
                        message="Demo dashboard connected to the PsychoPy LSL marker stream",
                        metadata=stream,
                    )
                samples, timestamps = inlet.pull_chunk(timeout=0.05, max_samples=32)
                for sample, timestamp in zip(samples, timestamps):
                    label = str(sample[0] if isinstance(sample, list) else sample)
                    self._schedule_marker(label, float(timestamp))
                self._emit_due_predictions()
            except Exception as exc:
                _close_inlet(inlet)
                inlet = None
                self._set_marker_status("reconnecting")
                self.telemetry.emit(
                    "demo.marker_stream_reconnecting",
                    level="default",
                    message="Demo dashboard lost the marker stream and will reconnect",
                    metadata={"exception_type": type(exc).__name__, "exception": str(exc)},
                )
                sleep(0.2)
        _close_inlet(inlet)

    def _schedule_marker(self, label: str, timestamp: float) -> None:
        key = (label, timestamp)
        if key in self._seen:
            return
        self._seen.add(key)
        parsed = parse_marker_label(label, self.demo_config["marker_prefix"])
        trial = parsed.get("trial")
        if not isinstance(trial, int) or trial < 1 or parsed.get("condition") not in {"go", "no_go", "nogo"}:
            return
        due_at = monotonic() + self.demo_config["prediction_delay_seconds"]
        pending = {
            "trial": trial,
            "status": "collecting_demo_window",
            "received_at": datetime.now().isoformat(timespec="milliseconds"),
            "due_at_monotonic": due_at,
        }
        with self._lock:
            self._received_marker_count += 1
            self._pending[trial] = pending
            heapq.heappush(self._pending_heap, (due_at, trial, label, timestamp))
        self.telemetry.emit(
            "demo.marker_received",
            level="realtime",
            message=f"Demo marker received for trial {trial}",
            metadata={"trial": trial, "prediction_delay_seconds": self.demo_config["prediction_delay_seconds"]},
        )

    def _emit_due_predictions(self) -> None:
        now = monotonic()
        due = []
        with self._lock:
            while self._pending_heap and self._pending_heap[0][0] <= now:
                due.append(heapq.heappop(self._pending_heap))
        for _, trial, label, timestamp in due:
            prediction = demo_prediction_from_marker(label, timestamp, self.demo_config, self.rng)
            if prediction is None:
                continue
            append_jsonl(self.output_path, prediction)
            with self._lock:
                self._pending.pop(trial, None)
            self.telemetry.emit(
                "demo.prediction",
                level="realtime",
                message=f"Demo guess: {prediction['predicted_condition']}",
                metadata={
                    "trial": trial,
                    "predicted_condition": prediction["predicted_condition"],
                    "is_correct": prediction["is_correct"],
                },
            )

    def _set_marker_status(self, value: str) -> None:
        with self._lock:
            self._marker_status = value


def _open_session_marker_inlet(pylsl: Any, config: dict[str, Any]) -> tuple[Any | None, dict[str, Any] | None]:
    marker_config = dict(config.get("hardware", {}).get("markers", {}))
    name = str(marker_config.get("lsl_stream_name", "EEGleMarkers"))
    stream_type = str(marker_config.get("lsl_stream_type", "Markers"))
    source_id = marker_config.get("source_id")
    matches = [
        info
        for info in pylsl.resolve_streams(wait_time=0.05)
        if info.name() == name and info.type() == stream_type and (not source_id or info.source_id() == source_id)
    ]
    if len(matches) != 1:
        return None, None
    info = matches[0]
    inlet = pylsl.StreamInlet(
        info,
        max_buflen=30,
        max_chunklen=16,
        recover=True,
        processing_flags=lsl_processing_flags(pylsl, dejitter=False),
    )
    inlet.open_stream(timeout=0.2)
    return inlet, {
        "name": info.name(),
        "type": info.type(),
        "source_id": info.source_id(),
        "lsl_processing": ["clocksync", "monotonize"],
        "initial_time_correction_seconds": inlet_time_correction(inlet, timeout=0.2),
    }


def _close_inlet(inlet: Any) -> None:
    if inlet is None:
        return
    try:
        inlet.close_stream()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local classifier dashboard worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--backend", default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    paths = paths_for_existing_session(args.session_dir)
    telemetry = Telemetry.from_config(config, paths, component="dashboard")
    status = StatusWriter(paths.process_logs / "dashboard.status.json", "dashboard", args.backend, telemetry)
    if args.backend in {"disabled", "none"}:
        status.update("disabled", reason="dashboard disabled")
        return 0
    if args.backend != "http" or args.host not in {"127.0.0.1", "localhost"}:
        status.update("failed", error="dashboard requires http backend bound to localhost")
        return 2
    stop_event = threading.Event()
    install_stop_signal_handlers(stop_event)
    demo_bridge = None
    demo_config = demo_config_from(config)
    if demo_config["enabled"]:
        demo_bridge = DemoPredictionBridge(config, paths.realtime / "demo_predictions.jsonl", telemetry, stop_event)
        demo_bridge.start()
    handler = _handler_for(paths.root, demo_bridge)
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
        server.timeout = 0.25
    except Exception as exc:
        if demo_bridge is not None:
            demo_bridge.stop()
        status.update("failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    status.update(
        "running",
        url=f"http://{args.host}:{args.port}",
        auto_open=False,
        mode="demo" if demo_config["enabled"] else "classifier",
    )
    try:
        while not stop_event.is_set():
            server.handle_request()
    finally:
        server.server_close()
        if demo_bridge is not None:
            demo_bridge.stop()
    status.update("stopped", url=f"http://{args.host}:{args.port}")
    return 0


def _handler_for(
    session_dir: Path,
    demo_bridge: DemoPredictionBridge | None = None,
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/snapshot":
                state = None if demo_bridge is None else demo_bridge.snapshot()
                self._send("application/json", json.dumps(dashboard_snapshot(session_dir, state)).encode("utf-8"))
                return
            if self.path in {"/", "/index.html"}:
                self._send("text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
                return
            self.send_error(404)

        def _send(self, content_type: str, payload: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EEGle Realtime Classifier</title>
<style>
:root{--ink:#38280b;--muted:#826a3c;--panel:#fffdf7;--line:#ead7a5;--yellow:#d49a00;--orange:#e96b18;--green:#2f8057;--red:#b84127;--amber:#ffd66b}
*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:"Avenir Next","Trebuchet MS",Avenir,system-ui,sans-serif;color:var(--ink);background:radial-gradient(circle at 12% 0%,#fff1b9 0,transparent 32%),radial-gradient(circle at 92% 12%,#ffd2a0 0,transparent 30%),#fffaf0}
body:before{content:"";position:fixed;inset:0;pointer-events:none;background-image:linear-gradient(rgba(141,92,0,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(141,92,0,.035) 1px,transparent 1px);background-size:34px 34px}
.app{position:relative;max-width:1440px;margin:auto;padding:28px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:18px}.brand{display:flex;align-items:center;gap:14px}.logo{width:45px;height:45px;border-radius:14px;background:linear-gradient(145deg,var(--amber),var(--orange));display:grid;place-items:center;color:#432b00;font-weight:950;box-shadow:0 12px 30px rgba(210,126,0,.24)}h1{font-size:22px;margin:0;letter-spacing:-.02em}.subtitle{color:var(--muted);font-size:13px;margin-top:3px}.mode-badge,.status-pill{display:inline-flex;align-items:center;gap:7px;padding:8px 12px;border:1px solid var(--line);border-radius:999px;background:#fff8e6;font-size:11px;font-weight:800;letter-spacing:.11em;text-transform:uppercase}.dot{width:8px;height:8px;border-radius:50%;background:var(--green)}
.disclosure{display:flex;gap:12px;align-items:flex-start;border:1px solid #e5bb51;background:#fff3c9;color:#654300;border-radius:14px;padding:12px 15px;margin-bottom:18px;font-size:13px;line-height:1.45}.disclosure strong{white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}.stat,.card{border:1px solid var(--line);background:var(--panel);box-shadow:0 16px 45px rgba(119,72,0,.09)}.stat{border-radius:14px;padding:13px 15px}.stat-label{color:var(--muted);font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.12em}.stat-value{font-size:21px;font-weight:850;margin-top:4px;letter-spacing:-.03em}.grid{display:grid;grid-template-columns:minmax(320px,.85fr) minmax(470px,1.4fr);gap:14px}.card{border-radius:18px;padding:18px}.card-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}.eyebrow{color:var(--orange);font-size:10px;font-weight:850;text-transform:uppercase;letter-spacing:.16em}h2,h3{margin:0;letter-spacing:-.02em}h2{font-size:17px}h3{font-size:14px}.guess-stage{min-height:350px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;position:relative;overflow:hidden}.guess-stage:before{content:"";position:absolute;width:260px;height:260px;border-radius:50%;border:1px solid rgba(212,154,0,.16);box-shadow:0 0 0 35px rgba(255,214,107,.10),0 0 0 70px rgba(255,214,107,.06)}.guess-stage>*{position:relative}.state{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.12em;margin-bottom:18px}.stimulus{width:145px;height:145px;display:grid;place-items:center;margin:4px auto 20px;filter:drop-shadow(0 16px 24px rgba(115,68,0,.2))}.shape{width:116px;height:116px;background:var(--shape-color,#e96b18)}.shape.circle{border-radius:50%}.shape.square{border-radius:15px}.shape.triangle{clip-path:polygon(50% 0,100% 100%,0 100%)}.shape.hexagon{clip-path:polygon(25% 7%,75% 7%,100% 50%,75% 93%,25% 93%,0 50%)}.shape.star{clip-path:polygon(50% 0,61% 34%,98% 35%,68% 56%,79% 94%,50% 72%,21% 94%,32% 56%,2% 35%,39% 34%)}.shape.x{background:none;color:var(--shape-color,#38280b);font-size:132px;line-height:1;font-weight:900;transform:translateY(-8px)}.guess-label{font-size:42px;font-weight:950;letter-spacing:-.05em}.guess-meta{color:var(--muted);font-size:13px;margin-top:7px}.confidence{height:7px;width:min(260px,80%);border-radius:999px;overflow:hidden;background:#f3e3b9;margin-top:17px}.confidence span{height:100%;display:block;background:linear-gradient(90deg,var(--yellow),var(--orange));border-radius:inherit}.waiting-mark{width:145px;height:145px;border:2px dashed #dfbd68;border-radius:50%;display:grid;place-items:center;color:var(--yellow);font-size:48px;font-weight:800;margin:5px auto 22px}
.erp-wrap{height:315px;display:flex;flex-direction:column}.erp-caption{color:var(--muted);font-size:11px;margin-top:8px}.erp-svg{width:100%;height:245px;overflow:visible}.erp-grid{stroke:rgba(130,106,60,.16);stroke-width:1}.erp-axis{stroke:rgba(130,106,60,.4);stroke-width:1}.erp-line{fill:none;stroke:url(#signalGradient);stroke-width:3;stroke-linecap:round;stroke-linejoin:round;filter:drop-shadow(0 0 4px rgba(233,107,24,.22))}.p3-zone{fill:rgba(255,214,107,.22)}.axis-label{fill:#826a3c;font-size:10px}
.history{grid-column:1/-1}.history-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(225px,1fr));gap:9px;margin-top:13px}.history-item{border:1px solid var(--line);background:#fff9ea;border-radius:13px;padding:12px;display:grid;grid-template-columns:42px 1fr auto;gap:10px;align-items:center}.mini-stim{width:38px;height:38px;display:grid;place-items:center}.mini-stim .shape{width:30px;height:30px;border-radius:6px}.mini-stim .shape.circle{border-radius:50%}.mini-stim .shape.x{font-size:38px;transform:translateY(-3px)}.history-name{font-size:13px;font-weight:800;text-transform:uppercase}.history-sub{color:var(--muted);font-size:10px;margin-top:3px}.result{font-size:10px;font-weight:900;text-transform:uppercase;letter-spacing:.1em}.ok{color:var(--green)}.miss{color:var(--red)}.empty{color:var(--muted);display:grid;place-items:center;min-height:120px;text-align:center}
.model-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}.model-card{border:1px solid var(--line);background:#fff9ea;border-radius:14px;padding:15px}.meter{height:7px;background:#f3e3b9;border-radius:9px;overflow:hidden;margin:12px 0}.meter span{display:block;height:100%;background:linear-gradient(90deg,var(--yellow),var(--orange))}table{border-collapse:collapse;width:100%;font-size:12px}td,th{padding:9px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.1em}
.hidden{display:none!important}@media(max-width:900px){.app{padding:16px}.stats{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.topbar{align-items:flex-start}.mode-badge{display:none}}
</style></head><body><main class="app">
<header class="topbar"><div class="brand"><div class="logo">E</div><div><h1 id="title">EEGle Realtime Classifier</h1><div class="subtitle">A live look at stimulus-locked brain-signal classification</div></div></div><div class="mode-badge"><span class="dot"></span><span id="modeText">Starting</span></div></header>
<div class="disclosure" id="disclosure"><strong>How this works</strong><span>Loading dashboard mode...</span></div>
<section class="stats" id="stats"></section>
<section id="demoView" class="grid hidden">
  <article class="card"><div class="card-title"><div><div class="eyebrow">Delayed guess</div><h2>What did the brain see?</h2></div><span class="status-pill" id="markerPill"><span class="dot"></span>LSL</span></div><div class="guess-stage" id="guessStage"></div></article>
  <article class="card erp-wrap"><div class="card-title"><div><div class="eyebrow">Signal window</div><h2>Illustrative event-related potential</h2></div><span class="status-pill">-200 to 800 ms</span></div><svg class="erp-svg" id="erpChart" viewBox="0 0 720 245"></svg><div class="erp-caption">The highlighted region is a common P300 analysis window. This demo waveform is illustrative and is not measured from EEG.</div></article>
  <article class="card history"><div class="card-title"><div><div class="eyebrow">Guess history</div><h2>Recent trials</h2></div><span class="status-pill" id="accuracyPill">Waiting</span></div><div class="history-list" id="demoHistory"></div></article>
</section>
<section id="classifierView" class="hidden"><div class="card"><div class="card-title"><div><div class="eyebrow">Live models</div><h2>Classifier predictions</h2></div></div><div class="model-grid" id="models"></div></div><div class="card" style="margin-top:14px"><div class="card-title"><h2>Recent predictions</h2></div><table><thead><tr><th>Trial</th><th>Model</th><th>Role</th><th>Prediction</th><th>P(NO-GO)</th><th>Latency</th></tr></thead><tbody id="rows"></tbody></table></div></section>
</main><script>
const esc=x=>String(x??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=x=>x==null?'waiting':`${Math.round(Number(x)*100)}%`;
function shapeHtml(s,mini=false){if(!s)return '<div class="waiting-mark">?</div>';const name=esc(s.shape||'circle'),color=esc(s.color||'orange');return `<div class="${mini?'mini-stim':'stimulus'}"><div class="shape ${name}" style="--shape-color:${color}">${name==='x'?'X':''}</div></div>`}
function stat(label,value){return `<div class="stat"><div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(value)}</div></div>`}
function erpSvg(windowData){const svg=document.getElementById('erpChart'),times=windowData?.times_ms||[],amps=windowData?.amplitude_uv||[];if(!times.length){svg.innerHTML='<text x="360" y="125" text-anchor="middle" class="axis-label">Waiting for the first illustrative window</text>';return}const x=t=>55+(t+200)/1000*630,min=Math.min(...amps,-8),max=Math.max(...amps,8),y=a=>205-(a-min)/(max-min)*175,path=times.map((t,i)=>`${i?'L':'M'} ${x(t).toFixed(1)} ${y(amps[i]).toFixed(1)}`).join(' ');let grid='';[-200,0,200,400,600,800].forEach(t=>grid+=`<line class="erp-grid" x1="${x(t)}" y1="25" x2="${x(t)}" y2="205"/><text class="axis-label" x="${x(t)}" y="225" text-anchor="middle">${t}</text>`);[-5,0,5].forEach(a=>grid+=`<line class="${a===0?'erp-axis':'erp-grid'}" x1="55" y1="${y(a)}" x2="685" y2="${y(a)}"/><text class="axis-label" x="43" y="${y(a)+3}" text-anchor="end">${a}</text>`);svg.innerHTML=`<defs><linearGradient id="signalGradient"><stop stop-color="#d49a00"/><stop offset="1" stop-color="#e96b18"/></linearGradient></defs><rect class="p3-zone" x="${x(300)}" y="25" width="${x(600)-x(300)}" height="180" rx="7"/>${grid}<path class="erp-line" d="${path}"/><text class="axis-label" x="370" y="242" text-anchor="middle">time from stimulus (ms)</text><text class="axis-label" x="14" y="118" transform="rotate(-90 14 118)" text-anchor="middle">amplitude (uV)</text>`}
function renderDemo(d){document.getElementById('demoView').classList.remove('hidden');document.getElementById('classifierView').classList.add('hidden');const p=d.latest;document.getElementById('stats').innerHTML=stat('LSL marker link',String(d.marker_status||'starting').replaceAll('_',' '))+stat('Guesses made',d.prediction_count)+stat('Demo accuracy',pct(d.accuracy))+stat('Prediction delay',`${Number(d.prediction_delay_seconds||0).toFixed(1)} s`);document.getElementById('markerPill').innerHTML=`<span class="dot"></span>${esc(String(d.marker_status||'starting').replaceAll('_',' '))}`;if(p){const g=p.guessed_stimulus||{};document.getElementById('guessStage').innerHTML=`<div class="state">Latest prediction, trial ${esc(p.trial)}</div>${shapeHtml(g)}<div class="guess-label">${esc(String(p.predicted_condition).replace('_','-').toUpperCase())}</div><div class="guess-meta">${esc(g.color)} ${esc(g.shape)} | ${pct(p.confidence)} confidence</div><div class="confidence"><span style="width:${Number(p.confidence||0)*100}%"></span></div><div class="guess-meta ${p.is_correct?'ok':'miss'}">${p.is_correct?'Matched the marker':'Intentional demo miss'}</div>`}else{document.getElementById('guessStage').innerHTML=`<div class="state">Waiting for the first delayed prediction</div>${shapeHtml(null)}<div class="guess-label" style="font-size:24px">Ready to guess</div><div class="guess-meta">Each guess will remain here until the next one arrives.</div>`}erpSvg(p?.erp_window);document.getElementById('accuracyPill').textContent=d.accuracy==null?'Waiting':`${d.correct_count} / ${d.prediction_count} correct`;document.getElementById('demoHistory').innerHTML=(d.predictions||[]).slice(-12).reverse().map(row=>{const g=row.guessed_stimulus||{},a=row.actual_stimulus||{};return `<div class="history-item">${shapeHtml(g,true)}<div><div class="history-name">${esc(String(row.predicted_condition).replace('_','-'))}</div><div class="history-sub">Trial ${esc(row.trial)} | saw ${esc(a.color)} ${esc(a.shape)}</div></div><div class="result ${row.is_correct?'ok':'miss'}">${row.is_correct?'match':'miss'}</div></div>`}).join('')||'<div class="empty">Guesses will appear here after the first stimulus.</div>'}
function renderClassifier(d){document.getElementById('classifierView').classList.remove('hidden');document.getElementById('demoView').classList.add('hidden');const agreement=d.primary_shadow_agreement?.rate;document.getElementById('stats').innerHTML=stat('Predictions',d.prediction_count)+stat('Rejected epochs',d.rejected_epoch_count)+stat('Scored trials',d.truth_count)+stat('Model agreement',pct(agreement));document.getElementById('models').innerHTML=(d.model_ids||[]).map(id=>{const p=d.latest[id]||{},m=d.metrics[id]||{},v=Number(p.probability_no_go||0);return `<div class="model-card"><div class="eyebrow">${esc(id)}</div><div class="guess-label" style="font-size:27px;margin-top:8px">${esc(p.predicted_condition||'waiting')}</div><div class="meter"><span style="width:${v*100}%"></span></div><div class="history-sub">P(NO-GO) ${v.toFixed(3)} | balanced accuracy ${m.balanced_accuracy==null?'waiting':Number(m.balanced_accuracy).toFixed(3)} | mean latency ${m.mean_latency_ms==null?'waiting':Number(m.mean_latency_ms).toFixed(1)+' ms'}</div></div>`}).join('');document.getElementById('rows').innerHTML=(d.predictions||[]).slice(-30).reverse().map(p=>`<tr><td>${esc(p.trial)}</td><td>${esc(p.model_id)}</td><td>${esc(p.model_role)}</td><td>${esc(p.predicted_condition)}</td><td>${Number(p.probability_no_go||0).toFixed(3)}</td><td>${Number(p.processing_latency_ms||0).toFixed(1)} ms</td></tr>`).join('')}
async function refresh(){try{const d=await fetch('/api/snapshot',{cache:'no-store'}).then(r=>r.json());document.getElementById('title').textContent=d.title||'EEGle Realtime Classifier';document.getElementById('modeText').textContent=d.mode==='demo'?'LSL marker demo':'Live EEG classifier';document.getElementById('disclosure').innerHTML=d.mode==='demo'?`<span>${esc(d.disclosure||'')}</span>`:`<strong>How this works</strong><span>${esc(d.disclosure||'')}</span>`;d.mode==='demo'?renderDemo(d):renderClassifier(d)}catch(e){document.getElementById('modeText').textContent='Reconnecting'}}refresh();setInterval(refresh,250);
</script></body></html>"""


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
