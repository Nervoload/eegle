"""Build the external plugin fixture wheel using only the standard library."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import zipfile


DIST_NAME = "eegle_phase2_external_fixture"
VERSION = "1.2.0"


def build_external_plugin_wheel(source_root: Path, destination: Path) -> Path:
    """Create a valid, platform-independent wheel for installation tests."""

    module = (
        source_root / "phase2_external_plugin" / "__init__.py"
    ).read_bytes()
    dist_info = f"{DIST_NAME}-{VERSION}.dist-info"
    files: dict[str, bytes] = {
        "phase2_external_plugin/__init__.py": module,
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.3\n"
            "Name: eegle-phase2-external-fixture\n"
            f"Version: {VERSION}\n"
            "Requires-Python: >=3.11\n"
            "Requires-Dist: eegle\n"
            "\n"
        ).encode("utf-8"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: eegle-phase2-fixture\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
            "\n"
        ).encode("utf-8"),
        f"{dist_info}/entry_points.txt": (
            "[eegle.plugins]\n"
            "fixture-scale = phase2_external_plugin:plugin\n"
        ).encode("utf-8"),
        f"{dist_info}/top_level.txt": b"phase2_external_plugin\n",
    }
    record_path = f"{dist_info}/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for path in sorted(files):
        digest = base64.urlsafe_b64encode(hashlib.sha256(files[path]).digest()).rstrip(b"=")
        writer.writerow((path, f"sha256={digest.decode('ascii')}", len(files[path])))
    writer.writerow((record_path, "", ""))
    files[record_path] = record.getvalue().encode("utf-8")

    destination.mkdir(parents=True, exist_ok=True)
    wheel = destination / f"{DIST_NAME}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    return wheel
