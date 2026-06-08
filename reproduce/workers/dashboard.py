"""Localhost-only live dashboard for classifier predictions."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

from reproduce.config import load_config
from reproduce.realtime.models import binary_classification_metrics
from reproduce.session import paths_for_existing_session
from reproduce.telemetry import Telemetry
from reproduce.workers.common import StatusWriter, install_stop_signal_handlers


def dashboard_snapshot(session_dir: str | Path) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
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
    statuses = {}
    process_dir = root / "logs" / "processes"
    for path in process_dir.glob("*.status.json"):
        statuses[path.stem.removesuffix(".status")] = _load_json(path)
    predicted = [row for row in predictions if row.get("status") == "predicted"]
    rejected = [row for row in predictions if row.get("status") == "rejected"]
    agreement = _primary_shadow_agreement(predicted)
    return {
        "schema_version": 1,
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
    handler = _handler_for(paths.root)
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
        server.timeout = 0.25
    except Exception as exc:
        status.update("failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    status.update("running", url=f"http://{args.host}:{args.port}", auto_open=False)
    try:
        while not stop_event.is_set():
            server.handle_request()
    finally:
        server.server_close()
    status.update("stopped", url=f"http://{args.host}:{args.port}")
    return 0


def _handler_for(session_dir: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/snapshot":
                self._send("application/json", json.dumps(dashboard_snapshot(session_dir)).encode("utf-8"))
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
<html><head><meta charset="utf-8"><title>EEGle Classifier Dashboard</title>
<style>
body{font-family:system-ui;background:#10131a;color:#e8edf5;margin:20px}h1{font-size:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.card{background:#1a2030;border:1px solid #34405a;border-radius:8px;padding:12px}
.prob{height:10px;background:#283247;border-radius:5px;overflow:hidden}.prob span{display:block;height:100%;background:#57a6ff}
small{color:#9eabc1}table{border-collapse:collapse;width:100%}td,th{padding:5px;border-bottom:1px solid #34405a;text-align:left}
</style></head><body><h1>Realtime GO / NO-GO EEG Classification</h1><div id="summary"></div><div class="grid" id="models"></div>
<h2>Recent Predictions</h2><table><thead><tr><th>Trial</th><th>Model</th><th>Role</th><th>Prediction</th><th>P(NO-GO)</th><th>Latency</th></tr></thead><tbody id="rows"></tbody></table>
<script>
const esc=x=>String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function refresh(){const d=await fetch('/api/snapshot').then(r=>r.json());
const processState=Object.entries(d.processes||{}).map(([k,v])=>`${esc(k)}=${esc(v?.status||'missing')}`).join(', ');
const agreement=d.primary_shadow_agreement?.rate;
document.getElementById('summary').innerHTML=`<p>${d.prediction_count} predictions, ${d.rejected_epoch_count} rejected epochs, ${d.truth_count} scored trials, primary/shadow agreement ${agreement==null?'waiting':Number(agreement).toFixed(3)}</p><small>${processState}</small>`;
document.getElementById('models').innerHTML=d.model_ids.map(id=>{const p=d.latest[id]||{},m=d.metrics[id]||{},v=Number(p.probability_no_go||0);
return `<div class="card"><h3>${esc(id)}</h3><strong>${esc(p.predicted_condition||'waiting')}</strong><div class="prob"><span style="width:${v*100}%"></span></div><small>P(NO-GO) ${v.toFixed(3)}</small><p>Balanced accuracy: ${m.balanced_accuracy==null?'waiting':Number(m.balanced_accuracy).toFixed(3)}<br>Coverage: ${m.coverage==null?'waiting':Number(m.coverage).toFixed(3)}<br>Confusion: ${esc(JSON.stringify(m.confusion_matrix||'waiting'))}<br>Mean latency: ${m.mean_latency_ms==null?'waiting':Number(m.mean_latency_ms).toFixed(1)+' ms'}</p></div>`}).join('');
document.getElementById('rows').innerHTML=d.predictions.slice(-30).reverse().map(p=>`<tr><td>${esc(p.trial)}</td><td>${esc(p.model_id)}</td><td>${esc(p.model_role)}</td><td>${esc(p.predicted_condition)}</td><td>${Number(p.probability_no_go||0).toFixed(3)}</td><td>${Number(p.processing_latency_ms||0).toFixed(1)} ms</td></tr>`).join('');
}refresh();setInterval(refresh,1000);
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
