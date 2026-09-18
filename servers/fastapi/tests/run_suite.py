"""Run the FastAPI test suite with an isolated, cross-platform runtime."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gslide-tests-") as runtime_directory:
        runtime_path = Path(runtime_directory)
        app_data_directory = runtime_path / "app-data"
        temp_directory = runtime_path / "temp"
        app_data_directory.mkdir()
        temp_directory.mkdir()

        os.environ["APP_DATA_DIRECTORY"] = str(app_data_directory)
        os.environ["TEMP_DIRECTORY"] = str(temp_directory)
        os.environ["DATABASE_URL"] = (
            f"sqlite+aiosqlite:///{(runtime_path / 'gslide-test.db').as_posix()}"
        )
        os.environ.setdefault("DISABLE_IMAGE_GENERATION", "true")

        return pytest.main(sys.argv[1:] or ["-q"])


if __name__ == "__main__":
    raise SystemExit(main())
