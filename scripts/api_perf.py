#!/usr/bin/env python3
"""Compare the JSON API performance of two herdr server builds.

usage: api_perf.py [options]

Each round starts a headless server from each build in an isolated
XDG_CONFIG_HOME and XDG_RUNTIME_DIR, opens panes, and measures what an
external API client sees:

  idle          server CPU and syscalls with every pane idle
  ping          serial request latency for a method the app loop never sees
  snapshot      serial session.snapshot latency, which waits on the app loop
  late_write    latency when the client writes a moment after connecting
  event         time from workspace.rename to its workspace.renamed event
  burst         time until a burst of renames has all been delivered
  busy_*        ping, snapshot, and CPU again while panes produce output

The candidate is the working tree. The baseline is the newest release tag
reachable from HEAD, checked out in a git worktree under target/. Both are
built here with the same toolchain and the release libghostty-vt
optimization, because a downloaded release binary differs in allocator and
build settings: an unchanged v0.9.3 built in the dev shell idled at five
times the CPU of the published binary. Pass --candidate-bin or
--baseline-bin to measure an existing binary instead.

Rounds alternate the order of the two builds so that drift in machine load
spreads over both.

For a dev loop, run `just bench-api-quick` after each change. It takes
about a minute once both builds are cached, and its late write, event,
burst, and syscall figures are stable to about 1% between identical
builds. Sub-millisecond latencies and CPU vary by up to 30% between
identical builds, so confirm a change in those with `just bench-api`,
which runs two rounds over longer windows.

Run it inside `nix develop`, or outside Nix with the toolchain from
rust-toolchain.toml, Zig 0.16, just, perl, and Python 3.9 or newer. On
macOS without Nix, Zig needs the Xcode command line tools for nmedit.

The release smoke test (`just bench-release-smoke`)
covers render and fan-out CPU through a TUI client, and
`just bench-api-fairness` times the app loop draining queued requests
in-process. This suite covers the API socket path from outside the
server, which neither of them reaches.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PRODUCER = SCRIPT_DIR / "release_perf_producer.pl"
HERDR_ENV_VARS = (
    "HERDR_BIN_PATH", "HERDR_ENV", "HERDR_SOCKET_PATH",
    "HERDR_CLIENT_SOCKET_PATH", "HERDR_SESSION", "HERDR_STARTUP_CWD",
    "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_PANE_ID",
)
# Lower is better for every metric; the report prints the change as a
# percentage of the baseline.
REPORTED_METRICS = (
    "idle_cpu_percent", "idle_syscalls_per_second",
    "ping_p50_ms", "ping_p95_ms", "ping_syscalls_per_request",
    "snapshot_p50_ms", "snapshot_p95_ms",
    "late_write_p50_ms",
    "event_p50_ms", "event_p95_ms", "burst_seconds",
    "busy_cpu_percent", "busy_ping_p95_ms", "busy_snapshot_p95_ms",
)


class ApiError(Exception):
    """The server answered without a result, or not in the expected order."""


def percentile(values: list[float], fraction: float) -> float | None:
    """Return the FRACTION percentile of VALUES by nearest rank."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def parse_ps_time(text: str) -> float:
    """Return seconds from a BSD ps TIME field such as 1:02.50."""
    minutes, seconds = text.strip().split(":")
    return int(minutes) * 60 + float(seconds)


def cpu_seconds(pid: int) -> float:
    """Return the CPU seconds process PID has used, all threads included.

    Linux reads nanoseconds from each thread's schedstat, because the
    10 ms ticks in /proc/PID/stat cannot resolve an idle server.
    """
    if sys.platform.startswith("linux"):
        total = 0
        for path in glob.glob("/proc/%d/task/*/schedstat" % pid):
            try:
                total += int(Path(path).read_text().split()[0])
            except (OSError, IndexError, ValueError):
                continue
        return total / 1e9
    output = subprocess.run(["ps", "-o", "time=", "-p", str(pid)],
                            capture_output=True, text=True, check=True)
    return parse_ps_time(output.stdout)


