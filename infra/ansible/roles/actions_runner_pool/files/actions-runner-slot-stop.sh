#!/usr/bin/env bash
# actions-runner-slot-stop -- the cleanup of one runner slot. Three callers
# run it:
#
#   actions-runner-slot-stop SLOT, the stop phase, is the slot unit's
#     ExecStopPost=. systemd runs it once per start, after the main process
#     has ended for any reason: a job that finished, a failed or skipped
#     start, a stop, a timeout or a SIGKILL. The next start waits for it.
#   actions-runner-slot-stop --pending SLOT is the start phase's, before it
#     mints a token, when an earlier start's cleanup is pending.
#   actions-runner-slot-stop --retry SLOT is actions-runner-cleanup@SLOT,
#     which its timer starts while cleanup is pending. It acts only while
#     the slot unit is inactive or failed, so never beside a start or a job.
#
# The pending cleanup is the file pending in the unit's state directory,
# which the start phase writes before it registers a job: the runner ID and
# the container's name, neither of them secret. It outlives a stop and a
# reboot, and only a run that removed the container and resolved the
# registration removes it.
#
# In order, once:
#
#   1. takes the slot's lock without waiting; a run that finds it held
#      leaves the cleanup to the run that holds it;
#   2. as the stop phase, classifies how the start ended (below);
#   3. removes the slot's job container if it still exists;
#   4. if a pending cleanup names a runner and the container is gone: mints
#      a fresh installation token, deletes the registration, retrying
#      within the deadline, then revokes the token;
#   5. removes the pending cleanup once both are resolved, and prints one
#      JSON line: event job for the stop phase, cleanup for the others.
#
# It sets no trap and waits on no background child. Every call has a
# limit, and the whole run ends within ACTIONS_RUNNER_POOL_CLEANUP_SECONDS,
# which the unit's TimeoutStopSec= outlasts. A DELETE that answers 404
# found the runner gone already: an ephemeral runner that ran its job
# removes itself.
#
# A container it couldn't remove may still run a job, so then it leaves
# the registration alone and holds no token.
#
# The stop phase's classification, from what systemd passes and whether a
# stop or restart job for the unit is running:
#
#   clean         the main process exited 0, and no stop was requested;
#   stopped       a stop was requested, and the main process exited 0, 129,
#                 130 or 143, or died of SIGHUP, SIGINT or SIGTERM;
#   config-error  systemd skipped the start at its ExecCondition=;
#   failed        anything else, such as a container that exited 129, 130
#                 or 143 when no stop was requested.
#
# The unit's SuccessExitStatus= counts 129, 130 and 143 as success, so a
# requested stop ends clean. When no stop was requested, the stop phase
# exits 1, and systemd counts the start as failed.
#
# The exit status: 0 once nothing is pending and the start, for the stop
# phase, didn't fail where systemd saw success; 1 when it did; 75 while
# the container or the registration remains, or, except for --retry,
# while another run holds the lock; 78 when the state directory is
# missing or the env file can't name the registration.
#
# Usage: actions-runner-slot-stop [--pending | --retry] SLOT
set -uo pipefail
umask 077

# shellcheck source=SCRIPTDIR/actions-runner-slot-lib.sh
. "${BASH_SOURCE[0]%/*}/../lib/actions-runner/actions-runner-slot-lib.sh"

usage() {
  echo "usage: actions-runner-slot-stop [--pending | --retry] SLOT" >&2
  exit 2
}

mode=stop
case ${1:-} in
--pending)
  mode=pending
  shift
  ;;
--retry)
  mode=retry
  shift
  ;;
