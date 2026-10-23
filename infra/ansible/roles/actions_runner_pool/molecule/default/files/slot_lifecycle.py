"""The runner slot's lifecycle, checked from what the stubs saw.

Two suites, each against a stub GitHub API served here that mints a
distinct installation token per request and records every call with the
token it carried:

  scripts: runs actions-runner-slot, the start phase, and
    actions-runner-slot-stop, the stop phase, directly, one cell each,
    with a stub op, a stub docker, a stub systemctl and a stub sleep first
    on the PATH, which report each call here. The cell stands in for
    systemd: it creates the state directory, sets STATE_DIRECTORY and, for
    the stop phase, the pending cleanup and the variables systemd passes
    to ExecStopPost=. It runs wherever bash, curl, flock, jq and openssl
    are.

  units: starts the installed slot units under the service user's own
    systemd, with drop-ins that point them at this stub API and a stub op,
    runs the stand-in image on the real rootless daemon, and reads the
    containers' lifetimes from docker events. It also probes, with
    transient units, the systemd behavior the design relies on.

The checks every cell and case share:

  - each token minted has exactly one revocation that GitHub confirmed,
    with a 2xx, 401 or 404 the client received, unless the cell expects
    it unconfirmed; it has at most one revocation request, and no request
    carries it after that, so every DELETE made with it came first;
  - the calls, in order, are the ones the cell expects;
  - no secret in the output.

Nothing here reaches GitHub, 1Password or, in the scripts suite, a Docker
daemon: the stubs come first on the PATH, and the run refuses to start
otherwise.

Usage:
  python3 -I slot_lifecycle.py scripts START STOP LIB WORKDIR
      [--base-env FILE] [--only REGEX]
  python3 -I slot_lifecycle.py units BASE_ENV WORKDIR --image IMAGE
      [--only REGEX]

The results go to WORKDIR/results.json, and the exit status is 1 if any
cell or case failed.
"""

import argparse
import fcntl
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

# The prefix of every installation token the stub API mints.
MINTED_PREFIX = "ghs_example_"
# The stand-in image acts on the config it reads: a short clean job, a job
# that runs until a TERM and then exits 143, one that ignores a TERM, or a
# job that exits 137, 78, 143 or 1 on its own.
JIT_CONFIGS = {
    "clean": "example-jit-config",
    "hold": "example-jit-config-hold",
    "ignore": "example-jit-config-ignore",
    "x137": "example-jit-config-137",
    "x78": "example-jit-config-78",
    "x143": "example-jit-config-143",
    "x1": "example-jit-config-1",
}
RUNNER_ID = 42
# The 403 bodies GitHub sends for a secondary rate limit and for a missing
# permission. Only the latter is a refusal no retry fixes.
SECONDARY_LIMIT = {
    "message": "You have exceeded a secondary rate limit. Please wait a few minutes "
    "before you try again.",
    "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api"
    "#about-secondary-rate-limits",
    "status": "403",
}
NO_PERMISSION = {
    "message": "Resource not accessible by integration",
    "documentation_url": "https://docs.github.com/rest/actions/self-hosted-runners",
    "status": "403",
}
# The kind each rate limit's answer logs, and the answers that send a
# Retry-After.
RATE_KINDS = {
    "403-rate": "primary",
    "403-secondary": "secondary",
    "403-secondary-retry": "secondary",
    "429": "secondary",
}
RETRY_AFTER = ("403-secondary-retry", "429")
# The pending cleanup the start phase records for slot 1.
PENDING = {"runner_id": RUNNER_ID, "container": "actions-runner-slot-1"}
ORG = "example-org"
# The scripts' limits: each phase's API --max-time, a docker or op call
# with timeout's --kill-after, the start phase's bound past its first stop
# check, and the stop budget the role sets.
STEP_SECONDS = 15
CALL_SECONDS = 10
CALL_LIMIT = 25
START_BOUND = 115
CLEANUP_SECONDS = 120
LETTERS = {
    "token": "T",
    "list": "L",
    "jit": "J",
    f"delete:{RUNNER_ID}": "D",
    "revoke": "R",
}

# A page of 100 runners, then the rest, so the start phase has to page.
# Only 101 is a stale runner of host edge-1's slot 1, and only 107 one of
# host edge's slot 1: 108 has nine hex digits, and 109 eight and a newline.
RECONCILE_RUNNERS = [
    {"id": 1000 + i, "name": f"other-host-{i}", "status": "offline", "busy": False}
    for i in range(96)
] + [
    {"id": 102, "name": "pool-edge-1-1-4e5f6a7b", "status": "offline", "busy": True},
    {"id": 103, "name": "pool-edge-1-1-8c9d0e1f", "status": "online", "busy": False},
    {"id": 104, "name": "pool-edge-1-11-0a1b2c3d", "status": "offline", "busy": False},
    {"id": 105, "name": "pool-edge-1-2-0a1b2c3d", "status": "offline", "busy": False},
    {"id": 101, "name": "pool-edge-1-1-0a1b2c3d", "status": "offline", "busy": False},
    {"id": 106, "name": "other-edge-1-1-0a1b2c3d", "status": "offline", "busy": False},
    {"id": 107, "name": "pool-edge-1-0a1b2c3d", "status": "offline", "busy": False},
    {"id": 108, "name": "pool-edge-1-1-0a1b2c3d9", "status": "offline", "busy": False},
    {"id": 109, "name": "pool-edge-1-1-0a1b2c3d\n", "status": "offline", "busy": False},
]
# Four pages, none of them stale, for the reconcile's deadline.
MANY_RUNNERS = [
    {"id": 2000 + i, "name": f"other-{i}", "status": "online", "busy": False}
    for i in range(350)
]

# The settings a local run starts from; the Molecule run starts from the
# rendered pool.env instead.
DEFAULT_ENV = {
    "ORG": ORG,
    "APP_ID": "123456",
    "INSTALLATION_ID": "12345678",
    "RUNNER_GROUP_ID": "3",
    "LABELS": '["self-hosted","example"]',
    "DNS": '["192.0.2.53"]',
    "IMAGE": "ghcr.io/actions/actions-runner@sha256:" + "0" * 64,
    "CPUS": "1",
    "MEMORY": "512m",
    "PIDS_LIMIT": "256",
    "WORK_SIZE": "256m",
    "OP_KEY_VAULT": "example-vault",
    "OP_KEY_ITEM": "example-item",
    "OP_KEY_FIELD": "example-field",
    "NAME_PREFIX": "pool",
    "HOST_ALIAS": "edge-1",
    "CLEANUP_SECONDS": str(CLEANUP_SECONDS),
}

STUB_EVENT = """event() {
  curl --silent --max-time 5 --output /dev/null --data-urlencode "detail=$2" \\
    "$CELL_URL/$1"
}
"""
# Records its call and whether the token it got is the token file's, then
# prints the test key; CELL_OP=hang never answers, and hold:N answers late.
STUB_OP = (
    "#!/bin/sh\n"
    + STUB_EVENT
    + """event op "$*"
if [ "$OP_SERVICE_ACCOUNT_TOKEN" = "$(cat "$CELL_TOKEN_FILE")" ]; then
  event op-token match
else
  event op-token other
fi
case $CELL_OP in
hang) exec "$REAL_SLEEP" 60 ;;
hold:*) "$REAL_SLEEP" "${CELL_OP#hold:}" ;;
esac
cat "$CELL_KEY"
"""
)
# A daemon that has the image, no leftover container and no network yet.
# CELL_INFO=fail, hang or hold:N changes docker info, and CELL_RM the
# stop phase's docker rm. docker run reports its PID, what its stdin is
# and whether the line it reads is the JIT config, then exits CELL_RUN.
STUB_DOCKER = (
    "#!/bin/sh\n"
    + STUB_EVENT
    + """event docker "$(printf '%s\\n' "$@")"
case $1 in
info)
  case $CELL_INFO in
  fail) exit 1 ;;
  hang) exec "$REAL_SLEEP" 60 ;;
  hold:*) "$REAL_SLEEP" "${CELL_INFO#hold:}" ;;
  esac
  echo 27.0.0
  ;;
network) [ "$2" != inspect ] || exit 1 ;;
rm)
  case $CELL_RM in
  ok) ;;
  fail) echo "Error response from daemon: stub failure" >&2; exit 1 ;;
  *) echo "Error response from daemon: No such container: $4" >&2; exit 1 ;;
  esac
  ;;
run)
  event run-pid "$$"
  event run-stdin "$(readlink /proc/$$/fd/0)"
  IFS= read -r config
  if [ "$config" = "$CELL_JIT" ]; then
    event run-config ok
  else
    event run-config other
  fi
  exit "$CELL_RUN"
  ;;
esac
exit 0
"""
)
STUB_SLEEP = (
    "#!/bin/sh\n"
    + STUB_EVENT
    + """event sleep "$*"
exec "$REAL_SLEEP" "$@"
"""
)
# list-jobs prints CELL_JOB, the unit's running job if any, and show the
# slot unit's CELL_ACTIVE.
STUB_SYSTEMCTL = (
    "#!/bin/sh\n"
    + STUB_EVENT
    + """event systemctl "$*"
case $2 in
list-jobs) [ -z "$CELL_JOB" ] || printf '%s\\n' "$CELL_JOB" ;;
show) printf '%s\\n' "${CELL_ACTIVE:-inactive}" ;;
esac
"""
)


@dataclass
class Cell:
    name: str
    # start or stop, or a units case's kind.
    phase: str
    # The calls the cell expects, a regular expression over letters, with
    # spaces for reading only: T token, L list, J JIT, D the runner's
    # DELETE, R revocation, S a stale runner's DELETE.
    calls: str = ""
    rc: int = 0
    # Per call, the answers in turn, the last repeating: ok, a status,
    # 403-rate, 403-secondary, 403-secondary-retry, 403-unparsed,
    # 403-permission, timeout, no-config, no-id, each with an optional
    # @hold.
    answers: dict = field(default_factory=dict)
    retry_after: str = "900"
    # The signal, and the call or stub event the server sends it at.
    signal: str | None = None
    anchor: str | None = None
    # The flag, --dry-run or --condition for the start phase, --pending or
    # --retry for the stop phase.
    mode: str = ""
    # Start phase: whether the pending cleanup and the docker run follow,
    # and whether a cleanup ran before the token.
    runner: bool = False
    job: bool = False
    run_exit: int = 0
    op: str = ""
    info: str = ""
    preflight: bool = True
    cleans_first: bool = False
    # Both: the pending cleanup the cell starts with, whether it remains,
    # the config-error marker's age in seconds against the env file, or
    # None, and whether the marker remains.
    pending: str | None = None
    pending_after: bool = False
    marker: float | None = None
    marker_after: bool = False
    # Stop phase: docker rm's answer, whether the lock is held, the unit's
    # job and state, what systemd passes, and what the run's line reports.
    rm: str = "missing"
    locked: bool = False
    unit_job: str = ""
    active: str = "inactive"
    systemd: dict = field(
        default_factory=lambda: {
            "SERVICE_RESULT": "exit-code",
            "EXIT_CODE": "exited",
            "EXIT_STATUS": "75",
        }
    )
    cleans: bool = True
    outcome: str = "failed"
    stop_requested: bool = False
    registration: str = "none"
    container: str = "removed"
    sleeps: list | None = None
    # Tokens left without a confirmed revocation.
    unrevoked: int = 0
    events: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    state_dir: bool = True
    host: str = "edge-1"
    runners: list = field(default_factory=list)
    jit: str = JIT_CONFIGS["clean"]
    max_seconds: float = 8
    # Filled in by the run.
    cid: str = ""
    dir: Path | None = None
    pid: int = 0
    minted: int = 0
    counts: dict = field(default_factory=dict)
    records: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    started: threading.Event = field(default_factory=threading.Event)

    def record(self, kind: str, **detail: object) -> Record:
        entry: Record = {
            "t": time.monotonic(),
            "wall": time.time(),
            "kind": kind,
            **detail,
        }
        with self.lock:
            self.records.append(entry)
        return entry

    def at_anchor(self, point: str) -> None:
        if self.signal and point == self.anchor:
            self.started.wait(10)
            os.kill(self.pid, getattr(signal, "SIG" + self.signal))
            self.record("signal", sig=self.signal)