def syscall_count(pid: int) -> int | None:
    """Return a cumulative syscall count for PID, or None when unavailable.

    macOS counts every BSD syscall through top, which needs no root for
    the user's own processes. Linux has no unprivileged equivalent:
    /proc/PID/io counts only file reads and writes, not socket recv and
    send, so it would report zero for the request path.
    """
    if not sys.platform.startswith("darwin"):
        return None
    output = subprocess.run(
        ["top", "-l", "1", "-pid", str(pid), "-stats", "pid,sysbsd"],
        capture_output=True, text=True)
    for line in output.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(pid):
            return int(fields[1].rstrip("+"))
    return None


def read_line(conn: socket.socket) -> bytes:
    """Read one newline-terminated line from CONN."""
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


class Server:
    """One headless herdr server in its own config and runtime directories."""

    def __init__(self, binary: str, state: str) -> None:
        self.binary = binary
        self.env = {key: value for key, value in os.environ.items()
                    if key not in HERDR_ENV_VARS}
        self.env.update(XDG_CONFIG_HOME=os.path.join(state, "xdg"),
                        XDG_RUNTIME_DIR=os.path.join(state, "run"),
                        HERDR_DISABLE_SOUND="1", SHELL="/bin/sh")
        self.socket = os.path.join(state, "xdg", "herdr", "herdr.sock")
        os.makedirs(self.env["XDG_RUNTIME_DIR"])
        self.log = open(os.path.join(state, "server.log"), "w")
        self.process = subprocess.Popen(
            [binary, "server"], env=self.env, stdout=self.log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            self._wait_until_ready()
        except BaseException:
            self.stop()
            raise

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 15
        while True:
            try:
                self.request("ping")
                return
            except (OSError, ValueError, ApiError):
                if time.monotonic() > deadline or self.process.poll() is not None:
                    raise RuntimeError("server did not answer: " + self.binary)
                time.sleep(0.1)

    def request(self, method: str, params: dict | None = None,
                delay_write: float = 0.0) -> dict:
        """Send one request on a fresh connection and return its result."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(10)
            conn.connect(self.socket)
            if delay_write:
                time.sleep(delay_write)
            conn.sendall(json.dumps({"id": "perf", "method": method,
                                     "params": params or {}}).encode() + b"\n")
            reply = read_line(conn)
        answer = json.loads(reply)
        if "result" not in answer:
            raise ApiError("%s: %s" % (method, answer.get("error", reply)))
        return answer["result"]

    def cli(self, *arguments: str) -> dict | None:
        """Run the server binary's own CLI against this server."""
        output = subprocess.run([self.binary, *arguments], env=self.env,
                                capture_output=True, text=True, check=True)
        return json.loads(output.stdout)["result"] if output.stdout else None

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.request("server.stop")
            except (OSError, ValueError, ApiError):
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.log.close()


class Lines:
    """Newline-delimited messages from a socket that stays open."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.buffer = b""

    def next(self) -> dict:
        while b"\n" not in self.buffer:
            chunk = self.conn.recv(65536)
            if not chunk:
                raise ApiError("subscription closed")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)


def timed(function: Callable[[], object], count: int) -> list[float]:
    """Call FUNCTION COUNT times and return each duration in milliseconds."""
    samples = []
    for _ in range(count):
        start = time.monotonic()
        function()
        samples.append((time.monotonic() - start) * 1000)
    return samples


def summarize(name: str, samples: list[float]) -> dict:
    """Return the p50 and p95 of SAMPLES under keys prefixed with NAME."""
    return {name + "_p50_ms": percentile(samples, 0.5),
            name + "_p95_ms": percentile(samples, 0.95)}


def measure_cpu(server: Server, seconds: float) -> tuple[float, float | None]:
    """Return server CPU percent and syscalls per second over SECONDS.

    Rates divide by the measured interval, because on macOS the ps and top
    calls themselves lengthen it.
    """
    pid = server.process.pid
    start = time.monotonic()
    cpu_before, calls_before = cpu_seconds(pid), syscall_count(pid)
    time.sleep(seconds)
    cpu_after, calls_after = cpu_seconds(pid), syscall_count(pid)
    elapsed = time.monotonic() - start
    calls = (None if calls_before is None or calls_after is None
             else (calls_after - calls_before) / elapsed)
    return 100 * (cpu_after - cpu_before) / elapsed, calls


def rename(server: Server, workspace_id: str, label: str) -> None:
    server.request("workspace.rename",
                   {"workspace_id": workspace_id, "label": label})


def expect_rename(events: Lines, label: str) -> None:
    """Read the next event and check that it reports the rename to LABEL.

    Matching the label catches a duplicated or dropped event, which would
    otherwise shift every later latency without an error.
    """
    event = events.next()
    if json.dumps(label) not in json.dumps(event):
        raise ApiError("expected rename to %s, got %s" % (label, event))


def measure_events(server: Server, workspace_id: str, count: int,
                   burst: int) -> tuple[list[float], float]:
    """Return rename-to-event latencies in ms and the burst drain time."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as subscriber:
        subscriber.settimeout(10)
        subscriber.connect(server.socket)
        subscriber.sendall(json.dumps({
            "id": "perf-events", "method": "events.subscribe",
            "params": {"subscriptions": [{"type": "workspace.renamed"}]},
        }).encode() + b"\n")
        events = Lines(subscriber)
        events.next()
        latencies = []
        for index in range(count):
            label = "event-%d" % index
            start = time.monotonic()
            rename(server, workspace_id, label)
            expect_rename(events, label)
            latencies.append((time.monotonic() - start) * 1000)
        labels = ["burst-%d" % index for index in range(burst)]
        start = time.monotonic()
        for label in labels:
            rename(server, workspace_id, label)
        for label in labels:
            expect_rename(events, label)
        return latencies, time.monotonic() - start


def start_writers(server: Server, panes: list[str], state: str,
                  rate: float) -> None:
    gate = os.path.join(state, "start-output")
    for index, pane in enumerate(panes, start=1):
        server.cli("pane", "run", pane,
                   shlex.join([str(PRODUCER), str(rate), gate, "p%d" % index]))
    open(gate, "w").close()


def check_writers(server: Server, panes: list[str]) -> None:
    """Fail unless every writer pane shows producer output.

    A writer that failed to start would leave the busy figures equal to
    the idle ones and the comparison would look valid.
    """
    for pane in panes:
        screen = subprocess.run(
            [server.binary, "pane", "read", pane, "--source", "visible",
             "--format", "text"],
            env=server.env, capture_output=True, text=True, check=True)
        if "bench-output-" not in screen.stdout:
            raise RuntimeError("writer pane %s produced no output" % pane)


def run_round(binary: str, label: str, options: argparse.Namespace) -> dict:
    """Measure one server started from BINARY and return a result dict."""
    # /tmp rather than $TMPDIR: macOS caps a unix socket path at 104 bytes
    # and its per-user $TMPDIR is long enough to exceed that.
    state = tempfile.mkdtemp(prefix="hap-", dir="/tmp")
    server = None
    try:
        server = Server(binary, state)
        workspace = server.cli("workspace", "create", "--cwd", state,
                               "--label", "perf", "--no-focus")
        workspace_id = workspace["workspace"]["workspace_id"]
        panes = [workspace["root_pane"]["pane_id"]]
        for index in range(2, options.panes + 1):
            tab = server.cli("tab", "create", "--workspace", workspace_id,
                             "--label", "perf-%d" % index, "--no-focus")
            panes.append(tab["root_pane"]["pane_id"])
        time.sleep(options.warmup)
        result = {"label": label, "binary": binary, "panes": len(panes)}
        result["idle_cpu_percent"], result["idle_syscalls_per_second"] = \
            measure_cpu(server, options.seconds)

        pid = server.process.pid
        calls_before = syscall_count(pid)
        ping = timed(lambda: server.request("ping"), options.requests)
        calls_after = syscall_count(pid)
        if calls_before is not None and calls_after is not None:
            result["ping_syscalls_per_request"] = \
                (calls_after - calls_before) / options.requests
        snapshot = timed(lambda: server.request("session.snapshot"),
                         options.requests)
        late = timed(lambda: server.request("ping", delay_write=0.005),
                     options.late_requests)
        events, burst = measure_events(server, workspace_id,
                                       options.events, options.burst)
        result.update(summarize("ping", ping), **summarize("snapshot", snapshot))
        result["late_write_p50_ms"] = percentile(late, 0.5)
        result.update(summarize("event", events))
        result["burst_seconds"] = burst

        writers = panes[:options.writers]
        start_writers(server, writers, state, options.rate)
        time.sleep(options.warmup)
        result["busy_cpu_percent"], _ = measure_cpu(server, options.seconds)
        busy_ping = timed(lambda: server.request("ping"), options.requests)
        busy_snapshot = timed(lambda: server.request("session.snapshot"),
                              options.requests)
        check_writers(server, writers)
        result["busy_ping_p95_ms"] = percentile(busy_ping, 0.95)
        result["busy_snapshot_p95_ms"] = percentile(busy_snapshot, 0.95)
        return result
    finally:
        if server is not None:
            server.stop()
        shutil.rmtree(state, ignore_errors=True)


def compare(results: list[dict]) -> list[str]:
    """Return report lines comparing baseline and candidate medians."""
    by_label: dict[str, list[dict]] = {}
    for result in results:
        by_label.setdefault(result["label"], []).append(result)
    lines = ["%-26s %12s %12s %9s" % ("metric", "baseline", "candidate",
                                       "change")]
    for metric in REPORTED_METRICS:
        medians = {}
        for label in ("baseline", "candidate"):
            values = [r[metric] for r in by_label.get(label, [])
                      if r.get(metric) is not None]
            medians[label] = statistics.median(values) if values else None
        before, after = medians["baseline"], medians["candidate"]
        change = ("%+8.1f%%" % (100 * (after - before) / before)
                  if before and after is not None else "%9s" % "-")
        lines.append("%-26s %12s %12s %s" % (metric, number(before),
                                             number(after), change))
    return lines


def number(value: float | None) -> str:
    return "-" if value is None else "%.3f" % value


def build(source: Path, target_dir: Path) -> str:
    """Build herdr from SOURCE into TARGET_DIR and return the binary path.

    Each side gets its own target directory, so switching between them
    never rebuilds the other, and the dev shell's Debug libghostty-vt
    setting is overridden with the one releases use.
    """
    env = dict(os.environ, CARGO_TARGET_DIR=str(target_dir),
               LIBGHOSTTY_VT_OPTIMIZE="ReleaseFast")
    print("building %s" % source, file=sys.stderr)
    subprocess.run(["cargo", "build", "--release", "--locked"],
                   cwd=source, env=env, check=True)
    return str(target_dir / "release" / "herdr")


def baseline_source(ref: str | None) -> Path:
    """Return a worktree checked out at REF, or the newest reachable tag."""
    if ref is None:
        ref = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "describe", "--tags", "--abbrev=0",
             "--match", "v[0-9]*"],
            capture_output=True, text=True, check=True).stdout.strip()
    worktree = REPO_ROOT / "target" / "api-perf" / ("baseline-" + ref)
    if not worktree.exists():
        subprocess.run(["git", "-C", str(REPO_ROOT), "worktree", "add",
                        "--detach", str(worktree), ref], check=True)
    return worktree


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline-ref",
                        help="git ref to build as the baseline")
    parser.add_argument("--baseline-bin", help="existing baseline binary")
    parser.add_argument("--candidate-bin", help="existing candidate binary")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--panes", type=int, default=15)
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--rate", type=float, default=60)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--warmup", type=float, default=3)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--late-requests", type=int, default=20)
    parser.add_argument("--events", type=int, default=30)
    parser.add_argument("--burst", type=int, default=30)
    parser.add_argument("--out", help="JSON lines file for every round")
    options = parser.parse_args()

    target = REPO_ROOT / "target" / "api-perf"
    binaries = {
        "candidate": (os.path.abspath(options.candidate_bin)
                      if options.candidate_bin
                      else build(REPO_ROOT, target / "candidate-target")),
        "baseline": (os.path.abspath(options.baseline_bin)
                     if options.baseline_bin
                     else build(baseline_source(options.baseline_ref),
                                target / "baseline-target")),
    }
    results = []
    for round_number in range(1, options.rounds + 1):
        order = (("baseline", "candidate") if round_number % 2
                 else ("candidate", "baseline"))
        for label in order:
            print("round %d: %s" % (round_number, label), file=sys.stderr)
            result = run_round(binaries[label], label, options)
            result["round"] = round_number
            results.append(result)
            if options.out:
                with open(options.out, "a") as out:
                    out.write(json.dumps(result) + "\n")
    print("\n".join(compare(results)))


if __name__ == "__main__":
    main()
