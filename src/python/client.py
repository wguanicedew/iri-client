"""Pure-Python IRI API client configured from a YAML file.

Config file format (YAML):

    base_url: https://api.iri.nersc.gov   # optional, defaults to NERSC server
    access_token: <your_token>            # required for authenticated endpoints
    resource_id: <default_resource_id>    # optional default for all operations

Config resolution order (when no path is passed to Client()):
    1. $IRI_CLIENT_CONFIG environment variable
    2. ~/.iri.yaml

Usage:

    from client import Client

    c = Client()                 # auto-discover config
    c = Client("~/.iri.yaml")   # explicit path
    print(c.stat("/global/cfs/cdirs/m1234"))
    entries = c.ls("/global/cfs/cdirs/m1234")
    c.download("/global/cfs/cdirs/m1234/data.h5", "data.h5")
    c.upload("results.tar.gz", "/global/cfs/cdirs/m1234/results.tar.gz")
    job = c.launch_job(job_spec)
    status = c.get_job(job["id"])
"""

from __future__ import annotations

import json as _json
import os
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
import yaml

DEFAULT_BASE_URL = "https://api.iri.nersc.gov"
_DEFAULT_CONFIG_PATH = Path.home() / ".iri.yaml"
_TASK_TERMINAL_STATES = {"completed", "failed", "canceled"}
_TASK_POLL_INTERVAL = 5   # seconds between task status polls
_TASK_MAX_POLLS = 60      # ~5 minutes at 5s interval


def _stream_to_file(resp: requests.Response, dest: str | Path) -> None:
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=65536):
            fh.write(chunk)


class IriClientError(Exception):
    pass


class Client:
    """Synchronous IRI API client backed by a YAML config file."""

    def __init__(self, config_path: str | Path | None = None, *, debug: bool = False) -> None:
        config = _load_config(_resolve_config_path(config_path))
        self._base_url = config.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self._resource_id: str | None = config.get("resource_id")
        self._debug = debug
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
        url = f"{self._base_url}/api/v1/compute/job/{_encode(rid)}"
        self._curl("POST", url, json_body=job_spec)
        resp = self._session.post(url, json=job_spec)
        return self._fetch(resp)

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
        url = f"{self._base_url}/api/v1/compute/status/{_encode(rid)}/{_encode(job_id)}"
        self._curl("GET", url)
        resp = self._session.get(url)
        return self._fetch(resp)

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
        url = f"{self._base_url}/api/v1/filesystem/stat/{_encode(rid)}"
        self._curl("GET", url, params=params)
        resp = self._session.get(url, params=params)
        return self._fetch(resp)

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
        url = f"{self._base_url}/api/v1/filesystem/ls/{_encode(rid)}"
        self._curl("GET", url, params=params)
        resp = self._session.get(url, params=params)
        return self._fetch(resp)

    def download(
        self,
        remote_path: str,
        local_dest: str | Path,
        *,
        resource_id: str | None = None,
    ) -> None:
        """Download a file from the remote filesystem.

        GET /api/v1/filesystem/download/{resource_id}?path=...

        The server may respond immediately with binary content or return a task
        (``task_id`` / ``task_uri``).  In the task case the method polls until
        the task completes and then streams the file from the URL found in
        ``task.result``.

        Args:
            remote_path: Absolute path to the file on the remote filesystem.
            local_dest: Local destination path for the downloaded file.
            resource_id: Filesystem resource ID. Falls back to config ``resource_id``.
        """
        rid = self._resource(resource_id)
        url = f"{self._base_url}/api/v1/filesystem/download/{_encode(rid)}"
        params = {"path": remote_path}
        self._curl("GET", url, params=params, output_path=local_dest)
        resp = self._session.get(url, params=params, stream=True)
        _raise_for_error(resp)

        if "application/json" in resp.headers.get("Content-Type", ""):
            data = _json_response(resp)
            if "task_id" not in data or "task_uri" not in data:
                raise IriClientError(f"Unexpected JSON response from download: {data}")
            task = self._wait_for_task(data["task_id"], data["task_uri"])
            result = task.get("result") or {}
            if self._debug:
                print(f"# download task {data['task_id']} completed", file=sys.stderr)
            if "output" not in result:
                raise IriClientError(f"Download task completed but result contains no output: {result}")
            output = result["output"]
            with open(local_dest, "w" if isinstance(output, str) else "wb") as fh:
                fh.write(output)
        else:
            _stream_to_file(resp, local_dest)

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
        url = f"{self._base_url}/api/v1/filesystem/upload/{_encode(rid)}"
        params = {"path": remote_path}
        self._curl("POST", url, params=params, upload_path=local_path)
        with open(local_path, "rb") as fh:
            resp = self._session.post(url, params=params, files={"file": fh})
        return self._fetch(resp)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _curl(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        upload_path: str | Path | None = None,
        output_path: str | Path | None = None,
    ) -> None:
        if not self._debug:
            return
        parts = ["curl", "-s"]
        if method.upper() != "GET":
            parts += ["-X", method.upper()]
        auth = self._session.headers.get("Authorization")
        if auth:
            parts += ["-H", f"Authorization: {auth}"]
        parts += ["-H", "Accept: application/json"]
        if json_body is not None:
            parts += ["-H", "Content-Type: application/json", "-d", _json.dumps(json_body)]
        if upload_path is not None:
            parts += ["-F", f"file=@{upload_path}"]
        if output_path is not None:
            parts += ["-o", str(output_path)]
        full_url = f"{url}?{urlencode(params)}" if params else url
        parts.append(full_url)
        print(shlex.join(parts), file=sys.stderr)

    def _fetch(self, resp: requests.Response) -> dict:
        result = _json_response(resp)
        if "task_id" in result and "task_uri" in result:
            result = self._wait_for_task(result["task_id"], result["task_uri"])
        return result

    def _wait_for_task(self, task_id: str, task_uri: str) -> dict:
        for attempt in range(1, _TASK_MAX_POLLS + 1):
            self._curl("GET", task_uri)
            resp = self._session.get(task_uri)
            task = _json_response(resp)
            status = task.get("status", "")
            if self._debug:
                print(f"# task {task_id}: status={status}", file=sys.stderr)
                # print(_json.dumps(task, indent=2), file=sys.stderr)
            if status in _TASK_TERMINAL_STATES:
                return task
            if attempt < _TASK_MAX_POLLS:
                time.sleep(_TASK_POLL_INTERVAL)
        raise IriClientError(
            f"Task {task_id} did not reach a terminal state "
            f"after {_TASK_MAX_POLLS} polls ({_TASK_POLL_INTERVAL}s interval)"
        )

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


def _resolve_config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    env = os.environ.get("IRI_CLIENT_CONFIG")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_CONFIG_PATH


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
