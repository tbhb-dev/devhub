# actions-runner-slot-lib -- what the slot's two phases share: the
# settings, the API call, and the installation token's mint and
# revocation. actions-runner-slot, the start phase, and
# actions-runner-slot-stop, the stop phase, source it; it runs nothing on
# its own. Each phase sets api_seconds, its API calls' --max-time, before
# its first call.
#
# The 1Password token, the App's key, the JWT and the installation token
# live in shell variables and pipes only: never in a file, an argument
# list, an exported variable or a log line.
#
# shellcheck shell=bash
# The sourcing script sets slot before it calls anything here.
# shellcheck disable=SC2154
# Both phases set pipefail too; setting it here tells shellcheck so.
set -o pipefail

# shellcheck disable=SC2034 # the phases use EX_TEMPFAIL
readonly EX_TEMPFAIL=75 EX_CONFIG=78
# Each call's limit, in seconds:
#   STEP_SECONDS, a start-phase API call's --max-time;
#   CALL_SECONDS, a stop-phase API call's --max-time;
#   OP_SECONDS and DOCKER_SECONDS, op read and each docker or systemctl
#   call but the pull, which timeout then ends, with KILL_AFTER more before
#   its SIGKILL;
#   PULL_SECONDS, the image pull, which comes before any credential exists;
#   RECONCILE_SECONDS, after which the start phase starts no further list
#   or stale DELETE call.
readonly STEP_SECONDS=15 CALL_SECONDS=10 OP_SECONDS=20 DOCKER_SECONDS=20 KILL_AFTER=5
# shellcheck disable=SC2034 # the start phase uses both
readonly PULL_SECONDS=600 RECONCILE_SECONDS=30
# The longest the start phase runs past its last stop check before op
# read: op read, the token, the reconcile with the call it may still start
# at its deadline, the JIT config and the revocation.
readonly START_BOUND=$((OP_SECONDS + KILL_AFTER + STEP_SECONDS + RECONCILE_SECONDS + 3 * STEP_SECONDS))
# The least the stop phase needs: the query for a stop job, the
# container's removal, op read, the token, one DELETE and the revocation.
readonly STOP_BOUND=$((2 * DOCKER_SECONDS + OP_SECONDS + 3 * KILL_AFTER + 3 * CALL_SECONDS))

installation_token=
api_seconds=
env_file=${ACTIONS_RUNNER_POOL_ENV:-$HOME/.config/actions-runner/pool.env}

# die STATUS MESSAGE: revokes the installation token if one is held, then
# exits.
die() {
  local status=$1
  shift
  echo "actions-runner-slot: $*" >&2
  revoke_token || :
  exit "$status"
}

# load_env NAMES...: sources the env file and checks that each named
# setting is set, the name prefix and the stop budget. Sourcing runs the
# file, so only the service user may write it.
load_env() {
  local state uid name var
  [[ -f $env_file && ! -L $env_file ]] || die "$EX_CONFIG" "the env file is missing"
  state=$(stat -c %u:%a -- "$env_file")
  uid=$(id -u)
  [[ $state == "$uid:600" ]] || die "$EX_CONFIG" "the env file must be mode 0600 and owned by the service user"
  # shellcheck source=/dev/null
  . "$env_file" || die "$EX_CONFIG" "the env file doesn't parse"
  for name in "$@" NAME_PREFIX CLEANUP_SECONDS; do
    var=ACTIONS_RUNNER_POOL_$name
    [[ -n ${!var:-} ]] || die "$EX_CONFIG" "$var is unset in the env file"
  done
  # No hyphen, so a runner's name yields exactly one prefix, host and slot.
  [[ $ACTIONS_RUNNER_POOL_NAME_PREFIX =~ ^[A-Za-z0-9]+$ ]] ||
    die "$EX_CONFIG" "ACTIONS_RUNNER_POOL_NAME_PREFIX must be letters and digits only"
  # The stop budget: the start phase past its first check and the stop
  # phase each end within it, and the unit's TimeoutStopSec= is it plus a
  # margin.
  local least=$((START_BOUND > STOP_BOUND ? START_BOUND : STOP_BOUND))
  if [[ ! $ACTIONS_RUNNER_POOL_CLEANUP_SECONDS =~ ^[1-9][0-9]{0,3}$ ]] ||
    ((ACTIONS_RUNNER_POOL_CLEANUP_SECONDS < least)); then
    die "$EX_CONFIG" "ACTIONS_RUNNER_POOL_CLEANUP_SECONDS must be a number of seconds from $least"
  fi
}

# check_token_file: fails unless the service-account token file is a
# plain file, mode 0400, owned by the service user.
check_token_file() {
  local token_file=$ACTIONS_RUNNER_POOL_OP_TOKEN_FILE state uid
  [[ -f $token_file && ! -L $token_file ]] || return 1
  state=$(stat -c %u:%a -- "$token_file") || return 1
  uid=$(id -u)
  [[ $state == "$uid:400" ]]
}

