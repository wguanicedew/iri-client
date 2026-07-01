"""Example usage of the pure-Python IRI client.

Before running:
  pip install requests pyyaml

  Copy src/python/config_example.yaml to ~/.iri.yaml and fill in:
    access_token: <your_token>
    resource_id:  <your_resource_id>

Usage:
  python examples/python_client_example.py [--config PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "python"))
from client import Client, IriClientError

TERMINAL_STATES = {"completed", "failed", "canceled"}

JOB_SPEC = {
    "executable": "bash",
    "arguments": ["-c", "echo hello from IRI job"],
    "name": "iri-python-client-example",
    "inherit_environment": True,
    "environment": {},
    "resources": {
        "node_count": 1,
        "process_count": 1,
        "processes_per_node": 1,
        "cpu_cores_per_process": 1,
        "exclusive_node_use": False,
    },
    "attributes": {
        "duration": 300,
        "queue_name": "debug",
        "account": "m5037",
        "custom_attributes": {},
    },
    "launcher": "single",
}

_NERSC_JOB_DIR = "/global/cfs/cdirs/m5037/iri_test"
NERSC_JOB_SPEC = {
    "executable": "bash",
    "arguments": [f"{_NERSC_JOB_DIR}/test.sh", "-d", _NERSC_JOB_DIR],
    "directory": _NERSC_JOB_DIR,
    "name": "triton-inference-server",
    "inherit_environment": True,
    "environment": {},
    "stdout_path": f"{_NERSC_JOB_DIR}/output.txt",
    "stderr_path": f"{_NERSC_JOB_DIR}/error.txt",
    "resources": {
        "node_count": 1,
        "process_count": 1,
        "processes_per_node": 1,
        "cpu_cores_per_process": 64,
        "gpu_cores_per_process": 4,
        "exclusive_node_use": True,
    },
    "attributes": {
        "duration": 30 * 60,
        "queue_name": "debug",
        "account": "m5037",
        "custom_attributes": {},
    },
    "launcher": "single",
}


def demo_filesystem(client: Client, remote_dir: str) -> None:
    print(f"\n--- stat {remote_dir} ---")
    try:
        info = client.stat(remote_dir)
        print(json.dumps(info, indent=2))
    except IriClientError as e:
        print(f"stat failed: {e}")

    print(f"\n--- ls {remote_dir} ---")
    try:
        listing = client.ls(remote_dir)
        print(json.dumps(listing, indent=2))
    except IriClientError as e:
        print(f"ls failed: {e}")


def demo_download_upload(client: Client, remote_path: str, local_path: str) -> None:
    print(f"\n--- download {remote_path} -> {local_path} ---")
    try:
        client.download(remote_path, local_path)
        print(f"Saved to {local_path} ({Path(local_path).stat().st_size} bytes)")
    except IriClientError as e:
        print(f"download failed: {e}")
        return

    print(f"\n--- upload {local_path} -> {remote_path}.copy ---")
    try:
        result = client.upload(local_path, remote_path + ".copy")
        print(json.dumps(result, indent=2))
    except IriClientError as e:
        print(f"upload failed: {e}")


def demo_launch_job(client: Client, job_spec: dict, poll_interval: int = 5, max_polls: int = 12) -> None:
    print("\n--- launch_job ---")
    try:
        job = client.launch_job(job_spec)
    except IriClientError as e:
        print(f"launch_job failed: {e}")
        return

    job_id = job.get("id", "")
    print(f"Submitted job id: {job_id}")
    print(json.dumps(job, indent=2))

    print(f"\n--- polling get_job (every {poll_interval}s, max {max_polls} attempts) ---")
    for attempt in range(1, max_polls + 1):
        try:
            status = client.get_job(job_id)
        except IriClientError as e:
            print(f"[{attempt}/{max_polls}] get_job failed: {e}")
            time.sleep(poll_interval)
            continue

        state = (status.get("status") or {}).get("state", "unknown").strip().lower()
        print(f"[{attempt}/{max_polls}] state={state}")

        if state in TERMINAL_STATES:
            print("Job reached terminal state.")
            return

        if attempt < max_polls:
            time.sleep(poll_interval)

    print(f"Job did not reach a terminal state after {max_polls} polls.")


def main() -> None:
    parser = argparse.ArgumentParser(description="IRI pure-Python client demo")
    parser.add_argument("--config", default=None,
                        help="Path to YAML config file (default: $IRI_CLIENT_CONFIG or ~/.iri.yaml)")
    parser.add_argument("--remote-dir", default="/global/cfs/cdirs/m5037/iri_test",
                        help="Remote directory to stat/ls")
    parser.add_argument("--remote-file", default=None,
                        help="Remote file to download/upload (skipped if not set)")
    parser.add_argument("--local-file", default="/tmp/iri_download",
                        help="Local path for downloaded file")
    parser.add_argument("--skip-job", action="store_true",
                        help="Skip the launch_job demo (avoids submitting a real job)")
    parser.add_argument("--debug", action="store_true",
                        help="Print equivalent curl command for each API call")
    args = parser.parse_args()

    client = Client(args.config, debug=args.debug)

    demo_filesystem(client, args.remote_dir)

    if args.remote_file:
        demo_download_upload(client, args.remote_file, args.local_file)

    if not args.skip_job:
        print("\n=== simple job ===")
        demo_launch_job(client, JOB_SPEC)
        print("\n=== NERSC job ===")
        demo_launch_job(client, NERSC_JOB_SPEC)


if __name__ == "__main__":
    main()
