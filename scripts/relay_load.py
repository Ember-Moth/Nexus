"""Reproducible P5 load run of release-built Rust Relays against isolated services.

    TMPDIR=/tmp/nexus-p5 LC_ALL=en_US.UTF-8 uv run python scripts/relay_load.py \\
        --out /tmp/nexus-p5/report [--scenarios control,http,ws,tokens,snapshot,failover]

The script builds ``nexus-relay`` with ``--release``, starts a private PostgreSQL and Redis
(``scripts/native_services.py``) with commit timestamps and room for 64 nodes x 8 pool
connections, migrates it, then runs the ``capacity`` tests in
``tests/integration/test_relay_load.py``. Every test starts its own Relay processes (each with
its own node login), the Worker and the TLS upstream double ``scripts/relay_upstream.py``, and
repeats each profile at least twice, reversing the profile order on the second pass.

Each profile appends one JSON object to ``<out>/results.jsonl`` with the host load before and
after it. ``<out>/summary.md`` lists every metric as the min-max range over the rounds. The
numbers describe this machine and the local upstream double only, not a deployment's capacity.
Keep the services between invocations with ``--workspace DIR --keep`` while iterating.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from uuid import uuid4

from native_services import NativeServices

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "src" / "relay"
SCENARIOS = ("control", "pools", "hotwallet", "http", "ws", "tokens", "snapshot", "failover")
TESTS = {"pools": "test_pool_variants", "hotwallet": "test_hot_wallet"}
# 64 nodes x the default pool of 8, plus API, Worker, samplers and the admin connection.
# Every node statement is logged with its duration; see relay_load_kit.PgLog.
PG_OPTIONS = (
    "max_connections=1000",
    "log_line_prefix=%m [%p] %u ",
    "log_parameter_max_length=0",
    "log_parameter_max_length_on_error=0",
    "log_checkpoints=on",
)


def host_state():
    load = os.getloadavg()
    return {"load1": round(load[0], 2), "load5": round(load[1], 2), "cpus": os.cpu_count()}


def uptime():
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()


def local_estimator(results: Path, rounds: int) -> int:
    """The tokens scenario's offline part: the Relay's tiktoken estimator in a fresh process."""
    for round_no in range(1, rounds + 1):
        before = host_state()
        run = subprocess.run(
            [
                "cargo",
                "test",
                "--release",
                "--locked",
                "--test",
                "tokenizer_capacity",
                "--",
                "--ignored",
                "--nocapture",
            ],
            cwd=CRATE,
            capture_output=True,
            text=True,
        )
        if run.returncode:
            sys.stderr.write(run.stdout + run.stderr)
            return run.returncode
        after = host_state()
        with results.open("a") as stream:
            for line in run.stdout.splitlines():
                if line.startswith("tokenizer_capacity "):
                    metrics = json.loads(line.removeprefix("tokenizer_capacity "))
                    record = {
                        "scenario": "tokens",
                        "profile": f"local-estimator-{metrics['encoding']}",
                        "round": round_no,
                        "host_before": before,
                        "host_after": after,
                        "metrics": metrics,
                    }
                    stream.write(json.dumps(record) + "\n")
    return 0


def build(profile_args):
    subprocess.run(
        ["cargo", "build", "--locked", "--release", "--bin", "nexus-relay", *profile_args],
        cwd=CRATE,
        check=True,
    )
    return CRATE / "target" / "release" / "nexus-relay"


def number(value):
    value = float(value)
    if value.is_integer():
        return f"{int(value)}"
    size = abs(value)
    return f"{value:.0f}" if size >= 100 else f"{value:.1f}" if size >= 10 else f"{value:.3g}"


def span(values):
    values = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if not values:
        return ""
    low, high = number(min(values)), number(max(values))
    return low if low == high else f"{low}–{high}"


def flatten(prefix, value, out):
    if isinstance(value, dict):
        for key, item in value.items():
            flatten(f"{prefix}.{key}" if prefix else key, item, out)
    else:
        out[prefix] = value


def summarize(results_path: Path) -> str:
    rows = [json.loads(line) for line in results_path.read_text().splitlines() if line.strip()]
    groups = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["profile"])].append(row)
    lines = ["# Relay load summary", "", f"{len(rows)} profile runs.", ""]
    by_scenario = defaultdict(list)
    for (scenario, profile), runs in groups.items():
        by_scenario[scenario].append((profile, runs))
    for scenario, profiles in by_scenario.items():
        lines += [f"## {scenario}", ""]
        for profile, attempts in profiles:
            # One run per round: the last attempt, clean unless every rerun was contended.
            per_round = {}
            for run in attempts:
                per_round[run["round"]] = run
            runs = list(per_round.values())
            contended = sum(1 for run in runs if run.get("contended"))
            flat = []
            for run in runs:
                values = {}
                flatten("", run.get("metrics", {}), values)
                flat.append(values)
            keys = sorted({k for values in flat for k in values})
            rounds = sorted({run["round"] for run in runs})
            loads = span([run["host_before"]["load1"] for run in runs])
            foreign = span(
                [
                    run["metrics"].get("postgres", {}).get("foreign_cpu_pct", {}).get("mean")
                    for run in runs
                ]
            )
            lines += [
                f"### {profile}",
                "",
                f"rounds {rounds}, attempts {len(attempts)}, contended kept {contended}, "
                f"load1 before {loads}, other processes' CPU during run {foreign} %",
                "",
                "| metric | min–max |",
                "| --- | --- |",
            ]
            for key in keys:
                text = span([values.get(key) for values in flat])
                if text == "":
                    seen = {json.dumps(values.get(key)) for values in flat if key in values}
                    text = " / ".join(sorted(seen))[:200]
                lines.append(f"| {key} | {text} |")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="directory for results")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--workspace", type=Path, help="reuse or keep this service directory")
    parser.add_argument("--keep", action="store_true", help="leave the services running")
    parser.add_argument("--relay", type=Path, help="prebuilt release nexus-relay")
    parser.add_argument("--summarize", action="store_true", help="only rebuild summary.md")
    parser.add_argument("pytest_args", nargs="*")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    results = args.out / "results.jsonl"
    if args.summarize:
        (args.out / "summary.md").write_text(summarize(results))
        return 0
    chosen = [s for s in args.scenarios.split(",") if s]
    if unknown := set(chosen) - set(SCENARIOS):
        parser.error(f"unknown scenarios {sorted(unknown)}")
    relay = args.relay or build([])
    workspace = args.workspace or Path(tempfile.mkdtemp(prefix="nxl-"))
    if len(str(workspace / "socket" / ".s.PGSQL.65535")) > 100:
        parser.error("set TMPDIR to a short path: Unix socket paths are limited to 103 bytes")
    isolated = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    meta = workspace / "services.json"
    database = (
        json.loads(meta.read_text())["database"]
        if meta.exists()
        else f"nexus_test_{uuid4().hex[:12]}"
    )
    services = NativeServices(workspace, database=database, track_commits=True, options=PG_OPTIONS)
    try:
        services.start()
        services.profiles(workspace)
        migrate = [sys.executable, "-m", "nexus.cli", "--env-file", str(workspace / ".env.migrate")]
        if subprocess.run([*migrate, "migrate"], cwd=ROOT, env=isolated).returncode:
            return 1
        env = dict(
            isolated,
            NEXUS_TEST_SERVICES=str(workspace),
            RELAY_LOAD_RUN="1",
            RELAY_LOAD_BINARY=str(relay),
            RELAY_LOAD_OUT=str(results),
            RELAY_LOAD_ROUNDS=str(args.rounds),
        )
        before = {"uptime": uptime(), **host_state()}
        expression = " or ".join(TESTS.get(name, f"test_{name}") for name in chosen)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "integration and capacity",
            "--capacity",
            "-s",
            "-q",
            "tests/integration/test_relay_load.py",
            "-k",
            expression,
            *args.pytest_args,
        ]
        code = subprocess.call(command, env=env, cwd=ROOT)
        if "tokens" in chosen and not os.environ.get("RELAY_LOAD_PROFILES"):
            code = code or local_estimator(results, args.rounds)
        after = {"uptime": uptime(), **host_state()}
        (args.out / "host.json").write_text(
            json.dumps({"before": before, "after": after, "relay": str(relay)}, indent=2)
        )
        if results.exists():
            (args.out / "summary.md").write_text(summarize(results))
        return code
    finally:
        if not args.keep:
            services.stop()


if __name__ == "__main__":
    sys.exit(main())