esac
[[ $# -eq 1 && $1 =~ ^[1-9][0-9]*$ ]] || usage
slot=$1
status=0
container_state=none
registration=none
runner_id=null
outcome=null
stop_requested=null
unit=actions-runner@$slot.service
container=actions-runner-slot-$slot

[[ -n ${STATE_DIRECTORY:-} && -d $STATE_DIRECTORY ]] ||
  die "$EX_CONFIG" "STATE_DIRECTORY doesn't name the unit's state directory"
pending=$STATE_DIRECTORY/pending

# log EVENT: one JSON line on stderr for the slot.
log() {
  jq -cn --argjson slot "$slot" --arg event "$1" '{event: $event, slot: $slot}' >&2
}

# classify: sets outcome and stop_requested. The stop or restart job that
# systemctl stop or restart queued runs until the stop phase has ended;
# an exit of the unit's own has none.
classify() {
  local jobs job_type=
  if jobs=$(timeout --kill-after="$KILL_AFTER" "$DOCKER_SECONDS" \
    systemctl --user list-jobs --no-legend --plain "$unit" 2>/dev/null); then
    # JOB UNIT TYPE STATE
    read -r _ _ job_type _ <<<"$jobs"
    stop_requested=false
    [[ ! $job_type =~ ^(stop|restart|try-restart)$ ]] || stop_requested=true
  else
    log job-query-failed
  fi
  local result=${SERVICE_RESULT:-} code=${EXIT_CODE:-} exit_status=${EXIT_STATUS:-}
  if [[ $result == exec-condition ]]; then
    outcome=config-error
  elif [[ $result == success && $stop_requested == true ]] &&
    [[ ($code == exited && $exit_status =~ ^(0|129|130|143)$) ||
      ($code == killed && $exit_status =~ ^(HUP|INT|TERM)$) ]]; then
    outcome=stopped
  elif [[ $result == success && $stop_requested != true && $code == exited && $exit_status == 0 ]]; then
    outcome=clean
  else
    outcome=failed
  fi
}

# slot_idle: succeeds while the slot unit is inactive or failed, so no
# start phase, job or stop phase of it runs. A start that begins after
# this check finds the lock held at its step 3 and exits 75.
slot_idle() {
  local state
  state=$(timeout --kill-after="$KILL_AFTER" "$DOCKER_SECONDS" \
    systemctl --user show --property=ActiveState --value "$unit" 2>/dev/null) || return 1
  [[ $state == inactive || $state == failed ]]
}

# cleanup: steps 3 to 5.
cleanup() {
  local out id deadline attempt delay room
  # 3. The container, before anything that needs the env file. docker rm
  # --force kills it first if it runs.
  container_state=removed
  if ! out=$(timeout --kill-after="$KILL_AFTER" "$DOCKER_SECONDS" docker rm --force -- "$container" 2>&1) &&
    [[ $out != *"No such container"* ]]; then
    container_state=failed
    status=$EX_TEMPFAIL
    jq -cn --argjson slot "$slot" --arg container "$container" \
      '{event: "container-remove-failed", slot: $slot, container: $container}' >&2
  fi

  # 4. The registration, if a start got that far.
  if [[ -f $pending ]]; then
    if id=$(jq -er --arg container "$container" 'select(.container == $container) | .runner_id | numbers' \
      "$pending" 2>/dev/null); then
      runner_id=$id
    else
      log pending-unreadable
    fi
  fi

  if [[ $runner_id != null && $container_state == failed ]]; then
    registration=skipped
    jq -cn --argjson slot "$slot" --argjson id "$runner_id" \
      '{event: "delete-skipped", slot: $slot, runner_id: $id}' >&2
  elif [[ $runner_id != null ]]; then
    load_env ORG APP_ID INSTALLATION_ID OP_TOKEN_FILE OP_KEY_VAULT OP_KEY_ITEM OP_KEY_FIELD API_URL
    deadline=$ACTIONS_RUNNER_POOL_CLEANUP_SECONDS
    api_seconds=$CALL_SECONDS
    attempt=0
    if mint_token; then
      # A retry waits 1, 2 or 4 s, or the longer Retry-After, and starts
      # only if it and the revocation can end by the deadline, in $SECONDS,
      # so the deadline caps the wait. At most four attempts in all.
      while :; do
        attempt=$((attempt + 1))
        if api DELETE "/orgs/$ACTIONS_RUNNER_POOL_ORG/actions/runners/$runner_id" "$installation_token"; then
          registration=deleted
          break
        fi
        if [[ $api_status == 404 ]]; then
          registration=gone
          break
        fi
        if [[ ! $api_status =~ ^(000|429|5..)$ ]] || ((attempt == 4)); then
          break
        fi
        delay=$((1 << (attempt - 1)))
        if [[ -n $api_retry_after ]] && ((api_retry_after > delay)); then
          delay=$api_retry_after
        fi
        room=$((deadline - SECONDS - 2 * CALL_SECONDS))
        ((room >= 0)) || break
        ((delay <= room)) || delay=$room
        sleep "$delay"
      done
    fi
    if [[ $registration == none ]]; then
      registration=failed
      status=$EX_TEMPFAIL
      jq -cn --argjson slot "$slot" --argjson id "$runner_id" --arg status "$api_status" \
        --argjson attempts "$attempt" \
        '{event: "delete-failed", slot: $slot, runner_id: $id, status: $status, attempts: $attempts}' >&2
    fi
    # The token's last use was the DELETE.
    revoke_token
  fi

  # 5. The pending cleanup, once nothing of it remains.
  if [[ $container_state == removed && $registration =~ ^(none|deleted|gone)$ ]]; then
    rm -f -- "$pending"
  fi
}

# 1. The lock. Every cleanup of this slot takes it, and the start phase's
# step 3 takes it through --pending, so no two of them overlap.
exec 9>>"$STATE_DIRECTORY/lock"
if ! flock -n 9; then
  log cleanup-busy
  [[ $mode == retry ]] || status=$EX_TEMPFAIL
  [[ $mode != stop ]] || classify
elif [[ $mode == stop ]]; then
  # 2. How the start ended.
  classify
  cleanup
elif [[ ! -e $pending ]]; then
  :
elif [[ $mode == retry ]] && ! slot_idle; then
  # The slot's own start or stop phase cleans up.
  log cleanup-deferred
else
  cleanup
fi

if [[ $mode == stop && $outcome == failed && ${SERVICE_RESULT:-} == success ]] && ((status == 0)); then
  status=1
fi
pending_state=false
[[ ! -e $pending ]] || pending_state=true
if [[ $mode == stop ]]; then
  jq -cn --argjson slot "$slot" --argjson id "$runner_id" --arg container "$container_state" \
    --arg registration "$registration" --argjson pending "$pending_state" --arg outcome "$outcome" \
    --argjson stop_requested "$stop_requested" --arg result "${SERVICE_RESULT:-}" --arg code "${EXIT_CODE:-}" \
    --arg exit_status "${EXIT_STATUS:-}" --arg invocation "${INVOCATION_ID:-}" --argjson status "$status" \
    '{event: "job", slot: $slot, runner_id: $id, container: $container, registration: $registration,
      pending: $pending, outcome: $outcome, stop_requested: $stop_requested, service_result: $result,
      exit_code: $code, exit_status: $exit_status, invocation: $invocation, status: $status}'
else
  jq -cn --arg mode "$mode" --argjson slot "$slot" --argjson id "$runner_id" --arg container "$container_state" \
    --arg registration "$registration" --argjson pending "$pending_state" --argjson status "$status" \
    '{event: "cleanup", mode: $mode, slot: $slot, runner_id: $id, container: $container,
      registration: $registration, pending: $pending, status: $status}'
fi
exit "$status"