def classify(method: str, route: str) -> str:
    if method == "POST" and route.endswith("/access_tokens"):
        return "token"
    if method == "GET" and route == f"/orgs/{ORG}/actions/runners":
        return "list"
    if method == "POST" and route.endswith("/generate-jitconfig"):
        return "jit"
    if method == "DELETE" and route == "/installation/token":
        return "revoke"
    match = re.fullmatch(rf"/orgs/{ORG}/actions/runners/(\d+)", route)
    if method == "DELETE" and match:
        return f"delete:{match[1]}"
    return f"{method} {route}"


# What the stub API records of each request, answer and signal.
type Record = dict[str, Any]
# An answer: its status, its body as JSON or bytes, and its headers.
type Answer = tuple[int, dict | bytes | None, dict[str, str]]


class Handler(BaseHTTPRequestHandler):
    cells: ClassVar[dict[str, Cell]] = {}

    def reply(
        self,
        status: int,
        payload: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        merged = {
            "X-RateLimit-Remaining": "4999",
            "X-RateLimit-Reset": "4102444800",
            **(headers or {}),
        }
        for name, value in merged.items():
            self.send_header(name, value)
        data = (
            payload
            if isinstance(payload, bytes)
            else b""
            if payload is None
            else json.dumps(payload).encode()
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        parts = self.path.split("/")
        if parts[1] == "_event":
            cell = self.cells[parts[2]]
            detail = parse_qs(body).get("detail", [""])[0]
            cell.record("event", name=parts[3], detail=detail)
            cell.at_anchor(f"{parts[3]}:{(detail.split() or [''])[0]}")
            self.reply(200)
            return
        cell = self.cells[parts[1]]
        url = urlsplit(self.path[len(parts[1]) + 1 :])
        call = classify(self.command, url.path)
        auth = self.headers.get("Authorization", "")
        number = auth[len("Bearer " + MINTED_PREFIX) :]
        token = (
            int(number)
            if auth.startswith("Bearer " + MINTED_PREFIX) and number.isdigit()
            else None
        )
        with cell.lock:
            index = cell.counts.get(call, 0)
            cell.counts[call] = index + 1
        cell.record("api", call=call, token=token, jwt=auth.count(".") == 2)
        cell.at_anchor(call)
        specs = cell.answers.get(call, ["ok"])
        spec, _, hold = specs[min(index, len(specs) - 1)].partition("@")
        status, payload, headers = self.resolve(cell, call, spec, url.query)
        if spec == "timeout":
            hold = (STEP_SECONDS if cell.phase == "start" else CALL_SECONDS) + 3
        if hold:
            time.sleep(float(hold))
        if call == "token" and status == 201:
            with cell.lock:
                cell.minted += 1
                minted = cell.minted
            payload = {
                "token": f"{MINTED_PREFIX}{minted}",
                "expires_at": "2099-01-01T00:00:00Z",
            }
            cell.record("minted", token=minted)
        # The answer as the client saw it: none once its --max-time passed. The
        # record goes in before the reply, because the slot can exit and the
        # harness read the records as soon as the client has the answer.
        answer = cell.record(
            "answer",
            call=call,
            token=token,
            status=None if spec == "timeout" else status,
        )
        try:
            self.reply(status, payload, headers)
        except OSError:
            with cell.lock:
                answer["status"] = None

    @staticmethod
    def resolve(cell: Cell, call: str, spec: str, query: str) -> Answer:
        if spec in ("ok", "timeout"):
            return Handler.success(cell, call, query)
        answers: dict[str, Answer] = {
            "no-config": (201, {"runner": {"id": RUNNER_ID}}, {}),
            "no-id": (201, {"encoded_jit_config": cell.jit}, {}),
            "403-rate": (
                403,
                {"message": "stub"},
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4102444800"},
            ),
            "403-secondary": (403, SECONDARY_LIMIT, {}),
            "403-secondary-retry": (
                403,
                SECONDARY_LIMIT,
                {"Retry-After": cell.retry_after},
            ),
            "403-unparsed": (403, b"<html><body>403 Forbidden</body></html>", {}),
            "403-permission": (403, NO_PERMISSION, {}),
        }
        if spec in answers:
            return answers[spec]
        status = int(spec)
        headers = {"Retry-After": cell.retry_after} if status == 429 else {}
        return status, None if status == 204 else {"message": "stub"}, headers

    @staticmethod
    def success(cell: Cell, call: str, query: str) -> Answer:
        if call == "list":
            page = int(parse_qs(query).get("page", ["1"])[0])
            return (
                200,
                {
                    "total_count": len(cell.runners),
                    "runners": cell.runners[(page - 1) * 100 : page * 100],
                },
                {},
            )
        if call == "jit":
            return (
                201,
                {"runner": {"id": RUNNER_ID}, "encoded_jit_config": cell.jit},
                {},
            )
        if call == "token":
            return 201, None, {}
        if call.startswith("delete:") or call == "revoke":
            return 204, None, {}
        return 404, {"message": "Not Found"}, {}

    def do_GET(self) -> None:
        self.answer()

    def do_POST(self) -> None:
        self.answer()

    def do_DELETE(self) -> None:
        self.answer()

    def log_message(self, *args: object) -> None:
        pass


def serve(cells: list[Cell]) -> tuple[ThreadingHTTPServer, str]:
    Handler.cells = {c.cid: c for c in cells}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def letters(cell: Cell) -> str:
    out = ""
    for r in sorted(cell.records, key=lambda r: r["t"]):
        if r["kind"] == "api":
            out += LETTERS.get(
                r["call"], "S" if r["call"].startswith("delete:") else "?"
            )
    return out


def confirmed(record: Record) -> bool:
    """A revocation's answer that the client received.

    That is a 2xx, or a 401 or 404, for a token already invalid.
    """
    status = record.get("status")
    return (
        record["kind"] == "answer"
        and record["call"] == "revoke"
        and status is not None
        and (200 <= status < 300 or status in (401, 404))
    )


def token_failures(records: list[Record], unrevoked: int = 0) -> list[str]:
    """Each minted token's revocation confirmed exactly once.

    That holds but for the unrevoked ones the cell expects, with one
    revocation request at most, and nothing after it.
    """
    failures = []
    recs = sorted(records, key=lambda r: r["t"])
    minted = [r["token"] for r in recs if r["kind"] == "minted"]
    left = []
    for token in minted:
        uses = [
            i for i, r in enumerate(recs) if r["kind"] == "api" and r["token"] == token
        ]
        requests = [i for i in uses if recs[i]["call"] == "revoke"]
        confirms = [r for r in recs if confirmed(r) and r["token"] == token]
        if len(requests) > 1:
            failures.append(f"token {token} had {len(requests)} revocation requests")
        if len(confirms) > 1:
            failures.append(f"token {token} had {len(confirms)} confirmed revocations")
        if not confirms:
            left.append(token)
        if requests and max(uses) > requests[0]:
            calls = [recs[i]["call"] for i in uses]
            failures.append(f"token {token} used after its revocation: {calls}")
    if len(left) != unrevoked:
        failures.append(
            f"tokens {left} without a confirmed revocation, expected {unrevoked}"
        )
    return failures


def held(records: list[Record]) -> list[tuple[int, float, float]]:
    """Each token's [minted, confirmed revocation] interval.

    The interval is in wall-clock seconds, open-ended without a
    confirmation.
    """
    out = []
    for r in records:
        if r["kind"] == "minted":
            ends = [
                x["wall"] for x in records if confirmed(x) and x["token"] == r["token"]
            ]
            out.append((r["token"], r["wall"], min(ends) if ends else float("inf")))
    return out


def json_lines(text: str) -> list[Record]:
    out = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def read_env(path: Path) -> dict[str, str]:
    values = {}
    with path.open() as handle:
        for line in handle:
            match = re.match(r"^ACTIONS_RUNNER_POOL_([A-Z_]+)=(.*)$", line.rstrip("\n"))
            if match:
                values[match[1]] = shlex.split(match[2])[0] if match[2] else ""
    return values


def write_env(path: Path, values: dict[str, object]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as env:
        env.write(
            "".join(
                f"ACTIONS_RUNNER_POOL_{k}={shlex.quote(str(v))}\n"
                for k, v in values.items()
            )
        )
    path.chmod(0o600)


def write_file(path: Path, text: str, mode: int) -> None:
    with path.open("w") as handle:
        handle.write(text)
    path.chmod(mode)


def make_key_and_token(workdir: Path) -> tuple[Path, Path]:
    key = workdir / "key.pem"
    if not key.exists():
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                key,
            ],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    key.chmod(0o600)
    token = workdir / "token"
    if token.exists():
        token.chmod(0o600)
    write_file(token, "example-service-account-token\n", 0o400)
    return key, token


# --- scripts -----------------------------------------------------------------


def start_cells() -> list[Cell]:
    out = []
    ok_calls = "T L J R"
    out.append(Cell("start/ok", "start", ok_calls, runner=True, job=True))
    # Each answer at each call before the revocation. A token request
    # that fails leaves nothing to revoke; a later one revokes the token.
    # A refusal no retry fixes leaves the config-error marker. A 403 is one
    # only when it is no rate limit and its body is a GitHub error.
    codes = {
        "401": 78,
        "403": 78,
        "403-permission": 78,
        "403-rate": 75,
        "403-secondary": 75,
        "403-secondary-retry": 75,
        "403-unparsed": 75,
        "404": 78,
        "429": 75,
        "500": 75,
        "timeout": 75,
    }
    for call, calls in (("token", "T"), ("list", "T L R"), ("jit", "T L J R")):
        for answer, rc in codes.items():
            events = (
                {"rate-limited": 1} if answer in RATE_KINDS else {"rate-limited": 0}
            )
            out.append(
                Cell(
                    f"start/{call}-{answer}",
                    "start",
                    calls,
                    rc=rc,
                    answers={call: [answer]},
                    events=events,
                    marker_after=rc == 78,
                    max_seconds=STEP_SECONDS + 5 if answer == "timeout" else 8,
                )
            )
    out.append(
        Cell(
            "start/jit-no-config",
            "start",
            ok_calls,
            rc=75,
            answers={"jit": ["no-config"]},
            runner=True,
        )
    )
    out.append(
        Cell("start/jit-no-id", "start", ok_calls, rc=75, answers={"jit": ["no-id"]})
    )
    # A revocation GitHub didn't confirm runs no job and leaves the
    # registration to the stop phase; its token stays unconfirmed. A 401
    # confirms a token that is already invalid.
    out.extend(
        Cell(
            f"start/revoke-{answer}",
            "start",
            ok_calls,
            rc=75,
            answers={"revoke": [answer]},
            runner=True,
            unrevoked=1,
            events={"revoke-failed": 1, "rate-limited": 0},
            max_seconds=STEP_SECONDS + 5 if answer == "timeout" else 8,
        )
        for answer in ("500", "429", "timeout")
    )
    out.append(
        Cell(
            "start/revoke-401",
            "start",
            ok_calls,
            answers={"revoke": ["401"]},
            runner=True,
            job=True,
            events={"revoke-failed": 0},
        )
    )
    # Larger than a pipe's buffer, where bash would put a here-document
    # or a here-string in a temporary file; process substitution still
    # hands docker a pipe.
    out.append(
        Cell(
            "start/large-config",
            "start",
            ok_calls,
            runner=True,
            job=True,
            jit=JIT_CONFIGS["clean"] + "-" + "x" * 100000,
        )
    )
    # After the exec the exit status is docker's.
    out.append(
        Cell(
            "start/exec-137",
            "start",
            ok_calls,
            rc=137,
            runner=True,
            job=True,
            run_exit=137,
        )
    )
    out.append(
        Cell(
            "start/stale-500",
            "start",
            "T L L S R",
            rc=75,
            answers={"delete:101": ["500"]},
            runners=RECONCILE_RUNNERS,
        )
    )
    # An earlier start's pending cleanup runs first, with its own token,
    # and a cleanup that doesn't resolve runs no job.
    pending = json.dumps(PENDING)
    out.append(
        Cell(
            "start/pending",
            "start",
            "T D R T L J R",
            pending=pending,
            cleans_first=True,
            runner=True,
            job=True,
        )
    )
    out.append(
        Cell(
            "start/pending-401",
            "start",
            "T D R",
            rc=75,
            pending=pending,
            cleans_first=True,
            pending_after=True,
            answers={"delete:42": ["401"]},
            events={"delete-failed": 1},
        )
    )
    out.append(
        Cell(
            "start/pending-dry-run",
            "start",
            "",
            rc=75,
            mode="--dry-run",
            pending=pending,
            pending_after=True,
            preflight=False,
        )
    )
    out.append(
        Cell(
            "reconcile/edge-1",
            "start",
            "T L L S J R",
            runner=True,
            job=True,
            runners=RECONCILE_RUNNERS,
            events={"stale-runner-deleted": 1},
        )
    )
    out.append(
        Cell(
            "reconcile/edge",
            "start",
            "T L L S J R",
            runner=True,
            job=True,
            host="edge",
            runners=RECONCILE_RUNNERS,
            events={"stale-runner-deleted": 1},
        )
    )
    # Each page takes 12 s, so no fourth page starts after the 30 s deadline.
    out.append(
        Cell(
            "reconcile/deadline",
            "start",
            "T L L L J R",
            runner=True,
            job=True,
            runners=MANY_RUNNERS,
            answers={"list": ["ok@12"]},
            events={"reconcile-incomplete": 1},
            max_seconds=45,
        )
    )
    return out


def signal_cells() -> list[Cell]:
    out = []
    ok_calls = "T L J R"
    # A signal only records itself. One during the preflight ends the run
    # at the first check, before op; one later lets the run finish its
    # calls, revoke and end at the last, with the pending cleanup for the
    # stop phase and no job.
    out.append(
        Cell(
            "signal/info-TERM",
            "start",
            "",
            signal="TERM",
            anchor="docker:info",
            info="hold:2",
            events={"stopped": 1},
        )
    )
    out.append(
        Cell(
            "signal/op-TERM",
            "start",
            ok_calls,
            signal="TERM",
            anchor="op:read",
            op="hold:2",
            runner=True,
            events={"stopped": 1},
        )
    )
    out.extend(
        Cell(
            f"signal/list-{sig}",
            "start",
            ok_calls,
            signal=sig,
            anchor="list",
            answers={"list": ["ok@2"]},
            runner=True,
            events={"stopped": 1},
        )
        for sig in ("TERM", "INT", "HUP")
    )
    out.append(
        Cell(
            "signal/revoke-TERM",
            "start",
            ok_calls,
            signal="TERM",
            anchor="revoke",
            answers={"revoke": ["ok@2"]},
            runner=True,
            events={"stopped": 1},
        )
    )
    # The preflight's and op's limits; the configuration refusals, before op.
    out.append(Cell("preflight/info-fail", "start", "", rc=75, info="fail"))
    out.append(
        Cell(
            "preflight/info-hang",
            "start",
            "",
            rc=75,
            info="hang",
            max_seconds=CALL_LIMIT + 5,
        )
    )
    out.append(
        Cell(
            "preflight/op-hang",
            "start",
            "",
            rc=75,
            op="hang",
            max_seconds=CALL_LIMIT + 5,
        )
    )
    for name, env in (
        ("no-dns", {"DNS": "[]"}),
        ("labels", {"LABELS": "self-hosted"}),
        ("prefix", {"NAME_PREFIX": "po-ol"}),
        ("budget", {"CLEANUP_SECONDS": str(START_BOUND - 1)}),
    ):
        out.append(Cell(f"config/{name}", "start", "", rc=78, env=env, preflight=False))
    out.append(
        Cell(
            "config/malformed",
            "start",
            "",
            rc=78,
            env={"_append": "ACTIONS_RUNNER_POOL_ORG='unterminated"},
            preflight=False,
        )
    )
    out.append(
        Cell(
            "config/token-mode",
            "start",
            "",
            rc=78,
            env={"_token_mode": 0o600},
            preflight=False,
        )
    )
    out.append(
        Cell(
            "config/no-state-dir", "start", "", rc=78, state_dir=False, preflight=False
        )
    )
    out.append(
        Cell(
            "start/dry-run",
            "start",
            ok_calls,
            runner=True,
            mode="--dry-run",
            preflight=False,
        )
    )
    # The unit's condition check: the configuration and the marker, which
    # holds while it is newer than the env file and the token file.
    condition = {"mode": "--condition", "preflight": False}
    out.append(Cell("condition/ok", "start", "", **condition))
    out.append(
        Cell(
            "condition/marker",
            "start",
            "",
            rc=78,
            marker=5,
            marker_after=True,
            **condition,
        )
    )
    out.append(Cell("condition/marker-stale", "start", "", marker=-1000, **condition))
    out.append(
        Cell(
            "condition/labels",
            "start",
            "",
            rc=78,
            env={"LABELS": "self-hosted"},
            **condition,
        )
    )
    out.append(
        Cell(
            "condition/token-mode",
            "start",
            "",
            rc=78,
            env={"_token_mode": 0o600},
            **condition,
        )
    )
    out.append(
        Cell("condition/no-state-dir", "start", "", rc=78, state_dir=False, **condition)
    )
    return out


def stop_cells() -> list[Cell]:
    out = []
    pending = json.dumps(PENDING)
    # The stop phase, with the pending cleanup the start phase recorded,
    # which stays until both the container and the registration resolve.
    stop = {"pending": pending, "max_seconds": CLEANUP_SECONDS + 2}
    out.append(
        Cell("stop/204", "stop", "T D R", registration="deleted", sleeps=[], **stop)
    )
    out.append(
        Cell(
            "stop/404",
            "stop",
            "T D R",
            registration="gone",
            sleeps=[],
            answers={"delete:42": ["404"]},
            **stop,
        )
    )
    out.append(
        Cell(
            "stop/500-204",
            "stop",
            "T D D R",
            registration="deleted",
            sleeps=["1"],
            answers={"delete:42": ["500", "204"]},
            **stop,
        )
    )
    out.append(
        Cell(
            "stop/500",
            "stop",
            "T D D D D R",
            rc=75,
            registration="failed",
            sleeps=["1", "2", "4"],
            pending_after=True,
            answers={"delete:42": ["500"]},
            events={"delete-failed": 1},
            **stop,
        )
    )
    out.append(
        Cell(
            "stop/timeout",
            "stop",
            "T D D D D R",
            rc=75,
            registration="failed",
            sleeps=["1", "2", "4"],
            pending_after=True,
            answers={"delete:42": ["timeout"]},
            events={"delete-failed": 1},
            **stop,
        )
    )
    # A Retry-After past the deadline shortens to fit it, and the last
    # attempts leave the revocation its call. Whether the deadline leaves
    # room for the third and fourth depends on where $SECONDS ticks.
    out.append(
        Cell(
            "stop/429-capped",
            "stop",
            "T D D (D D)? R",
            rc=75,
            registration="failed",
            pending_after=True,
            answers={"delete:42": ["429"]},
            retry_after="3600",
            events={"delete-failed": 1},
            env={"CLEANUP_SECONDS": str(START_BOUND)},
            pending=pending,
            max_seconds=START_BOUND + 2,
        )
    )
    out.append(
        Cell(
            "stop/token-401",
            "stop",
            "T",
            rc=75,
            registration="failed",
            pending_after=True,
            answers={"token": ["401"]},
            events={"delete-failed": 1},
            **stop,
        )
    )
    out.append(
        Cell(
            "stop/revoke-500",
            "stop",
            "T D R",
            registration="deleted",
            answers={"revoke": ["500"]},
            unrevoked=1,
            events={"revoke-failed": 1},
            **stop,
        )
    )
    out.append(
        Cell(
            "stop/rm-running", "stop", "T D R", registration="deleted", rm="ok", **stop
        )
    )
    # A container that may still run keeps its registration, and no
    # token is minted beside it; the pending cleanup stays for a retry.
    out.append(
        Cell(
            "stop/rm-fail",
            "stop",
            "",
            rc=75,
            registration="skipped",
            rm="fail",
            container="failed",
            pending_after=True,
            events={"container-remove-failed": 1, "delete-skipped": 1},
            **stop,
        )
    )
    # Without a pending cleanup the stop phase only removes the container.
    out.append(Cell("stop/no-id", "stop", "", max_seconds=8))
    out.append(
        Cell(
            "stop/bad-id",
            "stop",
            "",
            pending="junk",
            events={"pending-unreadable": 1},
            max_seconds=8,
        )
    )
    out.append(
        Cell("stop/no-id-no-env", "stop", "", env={"_no_env": True}, max_seconds=8)
    )
    # Another cleanup holds the lock, so this one touches nothing.
    out.append(
        Cell(
            "stop/locked",
            "stop",
            "",
            rc=75,
            locked=True,
            cleans=False,
            container="none",
            pending_after=True,
            events={"cleanup-busy": 1},
            **stop,
        )
    )
    # How the start ended: an orderly-stop status counts as success only
    # with a stop or restart job running, and the stop phase fails the
    # start when systemd counted one that no one requested.
    stop_job, restart_job = (
        "7 actions-runner@1.service stop running",
        "8 actions-runner@1.service restart running",
    )
    for name, (result, code, status), job, outcome, rc in (
        ("clean", ("success", "exited", "0"), "", "clean", 0),
        ("0-stop", ("success", "exited", "0"), stop_job, "stopped", 0),
        ("143-stop", ("success", "exited", "143"), stop_job, "stopped", 0),
        ("term-restart", ("success", "killed", "TERM"), restart_job, "stopped", 0),
        ("143", ("success", "exited", "143"), "", "failed", 1),
        ("130", ("success", "exited", "130"), "", "failed", 1),
        ("term", ("success", "killed", "TERM"), "", "failed", 1),
        ("78", ("exit-code", "exited", "78"), "", "failed", 0),
        ("kill-stop", ("timeout", "killed", "KILL"), stop_job, "failed", 0),
    ):
        out.append(
            Cell(
                f"stop/class-{name}",
                "stop",
                "T D R",
                rc=rc,
                registration="deleted",
                unit_job=job,
                systemd={
                    "SERVICE_RESULT": result,
                    "EXIT_CODE": code,
                    "EXIT_STATUS": status,
                },
                outcome=outcome,
                stop_requested=bool(job),
                **stop,
            )
        )
    out.append(
        Cell(
            "stop/class-condition",
            "stop",
            "",
            systemd={"SERVICE_RESULT": "exec-condition"},
            outcome="config-error",
            max_seconds=8,
        )
    )
    return out


def cleanup_cells() -> list[Cell]:
    out = []
    stop = {"pending": json.dumps(PENDING), "max_seconds": CLEANUP_SECONDS + 2}
    # The start phase's --pending, and the cleanup service's --retry, which
    # acts only while the slot unit is inactive or failed.
    out.append(
        Cell(
            "pending/ok",
            "stop",
            "T D R",
            mode="--pending",
            registration="deleted",
            **stop,
        )
    )
    out.append(
        Cell(
            "pending/none",
            "stop",
            "",
            mode="--pending",
            cleans=False,
            container="none",
            max_seconds=8,
        )
    )
    out.append(
        Cell(
            "pending/locked",
            "stop",
            "",
            rc=75,
            mode="--pending",
            locked=True,
            cleans=False,
            container="none",
            pending_after=True,
            events={"cleanup-busy": 1},
            **stop,
        )
    )
    out.extend(
        Cell(
            f"retry/{active}",
            "stop",
            "T D R",
            mode="--retry",
            active=active,
            registration="deleted",
            **stop,
        )
        for active in ("inactive", "failed")
    )
    out.extend(
        Cell(
            f"retry/{active}",
            "stop",
            "",
            mode="--retry",
            active=active,
            cleans=False,
            container="none",
            pending_after=True,
            events={"cleanup-deferred": 1},
            **stop,
        )
        for active in ("active", "activating", "deactivating")
    )
    out.append(
        Cell(
            "retry/none",
            "stop",
            "",
            mode="--retry",
            cleans=False,
            container="none",
            max_seconds=8,
        )
    )
    out.append(
        Cell(
            "retry/locked",
            "stop",
            "",
            mode="--retry",
            locked=True,
            cleans=False,
            container="none",
            pending_after=True,
            events={"cleanup-busy": 1},
            **stop,
        )
    )
    out.append(
        Cell(
            "retry/rm-fail",
            "stop",
            "",
            rc=75,
            mode="--retry",
            active="failed",
            rm="fail",
            container="failed",
            registration="skipped",
            pending_after=True,
            events={"container-remove-failed": 1, "delete-skipped": 1},
            **stop,
        )
    )
    out.append(
        Cell(
            "retry/500",
            "stop",
            "T D D D D R",
            rc=75,
            mode="--retry",
            active="failed",
            registration="failed",
            sleeps=["1", "2", "4"],
            pending_after=True,
            answers={"delete:42": ["500"]},
            events={"delete-failed": 1},
            **stop,
        )
    )
    return out


def script_cells() -> list[Cell]:
    out = [*start_cells(), *signal_cells(), *stop_cells(), *cleanup_cells()]
    for i, cell in enumerate(out):
        cell.cid = f"c{i:03d}"
    return out


def write_script_cell(
    cell: Cell, ctx: dict[str, Any]
) -> tuple[dict[str, object], Path]:
    """Lay out the cell's directory as systemd and the role would leave it.

    Returns the settings the env file holds and the token file the cell uses.
    """
    cell.dir = ctx["workdir"] / "cells" / cell.cid
    shutil.rmtree(cell.dir, ignore_errors=True)
    cell.dir.mkdir(parents=True)
    state = cell.dir / "state"
    state.mkdir(mode=0o700)
    env_path = cell.dir / "pool.env"
    token_file = ctx["token"]
    values = {
        **ctx["base"],
        "API_URL": f"{ctx['url']}/{cell.cid}",
        "ORG": ORG,
        "HOST_ALIAS": cell.host,
        "NAME_PREFIX": "pool",
        "OP_TOKEN_FILE": token_file,
    }
    values.update({k: v for k, v in cell.env.items() if not k.startswith("_")})
    if "_token_mode" in cell.env:
        token_file = cell.dir / "token"
        write_file(
            token_file, "example-service-account-token\n", cell.env["_token_mode"]
        )
        values["OP_TOKEN_FILE"] = token_file
    if not cell.env.get("_no_env"):
        write_env(env_path, values)
    if "_append" in cell.env:
        with env_path.open("a") as handle:
            handle.write(cell.env["_append"] + "\n")
    if cell.pending is not None:
        write_file(state / "pending", cell.pending + "\n", 0o600)
    if cell.marker is not None:
        marker = state / "config-error"
        write_file(marker, "the installation token request: HTTP 401\n", 0o600)
        stamp = env_path.stat().st_mtime + cell.marker
        os.utime(marker, (stamp, stamp))
    return values, token_file


def run_script_cell(cell: Cell, ctx: dict[str, Any]) -> dict[str, object]:
    values, token_file = write_script_cell(cell, ctx)
    state = cell.dir / "state"
    env = {
        "PATH": f"{ctx['stubs']}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/"),
        "LC_ALL": "C.UTF-8",
        "ACTIONS_RUNNER_POOL_ENV": str(cell.dir / "pool.env"),
        "CELL_URL": f"{ctx['url']}/_event/{cell.cid}",
        "CELL_KEY": str(ctx["key"]),
        "CELL_TOKEN_FILE": str(token_file),
        "CELL_JIT": cell.jit,
        "CELL_RUN": str(cell.run_exit),
        "CELL_OP": cell.op,
        "CELL_INFO": cell.info,
        "CELL_RM": cell.rm,
        "CELL_JOB": cell.unit_job,
        "CELL_ACTIVE": cell.active,
        "REAL_SLEEP": ctx["sleep"],
    }
    if cell.state_dir:
        env["STATE_DIRECTORY"] = str(state)
    flags = [cell.mode] if cell.mode else []
    if cell.phase == "stop":
        env.update(
            {**cell.systemd, "INVOCATION_ID": "0123456789abcdef0123456789abcdef"}
        )
        argv = [ctx["stop"], *flags, "1"]
    else:
        argv = [ctx["start"], *flags, "1"]
    # The lock another cleanup would hold, an flock on its own open file.
    lock = None
    if cell.locked:
        lock = os.open(state / "lock", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
    begin = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            env=env,
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        cell.pid = proc.pid
        cell.started.set()
        try:
            out, err = proc.communicate(timeout=200)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, err = proc.communicate()
            timed_out = True
    finally:
        if lock is not None:
            os.close(lock)
    end = time.monotonic()
    return check_script_cell(
        cell,
        ScriptRun(proc.returncode, out, err, timed_out, end - begin, values, state),
    )


@dataclass
class ScriptRun:
    """What a script cell's run left for its checks."""

    rc: int
    out: str
    err: str
    timed_out: bool
    seconds: float
    values: dict[str, object]
    state: Path


@dataclass
class Seen:
    """What the stub API and the stubs saw of a script cell's run, in order."""

    records: list[Record]
    events: list[Record]
    names: list[str]
    docker: list[list[str]]
    systemctl: list[str]

    @classmethod
    def of(cls, cell: Cell) -> Seen:
        recs = sorted(cell.records, key=lambda r: r["t"])
        events = [r for r in recs if r["kind"] == "event"]
        return cls(
            records=recs,
            events=events,
            names=[r["name"] for r in events],
            docker=[r["detail"].split("\n") for r in events if r["name"] == "docker"],
            systemctl=[r["detail"] for r in events if r["name"] == "systemctl"],
        )


def check_script_cell(cell: Cell, result: ScriptRun) -> dict[str, object]:
    failures = []
    seen = Seen.of(cell)
    if result.timed_out:
        failures.append("the run didn't end within 200 s")
    if result.rc != cell.rc:
        failures.append(f"exit {result.rc}, expected {cell.rc}")
    calls = letters(cell)
    if not re.fullmatch(cell.calls.replace(" ", ""), calls):
        failures.append(f"calls {calls}, expected {cell.calls}")
    failures += token_failures(seen.records, cell.unrevoked)
    if result.seconds > cell.max_seconds:
        failures.append(
            f"the run took {result.seconds:.1f} s, more than {cell.max_seconds}"
        )
    failures += log_failures(cell, json_lines(result.err))
    failures += call_failures(cell, seen, result.out + result.err)

    pending_path = result.state / "pending"
    pending = pending_path.read_text() if pending_path.exists() else None
    if (result.state / "config-error").exists() != cell.marker_after:
        failures.append(
            f"config-error marker present: {not cell.marker_after}, "
            f"expected {cell.marker_after}"
        )
    if cell.phase == "start":
        failures += start_failures(cell, seen, pending, pending_path)
        if cell.job and not failures:
            failures += check_run(cell, seen.records, seen.docker[-1], result.values)
        failures += dry_run_failures(cell, result.out)
    else:
        failures += stop_failures(cell, seen, pending, result.out)
    return {
        "name": cell.name,
        "passed": not failures,
        "failures": failures,
        "exit": result.rc,
        "seconds": round(result.seconds, 1),
        "calls": calls,
        "stderr": redact(cell, result.err)[-2000:] if failures else "",
    }


def redact(cell: Cell, text: str) -> str:
    return re.sub(r"ghs_example_\d+", "<token>", text).replace(cell.jit, "<jit>")


def log_failures(cell: Cell, logs: list[Record]) -> list[str]:
    """The log lines the cell expects, and what each one reports."""
    failures = []
    for event, count in cell.events.items():
        got = sum(1 for entry in logs if entry.get("event") == event)
        if got != count:
            failures.append(f"{got} {event} lines, expected {count}")
    want = rate_limit_line(cell)
    for entry in logs:
        if (
            entry.get("event") == "delete-failed"
            and entry.get("runner_id") != RUNNER_ID
        ):
            failures.append(f"delete-failed without the runner ID: {entry}")
        if (
            entry.get("event") == "rate-limited"
            and {k: entry.get(k) for k in want} != want
        ):
            failures.append(f"rate-limited {entry}, expected {want}")
    return failures


def rate_limit_line(cell: Cell) -> dict[str, object]:
    """What a rate-limited line reports of the rate limit the cell answers."""
    specs = [s.partition("@")[0] for specs in cell.answers.values() for s in specs]
    spec = next((s for s in specs if s in RATE_KINDS), None)
    return {
        "kind": RATE_KINDS.get(spec),
        "retry_after": int(cell.retry_after) if spec in RETRY_AFTER else None,
    }


def call_failures(cell: Cell, seen: Seen, output: str) -> list[str]:
    """No secret in the output, and the credential each call carries."""
    failures = []
    secrets = [MINTED_PREFIX, cell.jit, "example-service-account-token"]
    if any(secret in output for secret in secrets):
        failures.append("a secret in the output")
    recs = seen.records
    if any(
        r["kind"] == "api" and r["call"] != "token" and r["token"] is None for r in recs
    ):
        failures.append("a call without an installation token")
    if any(r["kind"] == "api" and r["call"] == "token" and not r["jwt"] for r in recs):
        failures.append("a token request without a JWT")
    if (
        "op" in seen.names
        and "op-token" in seen.names
        and next(r["detail"] for r in seen.events if r["name"] == "op-token") != "match"
    ):
        failures.append("op didn't get the token file's token")
    return failures


def start_failures(
    cell: Cell, seen: Seen, pending: str | None, pending_path: Path
) -> list[str]:
    failures = []
    if "sleep" in seen.names:
        failures.append("the start phase slept")
    if seen.systemctl:
        failures.append(f"the start phase called systemctl: {seen.systemctl}")
    failures += start_pending_failures(cell, pending, pending_path)
    if cell.signal and sum(1 for r in seen.records if r["kind"] == "signal") != 1:
        failures.append("the signal wasn't sent at its anchor")
    if cell.signal and cell.anchor == "docker:info" and "op" in seen.names:
        failures.append("op ran after a stop during the preflight")
    if cell.name.startswith(("config/", "condition/")) and "op" in seen.names:
        failures.append("op ran before the configuration error")
    words = [" ".join(argv[:2]) for argv in seen.docker]
    want = start_docker_calls(cell)
    if words != want:
        failures.append(f"docker calls {words}, expected {want}")
    return failures


def start_pending_failures(
    cell: Cell, pending: str | None, pending_path: Path
) -> list[str]:
    expected = cell.runner or cell.pending_after
    if (pending is not None) != expected:
        return [f"pending cleanup {pending!r}, expected {expected}"]
    if cell.runner and (
        pending != json.dumps(PENDING, separators=(",", ":")) + "\n"
        or pending_path.stat().st_mode & 0o777 != 0o600
    ):
        mode = pending_path.stat().st_mode & 0o777
        return [f"pending cleanup {pending!r}, mode {mode:o}"]
    if cell.pending_after and pending != cell.pending + "\n":
        return [f"pending cleanup {pending!r}, expected the earlier start's"]
    return []


def start_docker_calls(cell: Cell) -> list[str]:
    """The docker calls the start phase makes, by their first two words."""
    want = []
    if cell.preflight and cell.mode != "--dry-run":
        want = ["info --format"]
        if cell.info not in ("fail", "hang"):
            want += [
                "image inspect",
                "ps --all",
                "network inspect",
                "network create",
            ]
    if cell.cleans_first:
        want.append("rm --force")
    if cell.job:
        want.append("run --rm")
    return want


def dry_run_failures(cell: Cell, out: str) -> list[str]:
    if cell.mode != "--dry-run" or cell.rc != 0:
        return []
    plan = json_lines(out)
    if (
        not plan
        or plan[-1].get("event") != "dry-run"
        or plan[-1].get("start_bound_seconds") != START_BOUND
    ):
        return [f"dry run plan {plan[-1:] if plan else out!r}"]
    return []


def stop_failures(cell: Cell, seen: Seen, pending: str | None, out: str) -> list[str]:
    failures = []
    if cell.cleans and seen.docker[:1] != [
        ["rm", "--force", "--", "actions-runner-slot-1"]
    ]:
        failures.append(
            f"the cleanup's first docker call wasn't docker rm: {seen.docker[:1]}"
        )
    if not cell.cleans and seen.docker:
        failures.append(f"docker called without a cleanup: {seen.docker}")
    want_systemctl = stop_systemctl_calls(cell)
    if seen.systemctl != want_systemctl:
        failures.append(f"systemctl calls {seen.systemctl}, expected {want_systemctl}")
    readable = cell.pending is not None and cell.pending.startswith("{")
    if ("op" in seen.names) != (cell.cleans and readable and cell.rm != "fail"):
        failures.append(f"op called: {'op' in seen.names}")
    if (pending is not None) != cell.pending_after:
        failures.append(
            f"pending cleanup present: {pending is not None}, "
            f"expected {cell.pending_after}"
        )
    if cell.sleeps is not None:
        slept = [r["detail"] for r in seen.events if r["name"] == "sleep"]
        if slept != cell.sleeps:
            failures.append(f"sleeps {slept}, expected {cell.sleeps}")
    failures += stop_line_failures(cell, out, readable=readable)
    return failures


def stop_systemctl_calls(cell: Cell) -> list[str]:
    """The systemctl calls the stop phase makes in the cell's mode."""
    if not cell.mode:
        return ["--user list-jobs --no-legend --plain actions-runner@1.service"]
    if cell.mode == "--retry" and not cell.locked and cell.pending is not None:
        return ["--user show --property=ActiveState --value actions-runner@1.service"]
    return []


def stop_line_failures(cell: Cell, out: str, *, readable: bool) -> list[str]:
    """The stop phase's one job or cleanup line, and what it reports."""
    event = "cleanup" if cell.mode else "job"
    lines = [entry for entry in json_lines(out) if entry.get("event") == event]
    want = {
        "slot": 1,
        "registration": cell.registration,
        "container": cell.container,
        "runner_id": RUNNER_ID if cell.cleans and readable else None,
        "pending": cell.pending_after,
        "status": cell.rc,
    }
    if cell.mode:
        want["mode"] = cell.mode[2:]
    else:
        want.update(
            {
                "outcome": cell.outcome,
                "stop_requested": cell.stop_requested,
                "service_result": cell.systemd.get("SERVICE_RESULT", ""),
                "exit_code": cell.systemd.get("EXIT_CODE", ""),
                "exit_status": cell.systemd.get("EXIT_STATUS", ""),
            }
        )
    if len(lines) != 1 or any(lines[0].get(k) != v for k, v in want.items()):
        return [f"{event} line {lines}, expected {want}"]
    return []


def check_run(
    cell: Cell, recs: list[Record], argv: list[str], values: dict[str, object]
) -> list[str]:
    """Check the exec'd docker run.

    It must be the start phase's own process, its stdin a pipe carrying the
    JIT config, after the confirmed revocation, with the slot's name,
    network and limits.
    """
    failures = []
    detail = {
        r["name"]: r["detail"]
        for r in recs
        if r["kind"] == "event" and r["name"].startswith("run-")
    }
    if detail.get("run-pid") != str(cell.pid):
        failures.append(
            f"docker run ran as {detail.get('run-pid')}, "
            f"not the start phase's {cell.pid}"
        )
    if not re.fullmatch(r"pipe:\[\d+\]", detail.get("run-stdin", "")):
        failures.append(
            f"docker run's stdin is {detail.get('run-stdin')!r}, not a pipe"
        )
    if detail.get("run-config") != "ok":
        failures.append("docker run didn't read the JIT config on its stdin")
    run_t = next(
        r["t"]
        for r in recs
        if r["kind"] == "event"
        and r["name"] == "docker"
        and r["detail"].startswith("run\n")
    )
    revoke_t = [r["t"] for r in recs if confirmed(r)]
    if not revoke_t or revoke_t[-1] > run_t:
        failures.append("docker run came before the confirmed revocation")

    def after(flag: str) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv else None

    dns = json.loads(values["DNS"])
    pairs = list(itertools.pairwise(argv))
    tmpfs = (
        "/home/runner/_work:rw,exec,uid=1001,gid=1001,mode=0700,"
        f"size={values['WORK_SIZE']}"
    )
    checks = [
        (
            argv[:4] == ["run", "--rm", "--interactive", "--sig-proxy=true"],
            "run --rm --interactive --sig-proxy=true",
        ),
        (after("--name") == "actions-runner-slot-1", "--name"),
        (after("--label") == "actions-runner-pool.slot=1", "--label"),
        (
            after("--network") == "pool-slot-1" and argv.count("--network") == 1,
            "--network",
        ),
        (after("--cpus") == values["CPUS"], "--cpus"),
        (after("--memory") == values["MEMORY"], "--memory"),
        (after("--pids-limit") == values["PIDS_LIMIT"], "--pids-limit"),
        (after("--security-opt") == "no-new-privileges", "--security-opt"),
        ([b for a, b in pairs if a == "--dns"] == dns, "--dns"),
        (after("--tmpfs") == tmpfs, "--tmpfs"),
        (values["IMAGE"] in argv, "image"),
        (
            not [
                a
                for a in argv
                if re.search(r"docker[.]sock|^-v$|^--volume|^--mount|^--privileged", a)
            ],
            "mounts",
        ),
        (
            not [a for a in argv if MINTED_PREFIX in a or cell.jit in a],
            "secrets in argv",
        ),
    ]
    failures += [f"docker run: {what}: {argv}" for ok, what in checks if not ok]
    return failures


def self_test() -> None:
    """Check the token checks themselves.

    They must catch a DELETE after the revocation, a second revocation
    request, a missing one and one GitHub didn't confirm.
    """

    def recs(*calls: str, status: int | None = 204) -> list[Record]:
        out: list[Record] = [{"t": 0, "wall": 0, "kind": "minted", "token": 1}]
        for i, call in enumerate(calls, 1):
            out.append({"t": i, "wall": i, "kind": "api", "call": call, "token": 1})
            out.append(
                {
                    "t": i + 0.5,
                    "wall": i + 0.5,
                    "kind": "answer",
                    "call": call,
                    "token": 1,
                    "status": status,
                }
            )
        return out

    assert not token_failures(recs("delete:42", "revoke"))
    assert token_failures(recs("revoke", "delete:42"))
    assert token_failures(recs("revoke", "revoke"))
    assert token_failures(recs("delete:42"))
    assert token_failures(recs("delete:42", "revoke", status=500))
    assert token_failures(recs("delete:42", "revoke", status=None))
    assert not token_failures(recs("delete:42", "revoke", status=500), unrevoked=1)
    assert not token_failures(recs("delete:42", "revoke", status=401))
    assert held(recs("revoke", status=500))[0][2] == float("inf")
    assert overlaps([(1, 0, 5)], [(4, 6)])
    assert not overlaps([(1, 0, 5)], [(6, 7)])


def scripts_main(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    ctx: dict[str, Any] = {
        "workdir": workdir,
        "stubs": workdir / "stubs",
        "bin": workdir / "bin",
    }
    lib_dir = workdir / "lib" / "actions-runner"
    for path in (ctx["stubs"], ctx["bin"], lib_dir):
        path.mkdir(parents=True, exist_ok=True)
    ctx["sleep"] = shutil.which("sleep")
    ctx["start"] = ctx["bin"] / "actions-runner-slot"
    ctx["stop"] = ctx["bin"] / "actions-runner-slot-stop"
    for src, dest, mode in (
        (args.start, ctx["start"], 0o700),
        (args.stop, ctx["stop"], 0o700),
        (args.lib, lib_dir / "actions-runner-slot-lib.sh", 0o600),
    ):
        write_file(dest, Path(src).read_text(), mode)
    stubs = {
        "op": STUB_OP,
        "docker": STUB_DOCKER,
        "sleep": STUB_SLEEP,
        "systemctl": STUB_SYSTEMCTL,
    }
    for name, text in stubs.items():
        write_file(ctx["stubs"] / name, text, 0o755)
    search = f"{ctx['stubs']}:{os.environ['PATH']}"
    for name in stubs:
        if shutil.which(name, path=search) != str(ctx["stubs"] / name):
            sys.exit(f"the stub {name} doesn't come first on the PATH")
    ctx["key"], ctx["token"] = make_key_and_token(workdir)
    ctx["base"] = {
        **DEFAULT_ENV,
        **(read_env(Path(args.base_env)) if args.base_env else {}),
    }
    selected = [
        c for c in script_cells() if not args.only or re.search(args.only, c.name)
    ]
    server, ctx["url"] = serve(selected)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda c: run_script_cell(c, ctx), selected))
    server.shutdown()
    return report(results, workdir)


# --- units -------------------------------------------------------------------


def run(
    *argv: str | Path, check: bool = False, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        message = f"{argv}: {proc.returncode} {proc.stderr.strip()}"
        raise RuntimeError(message)
    return proc


def systemctl(
    *argv: str, check: bool = True, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return run("systemctl", "--user", *argv, check=check, timeout=timeout)


def show(unit: str) -> dict[str, str]:
    out = systemctl(
        "show",
        unit,
        "--property=ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,NRestarts,"
        "InvocationID,ConditionResult",
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def wait_for[T](
    predicate: Callable[[], T], timeout: float, step: float = 0.2
) -> T | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def overlaps(
    tokens: list[tuple[int, float, float]], containers: list[tuple[float, float]]
) -> list[tuple[tuple[int, float, float], tuple[float, float]]]:
    return [(t, c) for t in tokens for c in containers if t[1] < c[1] and c[0] < t[2]]


def container_events(slot: int, since: float, until: float) -> list[Record]:
    out = run(
        "docker",
        "events",
        "--since",
        f"{since:.3f}",
        "--until",
        f"{until:.3f}",
        "--filter",
        "type=container",
        "--filter",
        f"label=actions-runner-pool.slot={slot}",
        "--format",
        "{{json .}}",
        check=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def lifetimes(events: list[Record]) -> list[tuple[float, float]]:
    """Each container's [start, die] interval, in seconds.

    Docker 29's events carry the container's ID in Actor.ID only.
    """
    out, started = [], {}
    for event in events:
        action, cid = event["Action"], event["Actor"]["ID"]
        if action == "start":
            started[cid] = event["timeNano"] / 1e9
        elif action == "die" and cid in started:
            out.append((started.pop(cid), event["timeNano"] / 1e9))
    out += [(t, float("inf")) for t in started.values()]
    return out


@dataclass
class Case:
    name: str
    slot: int = 1
    jit: str = "clean"
    answers: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    unit_lines: str = ""
    # What the case does once started, and the unit state it then waits for.
    action: str = "settle"
    pattern: str = "T L J R T D R"
    containers: int = 0
    state: dict = field(default_factory=dict)
    # What every job line reports, or, as a list, each line in turn.
    job: dict | list = field(default_factory=dict)
    stop_within: float | None = None
    # The starts, when the calls don't show them all.
    starts: int | None = None


# What the stop phase reports of a start whose result systemd counted.
FAILED = {"outcome": "failed", "stop_requested": False}


def unit_cases() -> list[Case]:
    return [
        # Three jobs in a row, each with a slow DELETE in its stop phase
        # and a restart delay shortened to a second, so a start that ran
        # beside the previous stop phase would show in the order of calls.
        Case(
            "clean",
            answers={"delete:42": ["404@3"]},
            unit_lines="RestartSec=1",
            action="three-jobs",
            pattern="(T L J R T D R)+",
            containers=3,
            state={"ActiveState": "inactive", "Result": "success"},
            job={
                "registration": "gone",
                "service_result": "success",
                "exit_code": "exited",
                "exit_status": "0",
                "outcome": "clean",
                "stop_requested": False,
            },
        ),
        Case(
            "stop-start",
            answers={"list": ["ok@3"]},
            action="stop-at-list",
            stop_within=3 + 10,
            state={
                "ActiveState": "inactive",
                "Result": "success",
                "ExecMainCode": "1",
                "ExecMainStatus": "0",
            },
            job={
                "registration": "deleted",
                "service_result": "success",
                "exit_code": "exited",
                "exit_status": "0",
                "outcome": "stopped",
                "stop_requested": True,
            },
        ),
        # A real systemctl stop: the container ends on the TERM with 143,
        # which counts as success because a stop job was running.
        Case(
            "stop-job",
            jit="hold",
            action="stop-in-job",
            containers=1,
            stop_within=10,
            state={
                "ActiveState": "inactive",
                "Result": "success",
                "ExecMainCode": "1",
                "ExecMainStatus": "143",
            },
            job={
                "registration": "deleted",
                "service_result": "success",
                "exit_code": "exited",
                "exit_status": "143",
                "outcome": "stopped",
                "stop_requested": True,
            },
        ),
        Case(
            "kill-job",
            jit="hold",
            action="kill-in-job",
            containers=1,
            state={
                "SubState": "auto-restart",
                "Result": "signal",
                "ExecMainCode": "2",
                "ExecMainStatus": "9",
            },
            job={
                "registration": "deleted",
                "service_result": "signal",
                "exit_code": "killed",
                "exit_status": "KILL",
                **FAILED,
            },
        ),
        # The container's own exit status never stops the restarts: 78 and
        # 1 restart, and so does 143, which the stop phase turns into a
        # failure because no stop was requested.
        Case(
            "exit-137",
            jit="x137",
            action="auto-restart",
            containers=1,
            state={
                "SubState": "auto-restart",
                "Result": "exit-code",
                "ExecMainCode": "1",
                "ExecMainStatus": "137",
            },
            job={
                "registration": "deleted",
                "service_result": "exit-code",
                "exit_code": "exited",
                "exit_status": "137",
                **FAILED,
            },
        ),
        Case(
            "exit-78",
            jit="x78",
            action="auto-restart",
            containers=1,
            state={
                "SubState": "auto-restart",
                "Result": "exit-code",
                "ExecMainCode": "1",
                "ExecMainStatus": "78",
            },
            job={
                "registration": "deleted",
                "service_result": "exit-code",
                "exit_code": "exited",
                "exit_status": "78",
                **FAILED,
            },
        ),
        Case(
            "exit-143",
            jit="x143",
            action="auto-restart",
            containers=1,
            state={
                "SubState": "auto-restart",
                "Result": "exit-code",
                "ExecMainCode": "1",
                "ExecMainStatus": "143",
            },
            job={
                "registration": "deleted",
                "service_result": "success",
                "exit_code": "exited",
                "exit_status": "143",
                "status": 1,
                **FAILED,
            },
        ),
        Case(
            "exit-1",
            jit="x1",
            action="auto-restart",
            containers=1,
            state={
                "SubState": "auto-restart",
                "Result": "exit-code",
                "ExecMainCode": "1",
                "ExecMainStatus": "1",
            },
            job={
                "registration": "deleted",
                "service_result": "exit-code",
                "exit_code": "exited",
                "exit_status": "1",
                **FAILED,
            },
        ),
        Case(
            "start-75",
            answers={"jit": ["no-config"]},
            action="auto-restart",
            state={
                "SubState": "auto-restart",
                "Result": "exit-code",
                "ExecMainCode": "1",
                "ExecMainStatus": "75",
            },
            job={
                "registration": "deleted",
                "service_result": "exit-code",
                "exit_code": "exited",
                "exit_status": "75",
                **FAILED,
            },
        ),
        # A configuration error the condition check finds: systemd skips
        # the start and doesn't restart it.
        Case(
            "config-78",
            slot=2,
            env={"OP_TOKEN_FILE": "/nonexistent/token"},
            action="skipped",
            pattern="",
            state={
                "ActiveState": "inactive",
                "ConditionResult": "no",
                "NRestarts": "0",
            },
            job={
                "registration": "none",
                "service_result": "exec-condition",
                "exit_code": "",
                "exit_status": "",
                "outcome": "config-error",
                "stop_requested": False,
            },
        ),
        # A refusal from GitHub: the start phase leaves the marker and
        # exits 78, the unit restarts, and the next start's condition check
        # skips it.
        Case(
            "config-github",
            slot=2,
            answers={"token": ["401"]},
            unit_lines="RestartSec=1",
            action="skipped",
            pattern="T",
            starts=2,
            state={
                "ActiveState": "inactive",
                "ConditionResult": "no",
                "NRestarts": "1",
            },
            job=[
                {
                    "registration": "none",
                    "service_result": "exit-code",
                    "exit_code": "exited",
                    "exit_status": "78",
                    **FAILED,
                },
                {
                    "registration": "none",
                    "service_result": "exec-condition",
                    "exit_code": "",
                    "exit_status": "",
                    "outcome": "config-error",
                    "stop_requested": False,
                },
            ],
        ),
        # A manual stop of a job that ignores the TERM, whose container
        # docker rm then can't remove: the pending cleanup outlives the
        # stop, nothing restarts the slot, and the cleanup timer's service
        # later removes the container, then deletes the registration.
        Case(
            "stop-rm-fail",
            jit="ignore",
            unit_lines="TimeoutStopSec=5",
            action="rm-fail",
            containers=1,
            state={"ActiveState": "failed", "NRestarts": "0"},
            job={
                "registration": "skipped",
                "container": "failed",
                "pending": True,
                "status": 75,
                "outcome": "failed",
                "stop_requested": True,
            },
        ),
    ]


def write_drop_in(ctx: dict[str, Any], unit: str, lines: list[str]) -> Path:
    path = ctx["units"] / f"{unit}.d"
    path.mkdir(parents=True, exist_ok=True)
    write_file(path / "test.conf", "\n".join([*lines, ""]), 0o644)
    return path


@dataclass
class UnitRun:
    """A units case as it runs: its units and paths, and what it found."""

    case: Case
    cell: Cell
    ctx: dict[str, Any]
    failures: list[str] = field(default_factory=list)
    drop_ins: list[Path] = field(default_factory=list)
    stub_lines: list[str] = field(default_factory=list)
    state: dict[str, str] = field(default_factory=dict)
    stop_seconds: float | None = None
    unit: str = field(init=False)
    cleanup_service: str = field(init=False)
    cleanup_timer: str = field(init=False)
    container: str = field(init=False)
    case_dir: Path = field(init=False)
    output: Path = field(init=False)
    env_path: Path = field(init=False)
    pending_path: Path = field(init=False)
    marker_path: Path = field(init=False)

    def __post_init__(self) -> None:
        slot = self.case.slot
        self.unit = f"actions-runner@{slot}.service"
        self.cleanup_service = f"actions-runner-cleanup@{slot}.service"
        self.cleanup_timer = f"actions-runner-cleanup@{slot}.timer"
        self.container = f"actions-runner-slot-{slot}"
        self.case_dir = self.ctx["workdir"] / "cases" / self.case.name
        self.output = self.case_dir / "output.log"
        self.env_path = self.case_dir / "pool.env"
        state_dir = self.ctx["state"] / f"actions-runner-slot-{slot}"
        self.pending_path = state_dir / "pending"
        self.marker_path = state_dir / "config-error"

    def running(self) -> str:
        return run(
            "docker",
            "ps",
            "--quiet",
            "--filter",
            f"name=^{self.container}$",
            "--filter",
            "status=running",
            check=True,
        ).stdout.strip()

    def lines(self, event: str) -> list[Record]:
        try:
            text = self.output.read_text()
        except OSError:
            return []
        return [e for e in json_lines(text) if e.get("event") == event]

    def job_lines(self) -> list[Record]:
        return self.lines("job")

    def timed_stop(self) -> float:
        begin = time.monotonic()
        systemctl("stop", self.unit)
        return time.monotonic() - begin


def prepare_unit_case(r: UnitRun) -> None:
    """Write the case's env file and the slot unit's drop-in."""
    shutil.rmtree(r.case_dir, ignore_errors=True)
    r.case_dir.mkdir(parents=True)
    write_env(
        r.env_path,
        {
            **r.ctx["base"],
            "API_URL": f"{r.ctx['url']}/{r.cell.cid}",
            "IMAGE": r.ctx["image"],
            "OP_TOKEN_FILE": r.ctx["token"],
            **r.case.env,
        },
    )
    r.stub_lines = [
        "[Service]",
        f"Environment=PATH={r.ctx['stubs']}:/usr/local/bin:/usr/bin:/bin",
        f"Environment=ACTIONS_RUNNER_POOL_ENV={r.env_path}",
        f"Environment=CELL_KEY={r.ctx['key']}",
        f"StandardOutput=append:{r.output}",
    ]
    if r.case.action == "rm-fail":
        # The test-only docker whose rm fails, for this slot unit alone.
        rm_fail = r.case_dir / "rm-fail"
        rm_fail.mkdir()
        write_file(
            rm_fail / "docker",
            "#!/bin/sh\n"
            'if [ "$1" = rm ]; then\n'
            '  echo "Error response from daemon: test failure" >&2\n'
            "  exit 1\n"
            "fi\n"
            f'exec {r.ctx["docker"]} "$@"\n',
            0o755,
        )
        slot_lines = [
            *r.stub_lines[:1],
            f"Environment=PATH={rm_fail}:{r.ctx['stubs']}:/usr/local/bin:/usr/bin:/bin",
            *r.stub_lines[2:],
        ]
    else:
        slot_lines = r.stub_lines
    r.drop_ins.append(write_drop_in(r.ctx, r.unit, [*slot_lines, r.case.unit_lines]))


def wait_for_restart(unit: str) -> dict[str, str]:
    return wait_for(
        lambda: (s := show(unit))["SubState"] == "auto-restart" and s, 60
    ) or show(unit)


def act_three_jobs(r: UnitRun) -> None:
    if not wait_for(lambda: len(r.job_lines()) >= 3, 120):
        r.failures.append(f"only {len(r.job_lines())} jobs ran")
    restarts = show(r.unit).get("NRestarts", "")
    # A clean exit resets the counter, so the delay stays the first one.
    if not restarts.isdigit() or int(restarts) > 1:
        r.failures.append(f"NRestarts={restarts} after clean jobs")
    # Stopped between starts if the poll catches the gap; otherwise
    # the last job line may come from a stop at any point.
    wait_for(lambda: show(r.unit)["SubState"] == "auto-restart", 30, step=0.05)
    r.timed_stop()
    r.state = show(r.unit)


def act_stop_at_list(r: UnitRun) -> None:
    if not wait_for(lambda: r.cell.counts.get("list"), 30):
        r.failures.append("the start phase never listed the runners")
    r.stop_seconds = r.timed_stop()
    r.state = show(r.unit)


def act_in_job(r: UnitRun) -> None:
    if not wait_for(r.running, 60):
        r.failures.append("the job container never ran")
    if r.case.action == "stop-in-job":
        r.stop_seconds = r.timed_stop()
        r.state = show(r.unit)
    else:
        systemctl("kill", "--kill-whom=main", "--signal=SIGKILL", r.unit)
        r.state = wait_for_restart(r.unit)


def act_auto_restart(r: UnitRun) -> None:
    r.state = wait_for_restart(r.unit)


def act_skipped(r: UnitRun) -> None:
    want = len(r.case.job) if isinstance(r.case.job, list) else 1
    wait_for(
        lambda: (
            len(r.job_lines()) >= want and show(r.unit)["ActiveState"] == "inactive"
        ),
        60,
    )
    # Long enough for a restart that shouldn't come.
    time.sleep(3)
    r.state = show(r.unit)
    if r.case.name == "config-github":
        try:
            if (
                r.marker_path.read_text()
                != "the installation token request: HTTP 401\n"
            ):
                r.failures.append("the config-error marker doesn't name the refusal")
        except OSError:
            r.failures.append("no config-error marker")


def act_rm_fail(r: UnitRun) -> None:
    if not wait_for(r.running, 60):
        r.failures.append("the job container never ran")
    systemctl("stop", r.unit, check=False)
    r.state = show(r.unit)
    # Nothing restarts a stopped slot, and the container still runs.
    time.sleep(3)
    try:
        with r.pending_path.open() as handle:
            if json.load(handle) != {
                "runner_id": RUNNER_ID,
                "container": r.container,
            }:
                r.failures.append(
                    "the pending cleanup doesn't name the runner and the container"
                )
    except OSError, ValueError:
        r.failures.append("the stop left no pending cleanup")
    if not r.running():
        r.failures.append("the container stopped before the retry")
    if (
        letters(r.cell) != "T L J R".replace(" ", "")
        or show(r.unit)["ActiveState"] != "failed"
    ):
        active = show(r.unit)["ActiveState"]
        r.failures.append(f"before the retry: calls {letters(r.cell)}, unit {active}")
    act_retry(r)


def act_retry(r: UnitRun) -> None:
    """Resolve the pending cleanup through the cleanup timer's service.

    The retry path runs with the stub op and a timer that fires every 2
    seconds instead of every 5 minutes.
    """
    r.drop_ins.append(write_drop_in(r.ctx, r.cleanup_service, r.stub_lines))
    r.drop_ins.append(
        write_drop_in(
            r.ctx,
            r.cleanup_timer,
            ["[Timer]", "OnCalendar=", "OnCalendar=*:*:0/2", "AccuracySec=1s"],
        )
    )
    systemctl("daemon-reload")
    systemctl("start", r.cleanup_timer)
    if not wait_for(lambda: not r.pending_path.exists(), 60):
        r.failures.append("the cleanup timer never resolved the pending cleanup")
    systemctl("stop", r.cleanup_timer)
    wait_for(
        lambda: show(r.cleanup_service)["ActiveState"] in ("inactive", "failed"),
        30,
    )
    cleanups = r.lines("cleanup")
    want = {
        "mode": "retry",
        "slot": r.case.slot,
        "runner_id": RUNNER_ID,
        "container": "removed",
        "registration": "deleted",
        "pending": False,
        "status": 0,
    }
    if len(cleanups) != 1 or any(cleanups[0].get(k) != v for k, v in want.items()):
        r.failures.append(f"cleanup lines {cleanups}, expected {want}")
    if show(r.unit)["ActiveState"] != "failed" or r.cell.counts.get("jit") != 1:
        active = show(r.unit)["ActiveState"]
        r.failures.append(
            f"a job started: unit {active}, {r.cell.counts.get('jit')} JIT calls"
        )


# What each units case does once started, and the unit state it then waits
# for; a case whose action has none here only settles.
UNIT_ACTIONS = {
    "three-jobs": act_three_jobs,
    "stop-at-list": act_stop_at_list,
    "stop-in-job": act_in_job,
    "kill-in-job": act_in_job,
    "auto-restart": act_auto_restart,
    "skipped": act_skipped,
    "rm-fail": act_rm_fail,
}


def run_unit_case(case: Case, cell: Cell, ctx: dict[str, Any]) -> dict[str, object]:
    r = UnitRun(case, cell, ctx)
    prepare_unit_case(r)
    systemctl("daemon-reload")
    systemctl("reset-failed", r.unit, check=False)
    since = time.time()
    try:
        systemctl("start", r.unit)
        action = UNIT_ACTIONS.get(case.action)
        if action:
            action(r)
    finally:
        systemctl("stop", r.unit, check=False)
        systemctl("reset-failed", r.unit, r.cleanup_service, check=False)
        for path in r.drop_ins:
            shutil.rmtree(path, ignore_errors=True)
        systemctl("daemon-reload", check=False)
        marker_left = r.marker_path.exists()
        if marker_left:
            r.marker_path.unlink()
    until = time.time() + 1
    time.sleep(1)
    return unit_case_result(r, since, until, marker_left=marker_left)


def unit_case_result(
    r: UnitRun, since: float, until: float, *, marker_left: bool
) -> dict[str, object]:
    case, failures = r.case, r.failures
    for key, want in case.state.items():
        if r.state.get(key) != want:
            failures.append(f"{key}={r.state.get(key)}, expected {want}")
    if r.stop_seconds is not None and r.stop_seconds > case.stop_within:
        failures.append(
            f"the stop took {r.stop_seconds:.1f} s, more than {case.stop_within}"
        )
    seen = letters(r.cell)
    if not re.fullmatch(case.pattern.replace(" ", ""), seen):
        failures.append(f"calls {seen}, expected {case.pattern}")
    failures += token_failures(r.cell.records)
    jobs = r.job_lines()
    failures += job_line_failures(case, jobs, seen)
    if marker_left != (case.name == "config-github"):
        failures.append(f"config-error marker left: {marker_left}")
    # No token held while a container ran, and every container gone.
    events = container_events(case.slot, since, until)
    lives = lifetimes(events)
    failures += container_failures(r, events, lives)
    text = r.output.read_text() if r.output.exists() else ""
    if MINTED_PREFIX in text or any(c in text for c in JIT_CONFIGS.values()):
        failures.append("a secret in the unit's output")
    return {
        "name": case.name,
        "passed": not failures,
        "failures": failures,
        "calls": seen,
        "state": r.state,
        "stop_seconds": None if r.stop_seconds is None else round(r.stop_seconds, 1),
        "containers": len(lives),
        "jobs": len(jobs),
    }


def job_line_failures(case: Case, jobs: list[Record], seen: str) -> list[str]:
    """Check the job lines, one per start.

    ExecStopPost ran once per start: one job line per invocation, one
    invocation per start. A final stop in the preflight registers nothing,
    so the clean case may have one start more than the calls show.
    """
    failures = []
    starts = case.starts or max(seen.count("TL"), 1)
    invocations = [j.get("invocation") for j in jobs]
    extra = 1 if case.action == "three-jobs" else 0
    if (
        not starts <= len(jobs) <= starts + extra
        or len(set(invocations)) != len(jobs)
        or not all(invocations)
    ):
        failures.append(
            f"{len(jobs)} job lines for {starts} starts, invocations {invocations}"
        )
    for i, job in enumerate(jobs):
        expected = (
            case.job[min(i, len(case.job) - 1)]
            if isinstance(case.job, list)
            else case.job
        )
        want = {
            "slot": case.slot,
            "container": "removed",
            "pending": False,
            "status": 0,
            **expected,
        }
        if case.action == "three-jobs" and job is jobs[-1]:
            want = {
                k: v
                for k, v in want.items()
                if k
                not in (
                    "service_result",
                    "exit_code",
                    "exit_status",
                    "registration",
                    "outcome",
                    "stop_requested",
                )
            }
        if any(job.get(k) != v for k, v in want.items()):
            failures.append(f"job line {job}, expected {want}")
    return failures


def container_failures(
    r: UnitRun, events: list[Record], lives: list[tuple[float, float]]
) -> list[str]:
    case = r.case
    failures = []
    if len(lives) < case.containers or (
        case.action != "three-jobs" and len(lives) != case.containers
    ):
        failures.append(f"{len(lives)} containers ran, expected {case.containers}")
    if overlaps(held(r.cell.records), lives):
        failures.append(
            "a token was held while a container ran: "
            f"{overlaps(held(r.cell.records), lives)}"
        )
    destroyed = sum(1 for e in events if e["Action"] == "destroy")
    if destroyed != len(lives):
        failures.append(f"{destroyed} containers removed of {len(lives)}")
    left = run(
        "docker",
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label=actions-runner-pool.slot={case.slot}",
        check=True,
    ).stdout.strip()
    if left:
        failures.append(f"containers left: {left}")
    if r.pending_path.exists():
        failures.append("a pending cleanup outlived the case")
    return failures


PROBE_HANG = """#!/bin/sh
echo "post $SERVICE_RESULT $EXIT_CODE $EXIT_STATUS" >>"$1"
sleep 300
echo post-end >>"$1"
"""
PROBE_START = """#!/bin/sh
echo start >>"$1"
[ "$2" != hold ] || exec sleep 300
"""
PROBE_POST = """#!/bin/sh
echo begin >>"$1"
sleep "$2"
echo end >>"$1"
"""
PROBE_CONDITION = """#!/bin/sh
echo condition >>"$1"
exit 78
"""
PROBE_RESULT = """#!/bin/sh
echo "post $SERVICE_RESULT" >>"$1"
"""


def probes(ctx: dict[str, Any]) -> list[dict[str, object]]:
    """The systemd behavior the design relies on, on this systemd."""
    probe_dir = ctx["workdir"] / "probes"
    shutil.rmtree(probe_dir, ignore_errors=True)
    probe_dir.mkdir(parents=True)
    for name, text in (
        ("hang", PROBE_HANG),
        ("start", PROBE_START),
        ("post", PROBE_POST),
        ("condition", PROBE_CONDITION),
        ("result", PROBE_RESULT),
    ):
        write_file(probe_dir / f"{name}.sh", text, 0o755)
    suffix = ctx["run_id"]
    return [
        probe_stop_post_after_kill(probe_dir, suffix),
        probe_restart_waits(probe_dir, suffix),
        probe_start_waits(probe_dir, suffix),
        probe_condition(probe_dir, suffix),
    ]


def log_words(path: Path) -> list[str]:
    try:
        return path.read_text().split()
    except OSError:
        return []


def transient(unit: str, *argv: str | Path) -> None:
    run("systemd-run", "--user", f"--unit={unit}", *argv, check=True)


def settle(unit: str, timeout: float = 30) -> bool | None:
    return wait_for(
        lambda: show(unit)["ActiveState"] in ("inactive", "failed"), timeout
    )


def probe_stop_post_after_kill(probe_dir: Path, suffix: str) -> dict[str, object]:
    """Check ExecStopPost after a SIGKILL of the main process.

    It runs, gets the result in its environment, and TimeoutStopSec= ends
    it.
    """
    unit, log = (
        f"runner-pool-probe-timeout-{suffix}",
        probe_dir / "timeout.log",
    )
    failures = []
    transient(
        unit,
        "-p",
        "TimeoutStopSec=2",
        "-p",
        "KillMode=mixed",
        "-p",
        f"ExecStopPost={probe_dir}/hang.sh {log}",
        shutil.which("sleep"),
        "300",
    )
    time.sleep(1)
    begin = time.monotonic()
    systemctl("kill", "--kill-whom=main", "--signal=SIGKILL", unit)
    settle(unit)
    seconds = time.monotonic() - begin
    if " ".join(log_words(log)) != "post signal killed KILL":
        failures.append(f"stop-post saw {log_words(log)}")
    if not 2 <= seconds <= 10:
        failures.append(
            f"the unit took {seconds:.1f} s to stop, "
            "against TimeoutStopSec=2 for the stop-post"
        )
    systemctl("reset-failed", unit, check=False)
    return {
        "name": "probe/stop-post-after-kill-and-timeout",
        "passed": not failures,
        "failures": failures,
        "seconds": round(seconds, 1),
    }


def probe_restart_waits(probe_dir: Path, suffix: str) -> dict[str, object]:
    """Under Restart=, the next start waits for the stop-post."""
    unit, log = (
        f"runner-pool-probe-restart-{suffix}",
        probe_dir / "restart.log",
    )
    failures = []
    transient(
        unit,
        "-p",
        "Restart=always",
        "-p",
        "RestartSec=100ms",
        "-p",
        "StartLimitIntervalSec=0",
        "-p",
        f"ExecStopPost={probe_dir}/post.sh {log} 1",
        f"{probe_dir}/start.sh",
        log,
    )
    time.sleep(4)
    systemctl("stop", unit)
    seen = log_words(log)
    if len(seen) < 9 or seen != ["start", "begin", "end"] * (len(seen) // 3):
        failures.append(f"order {seen}")
    systemctl("reset-failed", unit, check=False)
    return {
        "name": "probe/restart-waits-for-stop-post",
        "passed": not failures,
        "failures": failures,
        "order": " ".join(seen),
    }


def probe_start_waits(probe_dir: Path, suffix: str) -> dict[str, object]:
    """A start requested during the stop-post waits for it too."""
    unit, log = (
        f"runner-pool-probe-start-{suffix}",
        probe_dir / "start.log",
    )
    failures = []
    transient(
        unit,
        "-p",
        f"ExecStopPost={probe_dir}/post.sh {log} 2",
        f"{probe_dir}/start.sh",
        log,
        "hold",
    )
    time.sleep(0.5)
    systemctl("stop", "--no-block", unit)
    if not wait_for(lambda: show(unit)["SubState"] == "stop-post", 10):
        failures.append("the unit never reached stop-post")
    systemctl("start", unit)
    time.sleep(0.5)
    seen = log_words(log)
    if seen != ["start", "begin", "end", "start"]:
        failures.append(f"order {seen}")
    systemctl("stop", unit, check=False)
    systemctl("reset-failed", unit, check=False)
    return {
        "name": "probe/start-waits-for-stop-post",
        "passed": not failures,
        "failures": failures,
        "order": " ".join(seen),
    }


def probe_condition(probe_dir: Path, suffix: str) -> dict[str, object]:
    """Check an ExecCondition= exit of 78 under Restart=always.

    It skips the start and nothing restarts it; ExecStopPost still runs and
    sees exec-condition.
    """
    unit, log = (
        f"runner-pool-probe-condition-{suffix}",
        probe_dir / "condition.log",
    )
    failures = []
    transient(
        unit,
        "-p",
        "Restart=always",
        "-p",
        "RestartSec=100ms",
        "-p",
        "StartLimitIntervalSec=0",
        "-p",
        f"ExecCondition={probe_dir}/condition.sh {log}",
        "-p",
        f"ExecStopPost={probe_dir}/result.sh {log}",
        shutil.which("sleep"),
        "300",
    )
    time.sleep(2)
    seen = log_words(log)
    if seen != ["condition", "post", "exec-condition"]:
        failures.append(f"order {seen}")
    systemctl("stop", unit, check=False)
    systemctl("reset-failed", unit, check=False)
    return {
        "name": "probe/condition-stops-restarts",
        "passed": not failures,
        "failures": failures,
        "order": " ".join(seen),
    }


def units_main(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    stubs = workdir / "stubs"
    stubs.mkdir(parents=True, exist_ok=True)
    write_file(stubs / "op", '#!/bin/sh\ncat "$CELL_KEY"\n', 0o755)
    key, token = make_key_and_token(workdir)
    home = Path(os.environ["HOME"])
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local" / "state")
    ctx = {
        "workdir": workdir,
        "stubs": stubs,
        "key": key,
        "token": token,
        "image": args.image,
        "base": read_env(Path(args.base_env)),
        "state": state,
        "run_id": f"{time.time_ns()}",
        "docker": shutil.which("docker"),
        "units": home / ".config" / "systemd" / "user",
    }
    selected = [
        c for c in unit_cases() if not args.only or re.search(args.only, c.name)
    ]
    cells = []
    for i, case in enumerate(selected):
        cells.append(
            Cell(
                case.name,
                "units",
                answers=case.answers,
                jit=JIT_CONFIGS[case.jit],
                cid=f"u{i:02d}",
            )
        )
    server, ctx["url"] = serve(cells)
    results = []
    # The cleanup timers stay stopped while the cases run, so only the
    # case that tests the retry path starts one.
    timers = [f"actions-runner-cleanup@{slot}.timer" for slot in (1, 2)]
    systemctl("stop", *timers, check=False)
    try:
        for case, cell in zip(selected, cells, strict=True):
            try:
                results.append(run_unit_case(case, cell, ctx))
            except Exception as error:  # noqa: BLE001 -- a case's crash is its failure
                results.append(
                    {
                        "name": case.name,
                        "passed": False,
                        "failures": [f"{type(error).__name__}: {error}"],
                    }
                )
    finally:
        systemctl("start", *timers, check=False)
    server.shutdown()
    if not args.only or re.search(args.only, "probe"):
        try:
            results += probes(ctx)
        except Exception as error:  # noqa: BLE001 -- a probe's crash is its failure
            results.append(
                {
                    "name": "probe",
                    "passed": False,
                    "failures": [f"{type(error).__name__}: {error}"],
                }
            )
    return report(results, workdir)


def report(results: list[dict[str, Any]], workdir: Path) -> int:
    for result in results:
        mark = "PASS" if result["passed"] else "FAIL"
        print(
            f"{mark} {result['name']} "
            + " ".join(
                f"{k}={result[k]}" for k in ("exit", "calls", "seconds") if k in result
            )
        )
        for failure in result["failures"]:
            print(f"     {failure}")
    passed = sum(r["passed"] for r in results)
    print(f"{passed} of {len(results)} passed")
    with (workdir / "results.json").open("w") as handle:
        json.dump(results, handle, indent=1)
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="suite", required=True)
    scripts = sub.add_parser("scripts")
    scripts.add_argument("start")
    scripts.add_argument("stop")
    scripts.add_argument("lib")
    scripts.add_argument("workdir")
    scripts.add_argument("--base-env")
    scripts.add_argument("--only")
    scripts.add_argument("--jobs", type=int, default=16)
    units = sub.add_parser("units")
    units.add_argument("base_env")
    units.add_argument("workdir")
    units.add_argument("--image", required=True)
    units.add_argument("--only")
    args = parser.parse_args()
    self_test()
    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    return scripts_main(args) if args.suite == "scripts" else units_main(args)


if __name__ == "__main__":
    sys.exit(main())
