#!/bin/sh
set -eu

# The daemon's RuntimeDirectory may be absent before socket activation.
install -d -m 0755 /run/sshd

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
mkdir "$work/drop_ins"
cp -a /etc/ssh/sshd_config.d/. "$work/drop_ins/"
rm -f "$work/drop_ins/00-techops-ssh.conf"
cp "$1" "$work/drop_ins/00-techops-ssh.conf"

# Only the validator sees this bind mount. The real main file and all
# Includes keep their paths, but no daemon can see the candidate until
# Ansible atomically installs it after this whole-config check succeeds.
unshare --mount --propagation private sh -eu -s -- "$work/drop_ins" "$work" <<'VALIDATE'
mount --bind "$1" /etc/ssh/sshd_config.d
# Only the standard main-file Include is allowed; nested Includes can
# hide directives from the read-only health checks.
if ! awk '
  { line = tolower($0) }
  line ~ /^[ \t]*include([ \t=]|$)/ {
    if (line !~ /^[ \t]*include[ \t]+\/etc\/ssh\/sshd_config\.d\/\*\.conf[ \t]*(#.*)?$/) exit 1
    included = 1
  }
  !included && line ~ /^[ \t]*(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|allowusers)([ \t=]|$)/ {
    exit 1
  }
' /etc/ssh/sshd_config; then
  echo 'SSH main configuration has a foreign Include or an earlier hardening directive' >&2
  exit 1
fi
for config in /etc/ssh/sshd_config.d/*.conf; do
  if grep -qiE '^[[:space:]]*Include([[:space:]=]|$)' "$config"; then
    echo "SSH drop-in contains Include: $config" >&2
    exit 1
  fi
done
/usr/sbin/sshd -t -f /etc/ssh/sshd_config
/usr/sbin/sshd -T -f /etc/ssh/sshd_config > "$2/effective"
for setting in 'permitrootlogin no' 'passwordauthentication no' 'kbdinteractiveauthentication no'; do
  if ! grep -qxF "$setting" "$2/effective"; then
    echo "SSH configuration shadows the candidate: expected $setting" >&2
    exit 1
  fi
done
expected_users=$(awk 'tolower($1) == "allowusers" { for (i = 2; i <= NF; i++) print "allowusers " $i }' \
  /etc/ssh/sshd_config.d/00-techops-ssh.conf)
effective_users=$(awk '$1 == "allowusers"' "$2/effective")
if [ -z "$expected_users" ] || [ "$effective_users" != "$expected_users" ]; then
  echo 'Effective AllowUsers differs from the candidate' >&2
  exit 1
fi
# sshd -T without connection criteria does not evaluate Match blocks.
# Reject them even when they currently repeat the global settings.
for config in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; do
  [ "$config" != /etc/ssh/sshd_config.d/00-techops-ssh.conf ] || continue
  if grep -qiE '^[[:space:]]*Match([[:space:]=]|$)' "$config"; then
    echo "SSH configuration contains Match: $config" >&2
    exit 1
  fi
done
VALIDATE
