#!/usr/bin/env bash
# actions-runner-slot -- the start phase of one runner slot: it registers
# a single-use runner, then becomes the job's docker client, so the slot's
# systemd user unit runs one GitHub Actions job per start.
#
# In order, once:
#
#   1. checks the env file, the token file and the state directory, then
#      the Docker daemon: it pulls the pinned image if it is missing,
#      removes the container a killed run left behind, and creates the
#      slot's own bridge network if it doesn't exist;
#   2. exits 0 if a stop was requested, before any credential exists;
#   3. if an earlier start's cleanup is pending, runs
#      actions-runner-slot-stop --pending, which removes that container and
#      deletes that registration with a token of its own, and exits 75
#      unless both resolved; then exits 0 if a stop was requested;
#   4. reads the App's private key with op read, signs an App JWT and
#      exchanges it for an installation token;
#   5. deletes the offline, idle runners a killed run of this slot left
#      registered;
#   6. asks the org for a single-use JIT runner config, and records the
#      runner's ID and the container's name, which aren't secret, as the
#      slot's pending cleanup in the unit's state directory;
#   7. revokes the installation token, so no token is held while a job
#      runs, and exits 75 unless GitHub confirmed the revocation;
#   8. exits 0 if a stop was requested;
#   9. execs docker run, the JIT config on its stdin through a pipe.
#
# After the exec the docker client is the unit's main process, and its
# exit status is the container's. systemd then runs actions-runner-slot-stop,
# the unit's ExecStopPost=, however the main process ended: it removes
# the container, deletes the registration and revokes its own token. The
# role's README states the invariants, under "Slot lifecycle".
#
# Nothing here waits on a child it started in the background, sleeps or
# backs off. Every call has a limit, a rate limit exits 75 at once, and
# the unit's restart delay is the backoff. INT, TERM and HUP only record
# the first signal, which steps 2, 3 and 8 read; the handlers run no
# cleanup.
#
# The JIT config lives in a shell variable and a pipe only: never in a
# file, an argument list or the container's stored configuration. The
# settings come from the env file the actions_runner_pool role writes.
#
# The runner's --disableupdate is a config.sh option. Runner.Listener's run
# command, which a JIT config feeds, rejects it, so the image digest pin is
# what holds the version.
#
# The exit status, until the exec:
#
#   0   a stop was requested, or a dry run or a condition check passed;
#   75  EX_TEMPFAIL, a failure a later start may not hit: the daemon, the
#       network, 1Password, a GitHub error or rate limit, any 403 that
#       isn't plainly a refusal, a pending cleanup or a revocation GitHub
#       didn't confirm; the unit retries after its restart delay;
#   78  EX_CONFIG, a configuration error no retry fixes: the env file, the
#       token file, the resolver list, a 401 from GitHub, a 403 that isn't
#       a rate limit and whose body is a GitHub error, or a 404 for the
#       installation or the runner group.
#
# No exit status stops the unit's restarts, because after the exec the
# container's would too. The unit's ExecCondition= runs this script with
# --condition instead, before each start: it checks the settings, the
# token file and the state directory, and the config-error marker that a
# refusal from GitHub leaves in the state directory, and exits 78 on any
# of them. systemd then skips the start, and Restart= doesn't fire. The
# marker holds until the env file or the token file changes, or the
# operator removes it.
#
# Usage: actions-runner-slot [--dry-run | --condition] SLOT
#
# --dry-run makes no docker call. Like any other start, it deletes the
# slot's stale runners, registers a runner, records its ID and revokes the
# token. It then prints the network and run arguments it would use and
# exits 0, which leaves the registration to actions-runner-slot-stop. It
# refuses to start while cleanup is pending.
set -euo pipefail
umask 077

# shellcheck source=SCRIPTDIR/actions-runner-slot-lib.sh
. "${BASH_SOURCE[0]%/*}/../lib/actions-runner/actions-runner-slot-lib.sh"

usage() {
  echo "usage: actions-runner-slot [--dry-run | --condition] SLOT" >&2
  exit 2
}

dry_run=false
condition=false
case ${1:-} in
--dry-run)
  dry_run=true
  shift
  ;;
