"""The application under repair: how to build, run, probe and verify it. Defaults to the Quarkus demo in target.json; set TARGET_CONFIG to another file."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = json.loads(Path(os.getenv("TARGET_CONFIG", ROOT / "target.json")).read_text())
