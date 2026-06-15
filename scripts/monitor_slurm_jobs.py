#!/usr/bin/env python3
"""
Poll specific SLURM jobs and send a Slack alert if any enters an unexpected state.
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime


EXPECTED_NONTERMINAL_STATES = {
    "PENDING",
    "RUNNING",
    "CONFIGURING",
    "COMPLETING",
    "SUSPENDED",
    "RESIZING",
    "REQUEUED",
}

EXPECTED_TERMINAL_STATES = {
    "COMPLETED",
}

UNEXPECTED_TERMINAL_STATES = {
    "BOOT_FAIL",
    "CANCELLED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "REVOKED",
    "SPECIAL_EXIT",
    "STOPPED",
    "TIMEOUT",
}


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check = True, capture_output = True, text = True)
    return result.stdout


def fetch_states(job_ids: list[str]) -> dict[str, str]:
    output = _run([
        "sacct",
        "-j",
        ",".join(job_ids),
        "--format=JobIDRaw,State",
        "-P",
        "-X",
    ])
    lines = output.strip().splitlines()
    states: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        job_id, state = line.split("|", 1)
        if job_id in job_ids:
            states[job_id] = state.split()[0]
    return states


def send_slack(message: str):
    subprocess.run(
        ["python", "/home/saksham3/utils/slack.py", message],
        check = True,
    )


def main():
    parser = argparse.ArgumentParser(description = "Monitor SLURM jobs and Slack on unexpected failures")
    parser.add_argument("--job-id", dest = "job_ids", action = "append", required = True, help = "Job ID to monitor")
    parser.add_argument("--interval-minutes", type = float, default = 30, help = "Polling interval in minutes")
    parser.add_argument("--duration-hours", type = float, default = 8, help = "Total monitoring duration in hours")
    args = parser.parse_args()

    sent_alert_for: set[str] = set()
    deadline = time.time() + args.duration_hours * 3600
    interval_s = args.interval_minutes * 60

    while time.time() < deadline:
        try:
            states = fetch_states(args.job_ids)
        except Exception as exc:
            send_slack(f"SLURM monitor failed while checking jobs {','.join(args.job_ids)}: {type(exc).__name__}: {exc}")
            raise

        all_terminal = True
        timestamp = datetime.now().isoformat(timespec = "seconds")
        print(f"[{timestamp}] {states}", flush = True)

        for job_id in args.job_ids:
            state = states.get(job_id, "UNKNOWN")
            if state in EXPECTED_NONTERMINAL_STATES:
                all_terminal = False
                continue
            if state in EXPECTED_TERMINAL_STATES:
                continue
            if state in UNEXPECTED_TERMINAL_STATES and job_id not in sent_alert_for:
                send_slack(f"Kalshi SLURM job {job_id} entered unexpected state {state}")
                sent_alert_for.add(job_id)
                continue
            if state == "UNKNOWN":
                all_terminal = False

        if all_terminal:
            return

        time.sleep(interval_s)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