# api METHOD PATH CREDENTIAL [curl args...]: sets api_status, api_body and
# the rate-limit headers, and succeeds on a 2xx. api_retry_header holds the
# Retry-After as sent, api_retry_after its seconds or nothing. The
# credential reaches curl on its stdin as a config line, never on its
# command line. A connection failure or a timeout leaves the status 000.
api() {
  local method=$1 path=$2 credential=$3 out
  shift 3
  out=$(printf 'header = "Authorization: Bearer %s"\n' "$credential" |
    # lint-curl: allow the caller reads the status and the error body
    curl --config - --silent --show-error --max-time "$api_seconds" \
      --request "$method" \
      --header 'Accept: application/vnd.github+json' \
      --header 'X-GitHub-Api-Version: 2022-11-28' \
      --write-out '\n%{http_code}|%header{retry-after}|%header{x-ratelimit-remaining}|%header{x-ratelimit-reset}' \
      "$@" "$ACTIONS_RUNNER_POOL_API_URL$path") || true
  api_body=${out%$'\n'*}
  # shellcheck disable=SC2034 # the start phase reads the rate-limit headers
  IFS='|' read -r api_status api_retry_header api_remaining api_reset <<<"${out##*$'\n'}"
  [[ $api_status =~ ^[0-9]{3}$ ]] || api_status=000
  # Seconds, base 10, and no more than a day.
  # shellcheck disable=SC2034 # both phases read api_retry_after
  if [[ $api_retry_header =~ ^[0-9]{1,5}$ ]]; then
    api_retry_after=$((10#$api_retry_header > 86400 ? 86400 : 10#$api_retry_header))
  elif [[ $api_retry_header =~ ^[0-9]+$ ]]; then
    api_retry_after=86400
  else
    api_retry_after=
  fi
  [[ $api_status == 2?? ]]
}

# mint_token: reads the App's key with op, signs an App JWT with openssl
# and exchanges it for an installation token, which it sets. It fails
# with api_status set: op when op read failed or timed out, bad for a
# response without a token, or the HTTP status.
mint_token() {
  local op_token app_key now header claims signature jwt scheme=op
  op_token=$(<"$ACTIONS_RUNNER_POOL_OP_TOKEN_FILE") || {
    api_status=op
    return 1
  }
  # The reference is built here from its parts.
  app_key=$(OP_SERVICE_ACCOUNT_TOKEN=$op_token timeout --kill-after="$KILL_AFTER" "$OP_SECONDS" op read \
    --no-newline "$scheme://$ACTIONS_RUNNER_POOL_OP_KEY_VAULT/$ACTIONS_RUNNER_POOL_OP_KEY_ITEM/$ACTIONS_RUNNER_POOL_OP_KEY_FIELD") || {
    api_status=op
    return 1
  }
  op_token=
  # Valid for nine minutes from a minute ago, to allow for clock drift.
  now=$(date +%s)
  header=$(printf '%s' '{"alg":"RS256","typ":"JWT"}' | b64url)
  claims=$(jq -cjn --arg iss "$ACTIONS_RUNNER_POOL_APP_ID" --argjson now "$now" \
    '{iat: ($now - 60), exp: ($now + 540), iss: $iss}' | b64url)
  signature=$(printf '%s.%s' "$header" "$claims" |
    openssl dgst -sha256 -binary -sign <(printf '%s\n' "$app_key") | b64url)
  app_key=
  jwt=$header.$claims.$signature
  api POST "/app/installations/$ACTIONS_RUNNER_POOL_INSTALLATION_ID/access_tokens" "$jwt" || return 1
  installation_token=$(printf '%s' "$api_body" | jq -er .token) || {
    installation_token=
    api_status=bad
    return 1
  }
  api_body=
}

b64url() {
  openssl base64 -A | tr '+/' '-_' | tr -d '='
}

# revoke_token: revokes the installation token, if one is held, with one
# request and no retry, and forgets it first, so it runs at most once per
# token. It succeeds when no token is held or GitHub confirmed the
# revocation: a 2xx, or a 401 or 404, which mean the token is already
# invalid. Otherwise, a timeout, a 429 or a 5xx, it logs revoke-failed and
# fails; the token then expires within the hour on its own.
revoke_token() {
  [[ -n $installation_token ]] || return 0
  local token=$installation_token
  installation_token=
  if ! api DELETE /installation/token "$token" && [[ ! $api_status =~ ^(401|404)$ ]]; then
    jq -cn --argjson slot "$slot" --arg status "$api_status" \
      '{event: "revoke-failed", slot: $slot, status: $status}' >&2
    return 1
  fi
}
