"""Pure-Python IRI API client configured from a YAML file.

Config file format (YAML):

    base_url: https://api.iri.nersc.gov   # optional, defaults to NERSC server
    access_token: <your_token>            # required for authenticated endpoints
    resource_id: <default_resource_id>    # optional default for all operations

Usage:

    from client import Client

    c = Client("~/.iri.yaml")
    print(c.stat("/global/cfs/cdirs/m1234"))
    entries = c.ls("/global/cfs/cdirs/m1234")
    c.download("/global/cfs/cdirs/m1234/data.h5", "data.h5")
    c.upload("results.tar.gz", "/global/cfs/cdirs/m1234/results.tar.gz")
    job = c.launch_job(job_spec)
    status = c.get_job(job["id"])
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests
import yaml

DEFAULT_BASE_URL = "https://api.iri.nersc.gov"


class IriClientError(Exception):
    pass


class Client:
    """Synchronous IRI API client backed by a YAML config file."""

    def __init__(self, config_path: str | Path) -> None:
        config = _load_config(config_path)
        self._base_url = config.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self._resource_id: str | None = config.get("resource_id")
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        token: str | None = config.get("access_token")
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------

    def launch_job(self, job_spec: dict, *, resource_id: str | None = None) -> dict:
        """Submit a job to a compute resource.

        POST /api/v1/compute/job/{resource_id}

        Args:
            job_spec: Job specification dict (executable, arguments, resources, etc.).
            resource_id: Compute resource ID. Falls back to config ``resource_id``.

        Returns:
            Created job object including ``id``.
        """
        rid = self._resource(resource_id)
        resp = self._session.post(
            f"{self._base_url}/api/v1/compute/job/{_encode(rid)}",
            json=job_spec,
        )
        return _json_response(resp)

    def get_job(self, job_id: str, *, resource_id: str | None = None) -> dict:
        """Get status of a submitted job.

        GET /api/v1/compute/status/{resource_id}/{job_id}

        Args:
            job_id: Job identifier returned by :meth:`launch_job`.
            resource_id: Compute resource ID. Falls back to config ``resource_id``.

        Returns:
            Job object with status information.
        """
        rid = self._resource(resource_id)
        resp = self._session.get(
            f"{self._base_url}/api/v1/compute/status/{_encode(rid)}/{_encode(job_id)}",
        )
        return _json_response(resp)

    # ------------------------------------------------------------------
    # Filesystem
    # ------------------------------------------------------------------

    def stat(
        self,
        path: str,
        *,
        resource_id: str | None = None,
        dereference: bool = False,
    ) -> dict:
        """Get metadata for a file or directory.

        GET /api/v1/filesystem/stat/{resource_id}?path=...

        Args:
            path: Absolute path on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
            dereference: If ``True``, follow symbolic links.

        Returns:
            Stat object (name, size, type, permissions, owner, …).
        """
        rid = self._resource(resource_id)
        params: dict[str, str] = {"path": path}
        if dereference:
            params["dereference"] = "true"
        resp = self._session.get(
            f"{self._base_url}/api/v1/filesystem/stat/{_encode(rid)}",
            params=params,
        )
        return _json_response(resp)

    def ls(
        self,
        path: str,
        *,
        resource_id: str | None = None,
        show_hidden: bool = False,
        numeric_uid: bool = False,
        recursive: bool = False,
        dereference: bool = False,
    ) -> dict:
        """List directory contents.

        GET /api/v1/filesystem/ls/{resource_id}?path=...

        Args:
            path: Absolute path to the directory on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
            show_hidden: Include entries whose name begins with ``'.'``.
            numeric_uid: Show numeric UID/GID instead of names.
            recursive: List subdirectories recursively.
            dereference: Follow symbolic links.

        Returns:
            Directory listing object.
        """
        rid = self._resource(resource_id)
        params: dict[str, str] = {"path": path}
        if show_hidden:
            params["showHidden"] = "true"
        if numeric_uid:
            params["numericUid"] = "true"
        if recursive:
            params["recursive"] = "true"
        if dereference:
            params["dereference"] = "true"
        resp = self._session.get(
            f"{self._base_url}/api/v1/filesystem/ls/{_encode(rid)}",
            params=params,
        )
        return _json_response(resp)

    def download(
        self,
        remote_path: str,
        local_dest: str | Path,
        *,
        resource_id: str | None = None,
    ) -> None:
        """Download a file from the remote filesystem.

        GET /api/v1/filesystem/download/{resource_id}?path=...

        Args:
            remote_path: Absolute path to the file on the remote filesystem.
            local_dest: Local destination path for the downloaded file.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
        """
        rid = self._resource(resource_id)
        resp = self._session.get(
            f"{self._base_url}/api/v1/filesystem/download/{_encode(rid)}",
            params={"path": remote_path},
            stream=True,
        )
        _raise_for_error(resp)
        with open(local_dest, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

    def upload(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        resource_id: str | None = None,
    ) -> dict:
        """Upload a local file to the remote filesystem.

        POST /api/v1/filesystem/upload/{resource_id}?path=...

        Uses ``multipart/form-data`` with the field name ``file``.

        Args:
            local_path: Local path of the file to upload.
            remote_path: Destination absolute path on the remote filesystem.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.

        Returns:
            Task or result object returned by the API.
        """
        rid = self._resource(resource_id)
        with open(local_path, "rb") as fh:
            resp = self._session.post(
                f"{self._base_url}/api/v1/filesystem/upload/{_encode(rid)}",
                params={"path": remote_path},
                files={"file": fh},
            )
        return _json_response(resp)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resource(self, resource_id: str | None) -> str:
        rid = resource_id or self._resource_id
        if not rid:
            raise IriClientError(
                "resource_id is required; provide it as a keyword argument "
                "or set 'resource_id' in the config file"
            )
        return rid


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _load_config(path: str | Path) -> dict:
    with open(path) as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise IriClientError(f"Config file '{path}' must be a YAML mapping")
    return data


def _encode(segment: str) -> str:
    return quote(str(segment), safe="")


def _json_response(resp: requests.Response) -> dict:
    _raise_for_error(resp)
    if not resp.content:
        return {}
    return resp.json()


def _raise_for_error(resp: requests.Response) -> None:
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise IriClientError(f"HTTP {resp.status_code}: {detail}")
