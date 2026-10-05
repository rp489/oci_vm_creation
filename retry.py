"""Retry clear OCI capacity rejections without depending on cron delivery."""

import os
from pathlib import Path
import subprocess
import time


INTERVAL_SECONDS = 300
MAX_ATTEMPTS = 60
SCRIPT = Path(__file__).with_name("provision.sh")


def workflow_active():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return True
    result = subprocess.run(
        ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/provision.yml",
         "--jq", ".state"], capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Cannot verify workflow state; no new launch will be attempted.")
    return result.stdout.strip() == "active"


def run():
    mode = os.environ.get("PROVISION_MODE", "preflight")
    if mode != "launch":
        return subprocess.run(["bash", str(SCRIPT)]).returncode
    output_name = os.environ.get("GITHUB_OUTPUT")
    if not output_name:
        raise RuntimeError("Launch retries require GITHUB_OUTPUT to track safe retry decisions.")
    output = Path(output_name)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if not workflow_active():
            print("Workflow is disabled. No further launch will be attempted.", flush=True)
            return 0
        started = time.monotonic()
        offset = output.stat().st_size if output.exists() else 0
        print(f"Provisioning attempt {attempt}/{MAX_ATTEMPTS}.", flush=True)
        result = subprocess.run(["bash", str(SCRIPT)])
        if result.returncode:
            return result.returncode
        decisions = output.read_bytes()[offset:].decode("utf-8").splitlines() if output.exists() else []
        pauses = [line for line in decisions if line in ("pause=true", "pause=false")]
        # Retry only the explicit final decision from this attempt. Missing
        # output, acceptance, an existing VM, and uncertainty all stop here.
        if not pauses or pauses[-1] != "pause=false":
            return 0
        delay = max(0, INTERVAL_SECONDS - (time.monotonic() - started))
        print(f"Capacity unavailable or rate limited. Next attempt in {delay:.0f} seconds.", flush=True)
        time.sleep(delay)
        if attempt == MAX_ATTEMPTS:
            if workflow_active():
                with output.open("a", encoding="utf-8") as stream:
                    stream.write("continue=true\n")
                print("Retry window completed. Requesting an automatic continuation.", flush=True)
            return 0
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        print("Retry runner stopped because a required check could not be completed.", flush=True)
        raise SystemExit(1)
