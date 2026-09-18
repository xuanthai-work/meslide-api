import os
from typing import Optional, Union

from fastapi import HTTPException

from utils.get_env import default_gslide_directory, get_temp_directory_env
from api.v1.auth.context import get_current_owner_id_raw
import uuid


class TempFileService:

    def __init__(self):
        self.base_dir = get_temp_directory_env() or default_gslide_directory()
        # Root wipe is a process-start action (no request owner). Lazy first
        # use during a request has an owner ContextVar; running the wipe then
        # treats the process root as an owner-scoped path and 400s, and would
        # also delete other in-flight uploads.
        if get_current_owner_id_raw() is None:
            self.cleanup_base_dir()
        os.makedirs(self.base_dir, exist_ok=True)

    def _base_dir_realpath(self) -> str:
        return os.path.realpath(self.base_dir)

    def _owner_base_dir_realpath(self) -> str:
        owner_id = get_current_owner_id_raw()
        if owner_id is None:
            return self._base_dir_realpath()
        owner_dir = os.path.realpath(os.path.join(self.base_dir, str(owner_id)))
        root_dir = self._base_dir_realpath()
        if not owner_dir.startswith(f"{root_dir}{os.sep}"):
            raise HTTPException(
                status_code=400,
                detail="Directory path must stay within the temp directory",
            )
        os.makedirs(owner_dir, exist_ok=True)
        return owner_dir

    def _is_within_base_dir(self, path: str) -> bool:
        base_dir = self._owner_base_dir_realpath()
        return path == base_dir or path.startswith(f"{base_dir}{os.sep}")

    def _assert_within_base_dir(self, path: str, detail: str):
        if not self._is_within_base_dir(path):
            raise HTTPException(status_code=400, detail=detail)

    def sanitize_upload_filename(self, file_name: Optional[str]) -> str:
        normalized_name = (file_name or "").replace("\\", "/")
        safe_name = os.path.basename(normalized_name).strip()
        if not safe_name or safe_name in {".", ".."}:
            raise HTTPException(status_code=400, detail="Invalid filename")
        return safe_name

    def resolve_temp_path(self, file_path: str, must_exist: bool = False) -> str:
        if not isinstance(file_path, str) or not file_path.strip():
            raise HTTPException(status_code=400, detail="Invalid file path")

        try:
            resolved_path = os.path.realpath(os.path.abspath(file_path))
        except OSError as exc:
            raise HTTPException(status_code=404, detail="File not found") from exc

        base_dir = self._owner_base_dir_realpath()
        if not (
            resolved_path == base_dir
            or resolved_path.startswith(f"{base_dir}{os.sep}")
        ):
            raise HTTPException(
                status_code=400,
                detail="File path must stay within the temp directory",
            )

        if must_exist and not os.path.exists(resolved_path):
            raise HTTPException(status_code=404, detail="File not found")

        return resolved_path

    def resolve_existing_temp_paths(self, file_paths: Optional[list[str]]) -> list[str]:
        if not file_paths:
            return []
        return [
            self.resolve_temp_path(file_path, must_exist=True) for file_path in file_paths
        ]

    def create_dir_in_dir(self, base_dir: str, dir_name: Optional[str] = None) -> str:
        temp_dir_name = (
            self.sanitize_upload_filename(dir_name) if dir_name else str(uuid.uuid4())
        )
        temp_dir = os.path.join(base_dir, temp_dir_name)
        temp_dir = os.path.realpath(temp_dir)
        self._assert_within_base_dir(
            temp_dir, "Directory path must stay within the temp directory"
        )
        os.makedirs(temp_dir, exist_ok=True)
        return temp_dir

    def create_temp_dir(self, dir_name: Optional[str] = None) -> str:
        return self.create_dir_in_dir(self._owner_base_dir_realpath(), dir_name)

    def create_temp_file_path(
        self, file_path: str, dir_path: Optional[str] = None
    ) -> str:
        if dir_path is None:
            dir_path = self._owner_base_dir_realpath()

        safe_name = self.sanitize_upload_filename(file_path)
        resolved_dir = os.path.realpath(dir_path)
        self._assert_within_base_dir(
            resolved_dir, "Directory path must stay within the temp directory"
        )

        full_path = os.path.join(resolved_dir, safe_name)
        full_path = os.path.realpath(full_path)
        self._assert_within_base_dir(
            full_path, "File path must stay within the temp directory"
        )

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        return full_path

    def create_temp_file(
        self,
        file_path: str,
        content: Union[bytes, str],
        dir_path: Optional[str] = None,
    ) -> str:
        file_path = self.create_temp_file_path(file_path, dir_path)
        if isinstance(content, bytes):
            with open(file_path, "wb") as f:
                f.write(content)
        else:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

        return file_path

    def read_temp_file(self, file_path: str, binary: bool = True) -> Union[bytes, str]:
        file_path = self.resolve_temp_path(file_path, must_exist=True)
        base_dir = self._owner_base_dir_realpath()
        if not (file_path == base_dir or file_path.startswith(f"{base_dir}{os.sep}")):
            raise HTTPException(
                status_code=400,
                detail="File path must stay within the temp directory",
            )
        if binary:
            with open(file_path, "rb") as f:
                return f.read()
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    async def update_temp_file_from_upload(self, file_path: str, upload_file) -> None:
        if not isinstance(file_path, str) or not file_path.strip():
            raise HTTPException(status_code=400, detail="Invalid file path")

        base_dir = self._owner_base_dir_realpath()
        normalized_path = os.path.realpath(os.path.abspath(file_path))
        if not normalized_path.startswith(base_dir):
            raise HTTPException(
                status_code=400,
                detail="File path must stay within the temp directory",
            )
        self._assert_within_base_dir(
            normalized_path, "File path must stay within the temp directory"
        )

        with open(normalized_path, "wb") as f:
            f.write(await upload_file.read())

    def cleanup_temp_file(self, file_path: str):
        try:
            file_path = self.resolve_temp_path(file_path, must_exist=True)
        except HTTPException as exc:
            if exc.status_code == 404:
                return
            raise
        os.remove(file_path)

    def _delete_dir_files(self, dir_path: str):
        dir_path = self.resolve_temp_path(dir_path, must_exist=True)
        for root, dirs, files in os.walk(dir_path, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))

    def cleanup_temp_dir(self, dir_path: str):
        try:
            dir_path = self.resolve_temp_path(dir_path, must_exist=True)
        except HTTPException as exc:
            if exc.status_code == 404:
                return
            raise
        self._delete_dir_files(dir_path)
        os.rmdir(dir_path)

    def cleanup_base_dir(self):
        """Wipe contents of the process-wide temp root.

        Must not go through the owner-scoped resolver: the process root is
        the parent of per-owner directories, so that check would 400.
        """
        base_dir = os.path.realpath(self.base_dir)
        if not os.path.isdir(base_dir):
            return
        for root, dirs, files in os.walk(base_dir, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))


_temp_file_service_instance: Optional[TempFileService] = None


def get_temp_file_service() -> TempFileService:
    """Return the process-wide TempFileService, constructing it on first call.

    The constructor cleans and creates the base directory, so the service
    must not be built at import time: importing this module (directly or via
    any consumer) has to stay free of filesystem side effects, and the base
    directory must be resolvable per process from TEMP_DIRECTORY rather
    than at interpreter startup.
    """
    global _temp_file_service_instance
    if _temp_file_service_instance is None:
        _temp_file_service_instance = TempFileService()
    return _temp_file_service_instance


class _LazyTempFileService:
    """Compatibility adapter for the ``TEMP_FILE_SERVICE`` import surface.

    Consumers still do ``from services.temp_file_service import
    TEMP_FILE_SERVICE`` and call methods on it; reads and writes are
    forwarded to the lazily constructed singleton. Remove this adapter in a
    dedicated PR once every caller has moved to ``get_temp_file_service()``.
    """

    def __getattr__(self, name):
        return getattr(get_temp_file_service(), name)

    def __setattr__(self, name, value):
        setattr(get_temp_file_service(), name, value)


TEMP_FILE_SERVICE = _LazyTempFileService()
