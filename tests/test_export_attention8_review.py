import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "export_attention8_review.py"


class Attention8ReviewExportTests(unittest.TestCase):
    def test_exports_matched_sessions_without_forensic_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_root = root / "data"
            calibration = self._session(session_root, "sub-cal", "run-20260714T100000")
            online = self._session(session_root, "sub-online", "run-20260714T120000")

            model = calibration / "models" / "attention8" / "causal_bandpower_logreg"
            model.mkdir(parents=True)
            self._json(model / "manifest.json", {"bundle_hash": "matched-hash", "kind": "causal_bandpower_logreg"})
            self._json(model / "metrics.json", {"balanced_accuracy": 0.75})
            (model / "model.joblib").write_bytes(b"model")
            (calibration / "raw" / "eeg.csv").parent.mkdir(parents=True)
            (calibration / "raw" / "eeg.csv").write_text("sample\n1\n", encoding="utf-8")
            self._json(calibration / "realtime" / "epochs" / "manifest.json", {"epoch_count": 200})
            (calibration / "realtime" / "epochs" / "epochs.npz").write_bytes(b"epochs")

            self._json(
                online / "parameters.json",
                {
                    "realtime": {
                        "model": {
                            "kind": "causal_bandpower_logreg",
                            "bundle_path": str(model),
                        }
                    }
                },
            )
            predictions = online / "realtime" / "model_predictions.jsonl"
            predictions.parent.mkdir(parents=True, exist_ok=True)
            predictions.write_text('{"status":"predicted"}\n', encoding="utf-8")
            self._json(
                online / "realtime" / "models" / "primary-causal" / "manifest.json",
                {"bundle_hash": "matched-hash", "kind": "causal_bandpower_logreg"},
            )
            self._json(
                online / "realtime" / "adaptation_state" / "primary.json",
                {"support_size": 50},
            )
            (online / "realtime" / "engine_input.bin").write_bytes(b"capture")
            self._json(online / "reports" / "classification" / "metrics.json", {"status": "ok"})

            # A prior export must never be considered a source session.
            stale = session_root / "review_exports" / "attention8-review-newer" / "online"
            stale.mkdir(parents=True)
            self._json(stale / "parameters.json", {})
            (stale / "realtime").mkdir()
            (stale / "realtime" / "model_predictions.jsonl").write_text("{}\n", encoding="utf-8")

            output = root / "exports"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--session-root",
                    str(session_root),
                    "--output-dir",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            bundles = list(output.glob("attention8-review-*"))
            folders = [path for path in bundles if path.is_dir()]
            archives = [path for path in bundles if path.suffix == ".zip"]
            self.assertEqual(len(folders), 1)
            self.assertEqual(len(archives), 1)
            bundle = folders[0]
            summary = json.loads((bundle / "review_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(Path(summary["calibration"]["session"]), calibration.resolve())
            self.assertEqual(Path(summary["online"]["session"]), online.resolve())
            self.assertTrue((bundle / "online" / "realtime" / "adaptation_state" / "primary.json").is_file())
            self.assertTrue(
                (
                    bundle
                    / "calibration"
                    / "models"
                    / "attention8"
                    / "causal_bandpower_logreg"
                    / "manifest.json"
                ).is_file()
            )
            self.assertFalse((bundle / "calibration" / "raw" / "eeg.csv").exists())
            self.assertFalse((bundle / "calibration" / "realtime" / "epochs" / "epochs.npz").exists())
            self.assertFalse((bundle / "online" / "realtime" / "engine_input.bin").exists())
            self.assertFalse(
                (
                    bundle
                    / "calibration"
                    / "models"
                    / "attention8"
                    / "causal_bandpower_logreg"
                    / "model.joblib"
                ).exists()
            )
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertIn(
                    f"{bundle.name}/review_summary.json",
                    archive.namelist(),
                )

    @staticmethod
    def _session(root: Path, participant: str, run: str) -> Path:
        session = root / "participants" / participant / "sessions" / "2026-07-14" / "attention8" / "go_nogo" / run
        session.mkdir(parents=True)
        Attention8ReviewExportTests._json(session / "parameters.json", {})
        Attention8ReviewExportTests._json(session / "session_summary.json", {"status": "complete"})
        return session

    @staticmethod
    def _json(path: Path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
