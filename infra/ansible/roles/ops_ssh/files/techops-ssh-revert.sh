#!/bin/sh
set -eu

reload_ssh() {
  if systemctl is-active --quiet ssh.service; then
    systemctl reload ssh.service
  fi
}

cancel_revert() {
  # Either transient unit may be absent on a first or completed run.
  for unit in techops-ssh-revert.timer techops-ssh-revert.service; do
    unit_state=$(systemctl show --property=LoadState --value "$unit")
    if [ "$unit_state" != not-found ]; then
      systemctl stop "$unit"
      # --collect may unload the unit as soon as it stops.
      unit_state=$(systemctl show --property=LoadState --value "$unit")
      if [ "$unit_state" = loaded ]; then
        systemctl reset-failed "$unit"
      fi
    fi
  done
}

case ${1:-revert} in
arm)
  for unit in techops-ssh-revert.timer techops-ssh-revert.service; do
    unit_state=$(systemctl show --property=LoadState --value "$unit")
    if [ "$unit_state" != not-found ]; then
      echo 'Interrupted SSH hardening run detected; reverting before rearming.' >&2
      "$0" revert
      break
    fi
  done
  cancel_revert
  systemd-run --unit=techops-ssh-revert --on-active=10min --collect \
    /usr/local/sbin/techops-ssh-revert
  ;;
cancel)
  cancel_revert
  ;;
reload)
  reload_ssh
  ;;
revert)
  # A failed recovery step must not prevent the remaining recovery steps.
  status=0
  rm -f /etc/ssh/sshd_config.d/00-techops-ssh.conf || status=1
  unit_state=$(systemctl show --property=LoadState --value techops-ssh.service) || status=1
  if [ "$unit_state" != not-found ]; then
    systemctl disable --now techops-ssh.service || status=1
  fi
  if nft list table inet techops_ssh >/dev/null 2>&1; then
    nft delete table inet techops_ssh || status=1
  fi
  # Socket activation may not have created the daemon's runtime directory.
  install -d -m 0755 /run/sshd || status=1
  if /usr/sbin/sshd -t; then
    reload_ssh || status=1
  else
    status=1
  fi
  unit_state=$(systemctl show --property=LoadState --value techops-ssh-revert.timer) || status=1
  if [ "$unit_state" != not-found ]; then
    systemctl stop techops-ssh-revert.timer || status=1
  fi
  exit "$status"
  ;;
*)
  echo 'usage: techops-ssh-revert [arm|cancel|reload|revert]' >&2
  exit 2
  ;;
esac
