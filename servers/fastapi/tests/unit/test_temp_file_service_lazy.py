"""Unit tests for the lazy TempFileService seam.

Importing ``services.temp_file_service`` used to construct a
``TempFileService`` immediately: the constructor cleaned and created the
base directory, so any import had filesystem side effects and the default
``/tmp/gslide`` root broke on Windows. These tests pin the new contract:

- importing the module creates nothing on disk,
- the service is constructed on first use (lazy factory),
- the module-level ``TEMP_FILE_SERVICE`` adapter keeps delegating to the
  process-wide instance so existing consumers stay unchanged,
- the default root lives under ``tempfile.gettempdir()``.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from services import temp_file_service
from services.temp_file_service import TEMP_FILE_SERVICE

SERVERS_FASTAPI_ROOT = Path(__file__).resolve().parents[2]


def test_cold_import_does_not_create_temp_directory():
    """A fresh interpreter importing the module must not touch the filesystem."""
    code = textwrap.dedent(
        """
        import os
        import shutil
        import sys
        import tempfile

        root = sys.argv[1]
        candidate = os.path.join(tempfile.gettempdir(), "gslide-import-probe")
        shutil.rmtree(candidate, ignore_errors=True)

        os.environ["TEMP_DIRECTORY"] = candidate
        sys.path.insert(0, root)

        import services.temp_file_service as module

        assert not os.path.exists(candidate), (
            "importing the module must not create the temp directory"
        )

        module.get_temp_file_service()

        assert os.path.exists(candidate), (
            "first use of the factory must create the temp directory"
        )
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(SERVERS_FASTAPI_ROOT)],
        capture_output=True,
        text=True,
        env={**os.environ, "TEMP_DIRECTORY": ""},
    )
    assert result.returncode == 0, result.stderr


def test_factory_returns_process_wide_singleton():
    first = temp_file_service.get_temp_file_service()
    second = temp_file_service.get_temp_file_service()
    assert first is second


def test_lazy_proxy_delegates_reads_and_writes():
    service = temp_file_service.get_temp_file_service()
    # Reads forward to the singleton...
    assert TEMP_FILE_SERVICE.base_dir == service.base_dir
    # ...and writes do too (existing tests monkeypatch base_dir on the proxy).
    TEMP_FILE_SERVICE.base_dir = service.base_dir
    assert service.base_dir == TEMP_FILE_SERVICE.base_dir


def test_default_base_dir_lives_under_platform_temp_root(monkeypatch):
    monkeypatch.delenv("TEMP_DIRECTORY", raising=False)
    service = temp_file_service.TempFileService()
    expected = os.path.join(tempfile.gettempdir(), "gslide")
    assert service.base_dir == expected
    assert os.path.isdir(service.base_dir)


def test_temp_directory_env_still_overrides_default(monkeypatch, tmp_path):
    custom = tmp_path / "custom-temp"
    monkeypatch.setenv("TEMP_DIRECTORY", str(custom))
    service = temp_file_service.TempFileService()
    assert service.base_dir == str(custom)
    assert os.path.isdir(custom)


def test_constructing_service_during_a_request_does_not_400(monkeypatch, tmp_path):
    """Regression: lazy init used to 400 outline/stream.

    ``cleanup_base_dir`` ran through the owner-scoped path check, so the
    process root (``/tmp/gslide``) looked like an escape from
    ``/tmp/gslide/<owner>``.
    """
    import uuid

    from api.v1.auth.context import reset_current_owner_id, set_current_owner_id
    from fastapi import HTTPException

    custom = tmp_path / "request-temp"
    monkeypatch.setenv("TEMP_DIRECTORY", str(custom))
    token = set_current_owner_id(uuid.uuid4())
    try:
        service = temp_file_service.TempFileService()
        created = service.create_temp_dir()
    except HTTPException as exc:
        raise AssertionError(
            f"constructing TempFileService during a request raised {exc.status_code}: {exc.detail}"
        ) from exc
    finally:
        reset_current_owner_id(token)

    assert os.path.isdir(created)
    assert os.path.realpath(created).startswith(
        os.path.realpath(str(custom)) + os.sep
    )


def test_default_gslide_directory_helper_is_cross_platform():
    from utils.get_env import default_gslide_directory

    assert default_gslide_directory() == os.path.join(tempfile.gettempdir(), "gslide")