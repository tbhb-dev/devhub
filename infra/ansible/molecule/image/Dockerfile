FROM ubuntu:26.04@sha256:c098726dd9b9733f3e885f8de0ef375206a311b0a317eadc40d347b1115fb944 AS base

ENV container=docker
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-apt systemd systemd-sysv dbus sudo ca-certificates \
        apparmor nftables iproute2 aptitude rsync acl dbus-user-session e2fsprogs \
        iptables passt uidmap curl jq openssl \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/sbin/policy-rc.d
RUN systemctl mask systemd-modules-load.service getty@tty1.service
CMD ["/sbin/init"]

FROM base AS ops_accounts

FROM base AS docker_rootless
RUN mkdir -p /etc/apt/keyrings \
    && curl --fail --silent --show-error https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && echo '1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570  /etc/apt/keyrings/docker.asc' | sha256sum --check \
    && printf 'deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu resolute stable\n' > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker-ce docker-ce-cli docker-ce-rootless-extras containerd.io \
    && rm /etc/apt/keyrings/docker.asc /etc/apt/sources.list.d/docker.list \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /bin/bash ops \
    && printf 'ops ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/ops \
    && chmod 0440 /etc/sudoers.d/ops

FROM docker_rootless AS actions_runner_pool
RUN curl --fail --silent --show-error https://downloads.1password.com/linux/keys/1password.asc -o /etc/apt/keyrings/1password.asc \
    && echo 'f39e7dd9dedc581ced85732832f217e0de5860a3b80279b5af4bc7c6d8157bae  /etc/apt/keyrings/1password.asc' | sha256sum --check \
    && printf 'deb [arch=%s signed-by=/etc/apt/keyrings/1password.asc] https://downloads.1password.com/linux/debian/%s stable main\n' "$(dpkg --print-architecture)" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/1password.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends 1password-cli \
    && rm /etc/apt/keyrings/1password.asc /etc/apt/sources.list.d/1password.list \
    && rm -rf /var/lib/apt/lists/*

FROM docker_rootless AS ops_ssh
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/sbin/policy-rc.d \
    && useradd --create-home --shell /bin/bash ops-ro \
    && printf 'PasswordAuthentication yes\n' > /etc/ssh/sshd_config.d/50-cloud-init.conf \
    && systemctl disable ssh.service \
    && systemctl enable nftables.service ssh.socket dbus.socket
