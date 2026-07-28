"""Build the independent Phase 7 scikit-learn adapter fixture wheel."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import zipfile


DIST_NAME = "eegle_phase7_sklearn_fixture"
VERSION = "1.0.0"


def build_phase7_sklearn_wheel(source_root: Path, destination: Path) -> Path:
    module = (source_root / "phase7_sklearn_model" / "__init__.py").read_bytes()
    dist_info = f"{DIST_NAME}-{VERSION}.dist-info"
    files: dict[str, bytes] = {
        "phase7_sklearn_model/__init__.py": module,
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.3\n"
            "Name: eegle-phase7-sklearn-fixture\n"
            f"Version: {VERSION}\n"
            "Requires-Python: >=3.11\n"
            "Requires-Dist: eegle\n"
            "Requires-Dist: joblib>=1.5,<2\n"
            "Requires-Dist: numpy>=1.26,<3\n"
            "Requires-Dist: scikit-learn>=1.7,<2\n\n"
        ).encode("utf-8"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: eegle-phase7-fixture\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n\n"
        ).encode("utf-8"),
        f"{dist_info}/entry_points.txt": (
            "[eegle.plugins]\n"
            "sklearn-estimator = phase7_sklearn_model:plugin\n"
        ).encode("utf-8"),
        f"{dist_info}/top_level.txt": b"phase7_sklearn_model\n",
    }
    record_path = f"{dist_info}/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for path in sorted(files):
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(files[path]).digest()
        ).rstrip(b"=")
        writer.writerow((path, f"sha256={digest.decode('ascii')}", len(files[path])))
    writer.writerow((record_path, "", ""))
    files[record_path] = record.getvalue().encode("utf-8")

    destination.mkdir(parents=True, exist_ok=True)
    wheel = destination / f"{DIST_NAME}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    return wheel