--condition)
  condition=true
  shift
  ;;
esac
[[ $# -eq 1 && $1 =~ ^[1-9][0-9]*$ ]] || usage
slot=$1

stop_signal=
trap 'stop_signal=${stop_signal:-INT}' INT
trap 'stop_signal=${stop_signal:-TERM}' TERM
trap 'stop_signal=${stop_signal:-HUP}' HUP

# stop_check STEP: exits 0 if a signal was recorded.
stop_check() {
  [[ -n $stop_signal ]] || return 0
  jq -cn --argjson slot "$slot" --arg signal "$stop_signal" --arg step "$1" \
    '{event: "stopped", slot: $slot, signal: $signal, step: $step}' >&2
  exit 0
}

# config_error MESSAGE: a refusal from GitHub that no retry fixes. It
# leaves the marker the next start's condition check honours, then exits
# through die.
config_error() {
  printf '%s\n' "$1" >"$marker" || :
  die "$EX_CONFIG" "$1"
}

# api_fail WHAT: exits after a failed call with the status the unit reads,
# through die, which revokes the token first if one is held.
api_fail() {
  local what=$1
  case $api_status in
  op) die "$EX_TEMPFAIL" "op couldn't read the App's private key" ;;
  bad) die "$EX_TEMPFAIL" "$what: the response doesn't parse" ;;
  401 | 404) config_error "$what: HTTP $api_status" ;;
  403 | 429)
    # GitHub sends the x-ratelimit headers on every response. A spent
    # limit is the primary one. A Retry-After, or a body that names the
    # secondary limit or abuse detection, is a secondary one, which can
    # leave quota. A 403 is a refusal only when it is neither and its body
    # is a GitHub error; anything else is retried, without the marker.
    local kind=
    if [[ $api_remaining == 0 ]]; then
      kind=primary
    elif [[ $api_status == 429 || -n $api_retry_header ]] || printf '%s' "$api_body" | jq -e 'type == "object" and
      (((.documentation_url | strings) // "" | test("secondary-rate-limit|abuse-rate-limit"; "i"))
        or ((.message | strings) // "" | test("secondary rate limit|abuse detection"; "i")))' \
      >/dev/null 2>&1; then
      kind=secondary
    elif printf '%s' "$api_body" | jq -e 'type == "object" and (.message | type == "string")' >/dev/null 2>&1; then
      config_error "$what: HTTP 403"
    else
      die "$EX_TEMPFAIL" "$what: HTTP 403, neither a rate limit nor a GitHub error"
    fi
    jq -cn --argjson slot "$slot" --arg status "$api_status" --arg kind "$kind" \
      --arg retry_after "$api_retry_after" --arg reset "$api_reset" \
      '{event: "rate-limited", slot: $slot, status: $status, kind: $kind,
        retry_after: ($retry_after | tonumber? // null), reset: ($reset | tonumber? // null)}' >&2
    die "$EX_TEMPFAIL" "$what: HTTP $api_status"
    ;;
  *) die "$EX_TEMPFAIL" "$what: HTTP $api_status" ;;
  esac
}

load_env ORG APP_ID INSTALLATION_ID RUNNER_GROUP_ID LABELS DNS IMAGE CPUS MEMORY PIDS_LIMIT \
  WORK_SIZE OP_TOKEN_FILE OP_KEY_VAULT OP_KEY_ITEM OP_KEY_FIELD API_URL HOST_ALIAS
[[ $ACTIONS_RUNNER_POOL_RUNNER_GROUP_ID =~ ^[0-9]+$ ]] ||
  die "$EX_CONFIG" "ACTIONS_RUNNER_POOL_RUNNER_GROUP_ID is not a number"
jq -e 'type == "array" and length > 0 and all(type == "string")' \
  <<<"$ACTIONS_RUNNER_POOL_LABELS" >/dev/null 2>&1 ||
  die "$EX_CONFIG" "ACTIONS_RUNNER_POOL_LABELS is not a JSON list of strings"

# Public resolvers, because the egress table blocks the host's private or tailnet ones.
dns_list=$(jq -er '.[]' <<<"$ACTIONS_RUNNER_POOL_DNS" 2>/dev/null) ||
  die "$EX_CONFIG" "ACTIONS_RUNNER_POOL_DNS lists no resolver"
mapfile -t dns <<<"$dns_list"
dns_args=()
for server in "${dns[@]}"; do
  dns_args+=(--dns "$server")
done

check_token_file ||
  die "$EX_CONFIG" "the service-account token file must be mode 0400 and owned by the service user"
# systemd creates the directory, which outlives each start, a stop and a
# reboot, so the slot's pending cleanup does too.
[[ -n ${STATE_DIRECTORY:-} && -d $STATE_DIRECTORY ]] ||
  die "$EX_CONFIG" "STATE_DIRECTORY doesn't name the unit's state directory"
pending_file=$STATE_DIRECTORY/pending
marker=$STATE_DIRECTORY/config-error

# The condition check ends here, before any docker call. A marker older
# than the env file or the token file came before the operator's last
# change, so it no longer holds.
if [[ $condition == true ]]; then
  if [[ -f $marker ]]; then
    if [[ $marker -nt $env_file && $marker -nt $ACTIONS_RUNNER_POOL_OP_TOKEN_FILE ]]; then
      die "$EX_CONFIG" "$(<"$marker"), until the env file or the token file changes or $marker is removed"
    fi
    rm -f -- "$marker"
  fi
  exit 0
fi

# Each slot has a bridge of its own. The rootless daemon's isolation rules
# keep traffic from crossing between bridges, and with inter-container
# traffic off a job can't reach another container on its own bridge
# either. Neither path leaves the daemon's namespace, so the host's egress
# table never sees it.
network=$ACTIONS_RUNNER_POOL_NAME_PREFIX-slot-$slot
network_args=(
  network create --driver bridge
  --label "actions-runner-pool.slot=$slot"
  --opt com.docker.network.bridge.enable_icc=false
  "$network"
)
# actions-runner-slot-stop finds the container by this name.
container=actions-runner-slot-$slot

# The runner, named <prefix>-<host>-<slot>-<8 hex digits>.
prefix=$ACTIONS_RUNNER_POOL_NAME_PREFIX-$ACTIONS_RUNNER_POOL_HOST_ALIAS-$slot-
runner=$prefix$(openssl rand -hex 4)
body=$(jq -cn --arg name "$runner" --argjson group "$ACTIONS_RUNNER_POOL_RUNNER_GROUP_ID" \
  --argjson labels "$ACTIONS_RUNNER_POOL_LABELS" \
  '{name: $name, runner_group_id: $group, labels: $labels, work_folder: "_work"}')

# docker_call ARGS...: one preflight docker call, ended after
# DOCKER_SECONDS. timeout then returns 124, or 137 once it has had to kill.
docker_call() {
  timeout --kill-after="$KILL_AFTER" "$DOCKER_SECONDS" docker "$@"
}

# 1. The daemon, the image, the slot's leftover container and its network,
# all before any credential exists.
if [[ $dry_run == false ]]; then
  docker_call info --format '{{.ServerVersion}}' >/dev/null || die "$EX_TEMPFAIL" "the Docker daemon doesn't answer"
  inspect=0
  docker_call image inspect --format '{{.Id}}' "$ACTIONS_RUNNER_POOL_IMAGE" >/dev/null 2>&1 || inspect=$?
  ((inspect != 124 && inspect != 137)) || die "$EX_TEMPFAIL" "docker image inspect timed out"
  if ((inspect != 0)); then
    timeout --kill-after="$KILL_AFTER" "$PULL_SECONDS" docker pull --quiet "$ACTIONS_RUNNER_POOL_IMAGE" >/dev/null ||
      die "$EX_TEMPFAIL" "couldn't pull the runner image"
  fi
  # A container a killed run left behind would hold the slot's name, network and limits.
  stale=$(docker_call ps --all --quiet --filter "label=actions-runner-pool.slot=$slot") ||
    die "$EX_TEMPFAIL" "couldn't list the slot's containers"
  if [[ -n $stale ]]; then
    mapfile -t stale_ids <<<"$stale"
    docker_call rm --force "${stale_ids[@]}" >/dev/null ||
      die "$EX_TEMPFAIL" "couldn't remove the slot's leftover container"
  fi
  inspect=0
  icc=$(docker_call network inspect --format '{{index .Options "com.docker.network.bridge.enable_icc"}}' \
    "$network" 2>/dev/null) || inspect=$?
  ((inspect != 124 && inspect != 137)) || die "$EX_TEMPFAIL" "docker network inspect timed out"
  ((inspect == 0)) || icc=missing
  if [[ $icc != false ]]; then
    if [[ $icc != missing ]]; then
      docker_call network rm "$network" >/dev/null || die "$EX_TEMPFAIL" "couldn't replace the network $network"
    fi
    docker_call "${network_args[@]}" >/dev/null || die "$EX_TEMPFAIL" "couldn't create the network $network"
  fi
fi

# 2. A stop that came during the preflight ends the run before any
# credential exists.
stop_check preflight

# 3. An earlier start's cleanup, which a stop or a failed cleanup left
# pending: the stop phase removes its container and deletes its
# registration with a token of its own, under the slot's lock. A cleanup
# that doesn't resolve both leaves the state for the next try, and no job
# runs. It ends within the stop budget, and a stop that came meanwhile
# ends the run at the check after it. From that check on, the run ends
# within START_BOUND. The unit's stop timeout outlasts both.
if [[ -e $pending_file ]]; then
  [[ $dry_run == false ]] || die "$EX_TEMPFAIL" "an earlier start's cleanup is pending"
  "${BASH_SOURCE[0]%/*}/actions-runner-slot-stop" --pending "$slot" ||
    die "$EX_TEMPFAIL" "an earlier start's cleanup is still pending"
  stop_check cleanup
fi

# 4. The installation token. From here until its revocation, every
# failure leaves through die, which revokes it.
api_seconds=$STEP_SECONDS
mint_token || api_fail "the installation token request"

# 5. This slot's runners named <prefix>-<host>-<slot>-<8 hex digits> that
# are offline and idle: a killed run's registrations, since the slot's
# last container is gone. The whole list comes first, because each
# deletion moves the later pages. No call starts after the reconcile's
# deadline; the next start finishes what this one leaves.
reconcile_end=$((SECONDS + RECONCILE_SECONDS))
# reconcile_cut STEP: succeeds, and logs, once the deadline has passed.
reconcile_cut() {
  if ((SECONDS < reconcile_end)); then
    return 1
  fi
  jq -cn --argjson slot "$slot" --arg step "$1" \
    '{event: "reconcile-incomplete", slot: $slot, step: $step}' >&2
}
stale_runners=()
page=1
while ! reconcile_cut list; do
  if ! api GET "/orgs/$ACTIONS_RUNNER_POOL_ORG/actions/runners?per_page=100&page=$page" "$installation_token"; then
    api_fail "the runner list request"
  fi
  found=$(printf '%s' "$api_body" | jq -er --arg prefix "$prefix" '
    (.runners | length),
    (.runners[] | select(.status == "offline" and .busy == false
      and (.name | startswith($prefix)
        and (.[($prefix | length):] | length == 8 and test("^[0-9a-f]{8}$"))))
      | "\(.id) \(.name)")') ||
    die "$EX_TEMPFAIL" "the runner list response doesn't parse"
  mapfile -t found_lines <<<"$found"
  stale_runners+=("${found_lines[@]:1}")
  ((found_lines[0] == 100)) || break
  page=$((page + 1))
done
for line in "${stale_runners[@]}"; do
  if reconcile_cut delete; then
    break
  fi
  if ! api DELETE "/orgs/$ACTIONS_RUNNER_POOL_ORG/actions/runners/${line%% *}" "$installation_token" &&
    [[ $api_status != 404 ]]; then
    api_fail "the stale runner ${line#* } removal"
  fi
  jq -cn --argjson slot "$slot" --arg runner "${line#* }" --argjson id "${line%% *}" \
    '{event: "stale-runner-deleted", slot: $slot, runner: $runner, runner_id: $id}' >&2
done

# 6. The JIT config. The pending cleanup comes first, so the stop phase
# deletes a registration whose response lacks the config. It replaces the
# file in one rename, so a reader never sees half of it.
if ! api POST "/orgs/$ACTIONS_RUNNER_POOL_ORG/actions/runners/generate-jitconfig" \
  "$installation_token" --data "$body"; then
  api_fail "the JIT config request"
fi
runner_id=$(printf '%s' "$api_body" | jq -er '.runner.id | numbers') ||
  die "$EX_TEMPFAIL" "the JIT config response has no runner ID"
if ! jq -cn --argjson id "$runner_id" --arg container "$container" '{runner_id: $id, container: $container}' \
  >"$pending_file.tmp" || ! mv -f -- "$pending_file.tmp" "$pending_file"; then
  die "$EX_TEMPFAIL" "couldn't record the pending cleanup"
fi
jit_config=$(printf '%s' "$api_body" | jq -er '.encoded_jit_config | strings') ||
  die "$EX_TEMPFAIL" "the JIT config response has no config"
api_body=
jq -cn --argjson slot "$slot" --arg runner "$runner" --argjson id "$runner_id" \
  '{event: "registered", slot: $slot, runner: $runner, runner_id: $id}' >&2

# 7. The token's last use was the JIT request. A revocation GitHub didn't
# confirm runs no job, and the stop phase deletes the registration as
# usual, with a token of its own. The unconfirmed token only ever lived in
# this shell, never reached a container, and expires within the hour.
revoke_token || die "$EX_TEMPFAIL" "GitHub didn't confirm the token's revocation"

# 8. The last stop check. The handlers go first: a signal from here on
# ends bash at once, which systemd counts as a clean stop, or reaches the
# docker client, which passes it to the container.
trap - INT TERM HUP
stop_check registered

# 9. One job, on the slot's network. The work directory is a tmpfs owned
# by the image's runner user (1001), so it starts empty and its size is the
# slot's disk limit. The container's entrypoint reads the JIT config from
# stdin into the variable the runner reads, so the config never reaches
# docker's stored container configuration. No socket or host path is
# mounted.
docker_args=(
  run --rm --interactive --sig-proxy=true
  --name "$container"
  --label "actions-runner-pool.slot=$slot"
  --network "$network"
  --cpus "$ACTIONS_RUNNER_POOL_CPUS"
  --memory "$ACTIONS_RUNNER_POOL_MEMORY"
  --pids-limit "$ACTIONS_RUNNER_POOL_PIDS_LIMIT"
  --security-opt no-new-privileges
  "${dns_args[@]}"
  --tmpfs "/home/runner/_work:rw,exec,uid=1001,gid=1001,mode=0700,size=$ACTIONS_RUNNER_POOL_WORK_SIZE"
  --entrypoint /bin/bash
  "$ACTIONS_RUNNER_POOL_IMAGE"
  -c 'IFS= read -r ACTIONS_RUNNER_INPUT_JITCONFIG && export ACTIONS_RUNNER_INPUT_JITCONFIG && exec /home/runner/run.sh'
)

if [[ $dry_run == true ]]; then
  jit_config=
  # NUL-separated on stdin, because jq would read --rm and the rest as its own options.
  network_json=$(printf '%s\0' "${network_args[@]}" | jq -Rsc 'split("\u0000") | .[:-1]')
  docker_json=$(printf '%s\0' "${docker_args[@]}" | jq -Rsc 'split("\u0000") | .[:-1]')
  jq -cn --argjson network "$network_json" --argjson docker "$docker_json" --arg runner "$runner" \
    --argjson id "$runner_id" --argjson start_bound "$START_BOUND" --argjson stop_bound "$STOP_BOUND" \
    --argjson cleanup "$ACTIONS_RUNNER_POOL_CLEANUP_SECONDS" \
    '{event: "dry-run", runner: $runner, runner_id: $id, network: $network, docker: $docker,
      start_bound_seconds: $start_bound, stop_bound_seconds: $stop_bound, cleanup_seconds: $cleanup}'
  exit 0
fi

# Process substitution feeds stdin from a pipe whose writer is a forked
# copy of this shell, never a here-document's temporary file. docker run's
# own failures are 125, 126 and 127; any other status is the container's.
exec docker "${docker_args[@]}" < <(printf '%s\n' "$jit_config")
