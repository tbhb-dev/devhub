import base64
import os
import tempfile
from pathlib import Path
from typing import cast

import pytest
import yaml

os.environ.setdefault("ANSIBLE_LOCAL_TEMP", tempfile.gettempdir())

from ansible.template import Templar, trust_as_template

PLAYBOOK = Path(__file__).resolve().parents[1] / "infra/ansible/playbooks/verify.yml"

SAMPLE_NO_TAILSCALE_ROOT = (
    "Global\n"
    "         Protocols: -mDNS -DNSOverTLS\n"
    "Current DNS Server: 1.1.1.1\n"
    "       DNS Servers: 1.1.1.1 1.0.0.1\n"
    "Fallback DNS Servers: 9.9.9.9\n"
    "        DNS Domain: ~.\n"
    "\n"
    "Link 2 (eth0)\n"
    "    Current Scopes: DNS\n"
    "         Protocols: +DefaultRoute -mDNS +DNSOverTLS\n"
    "Current DNS Server: 192.0.2.1\n"
    "       DNS Servers: 192.0.2.1\n"
    "                    2001:db8::1\n"
    "        DNS Domain: example.com ~.\n"
    "\n"
    "Link 3 (tailscale0)\n"
    "    Current Scopes: DNS\n"
    "         Protocols: -DefaultRoute -mDNS -DNSOverTLS\n"
    "Current DNS Server: 198.51.100.53\n"
    "       DNS Servers: 198.51.100.53\n"
    "        DNS Domain: example.ts.net\n"
)

SAMPLE_TAILSCALE_CLAIMS_ROOT = (
    "Global\n"
    "         Protocols: -mDNS -DNSOverTLS\n"
    "       DNS Servers: 192.0.2.53\n"
    "\n"
    "Link 2 (eth0)\n"
    "    Current Scopes: DNS\n"
    "         Protocols: +DefaultRoute -mDNS DNSOverTLS=opportunistic\n"
    "       DNS Servers: 192.0.2.1 192.0.2.2\n"
    "        DNS Domain: ~.\n"
    "\n"
    "Link 3 (tailscale0)\n"
    "    Current Scopes: DNS\n"
    "         Protocols: +DefaultRoute -mDNS -DNSOverTLS\n"
    "       DNS Servers: 198.51.100.53\n"
    "        DNS Domain: example.ts.net ~.\n"
    "\n"
    "Link 4 (enp2s0)\n"
    "    Current Scopes: DNS\n"
    "         Protocols: +DefaultRoute -mDNS +DNSOverTLS\n"
    "       DNS Servers: 1.1.1.1\n"
    "           Domains: internal.example.com\n"
)


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def trust(value: object) -> object:
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, list):
        return [trust(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        return {
            key: trust(item) for key, item in cast("dict[str, object]", value).items()
        }
    return value


def base_variables() -> dict[str, object]:
    return {
        "ansible_facts": {"distribution": "Ubuntu", "distribution_version": "26.04"},
        "verify_os_distribution": "Ubuntu",
        "verify_os_version": "26.04",
        "verify_mdstat": {
            "content": b64(
                "md0 : active raid1 sda1[0] sdb1[1]\n      100 blocks [2/2] [UU]\n"
            )
        },
        "verify_ntp": {"stdout": "yes"},
        "verify_failed_units": {"rc": 0, "stdout_lines": []},
        "verify_dpkg_audit": {"rc": 0, "stdout_lines": []},
        "verify_reboot_required": {"stat": {"exists": False}},
        "verify_apt_upgrade": {"rc": 0, "stdout": ""},
        "verify_ssh_drop_in": {
            "content": b64(
                "PermitRootLogin no\n"
                "PasswordAuthentication no\n"
                "KbdInteractiveAuthentication no\n"
                "AllowUsers ops ops-ro\n"
            )
        },
        "verify_ssh_main": {
            "content": b64(
                "Include /etc/ssh/sshd_config.d/*.conf\n"
                "PermitRootLogin prohibit-password\n"
            )
        },
        "verify_ssh_drop_ins": {
            "files": [{"path": "/etc/ssh/sshd_config.d/00-techops-ssh.conf"}]
        },
        "verify_ssh_conflicts": [],
        "verify_ssh_unreadable": [],
        "verify_ssh_firewall_active": {"rc": 0, "stdout": "active"},
        "verify_ssh_firewall_enabled": {"rc": 0, "stdout": "enabled"},
        "verify_nftables_enabled": {"stdout": "disabled"},
        "verify_nftables_active": {"stdout": "inactive"},
        "verify_nftables_override": {"content": b64("[Service]\nExecStop=\n")},
        "verify_ssh_revert": {"rc": 0, "stdout_lines": []},
        "verify_resolvectl": {"rc": 0, "stdout": SAMPLE_NO_TAILSCALE_ROOT},
        "verify_networkctl_list": {
            "rc": 0,
            "stdout": (
                "  1 lo         loopback carrier    unmanaged\n"
                "  2 eth0       ether    routable   configured\n"
                "  3 tailscale0 none     routable   unmanaged\n"
            ),
        },
        "verify_networkctl_uplink": {
            "rc": 0,
            "stdout_lines": [
                "2: eth0",
                "Network File: /run/systemd/network/10-netplan-eth0.network",
                "Address: 192.0.2.10",
            ],
        },
        "verify_networkd_files": {"files": []},
        "verify_networkd_contents": {"results": []},
        "verify_netplan_files": {
            "files": [
                {
                    "path": "/etc/netplan/50-cloud-init.yaml",
                    "mode": "0600",
                    "roth": False,
                }
            ]
        },
        "verify_dot_probes": {
            "results": [
                {"item": "1.1.1.1", "state": "started", "failed": False},
                {"item": "1.0.0.1", "state": "started", "failed": False},
                {"item": "9.9.9.9", "state": "started", "failed": False},
            ]
        },
        "verify_nftables_files": {"files": []},
        "verify_nftables_contents": {"results": []},
        "verify_egress_firewall_active": {"rc": 0, "stdout": "active"},
        "verify_stub_listener": {
            "rc": 0,
            "stdout": "udp LISTEN 0 0 127.0.0.53%lo:53 0.0.0.0:*\n",
        },
    }


def evaluate(overrides: dict[str, object]) -> tuple[dict[str, object], list[str]]:
    play = cast(
        "dict[str, object]",
        trust(yaml.safe_load(PLAYBOOK.read_text())[0]),
    )
    tasks = cast("list[dict[str, object]]", play["tasks"])
    eval_task = next(
        task for task in tasks if task.get("name") == "Evaluate the checks"
    )
    variables = base_variables() | overrides
    templar = Templar(variables=variables)
    task_vars = cast("dict[str, object]", eval_task.get("vars", {}))
    for key, expr in task_vars.items():
        variables[key] = templar.template(expr)
        templar.available_variables = variables
    facts = cast(
        "dict[str, object]",
        templar.template(eval_task["ansible.builtin.set_fact"]),
    )
    return (
        cast("dict[str, object]", facts["verify_report"]),
        cast("list[str]", facts["verify_regressions"]),
    )


@pytest.mark.parametrize(
    (
        "stdout",
        "expected_global_dns",
        "expected_global_domains",
        "expected_links",
        "expected_tailscale_root",
    ),
    [
        (
            SAMPLE_NO_TAILSCALE_ROOT,
            ["1.1.1.1", "1.0.0.1"],
            ["~."],
            {
                "eth0": {
                    "dns_servers": ["192.0.2.1", "2001:db8::1"],
                    "domains": ["example.com", "~."],
                    "dns_over_tls": "yes",
                },
                "tailscale0": {
                    "dns_servers": ["198.51.100.53"],
                    "domains": ["example.ts.net"],
                    "dns_over_tls": "no",
                },
            },
            False,
        ),
        (
            SAMPLE_TAILSCALE_CLAIMS_ROOT,
            ["192.0.2.53"],
            [],
            {
                "eth0": {
                    "dns_servers": ["192.0.2.1", "192.0.2.2"],
                    "domains": ["~."],
                    "dns_over_tls": "opportunistic",
                },
                "tailscale0": {
                    "dns_servers": ["198.51.100.53"],
                    "domains": ["example.ts.net", "~."],
                    "dns_over_tls": "no",
                },
                "enp2s0": {
                    "dns_servers": ["1.1.1.1"],
                    "domains": ["internal.example.com"],
                    "dns_over_tls": "yes",
                },
            },
            True,
        ),
        (
            "",
            [],
            [],
            {},
            False,
        ),
    ],
)
def test_ac1_resolvectl_status(
    stdout: str,
    expected_global_dns: list[str],
    expected_global_domains: list[str],
    expected_links: dict[str, object],
    expected_tailscale_root: bool,
) -> None:
    report, _ = evaluate({"verify_resolvectl": {"rc": 0, "stdout": stdout}})
    assert report["resolver_global_dns_servers"] == expected_global_dns
    assert report["resolver_global_domains"] == expected_global_domains
    assert report["resolver_links"] == expected_links
    assert report["tailscale0_claims_default_route"] is expected_tailscale_root


@pytest.mark.parametrize(
    ("overrides", "expected_networkd", "expected_uplink", "expected_netplan"),
    [
        (
            {
                "verify_networkd_contents": {
                    "results": [
                        {
                            "source": "/run/systemd/network/10-netplan-eth0.network",
                            "content": b64(
                                "[Match]\nName=eth0\n\n[Network]\nDHCP=ipv4\n"
                            ),
                        },
                        {
                            "source": (
                                "/etc/systemd/network/"
                                "10-netplan-eth0.network.d/override.conf"
                            ),
                            "content": b64("[DHCPv4]\nUseDNS=false\n"),
                        },
                    ]
                },
                "verify_netplan_files": {
                    "files": [
                        {
                            "path": "/etc/netplan/50-cloud-init.yaml",
                            "mode": "0600",
                            "roth": False,
                        }
                    ]
                },
            },
            {
                "/etc/systemd/network/10-netplan-eth0.network.d/override.conf": (
                    "[DHCPv4]\nUseDNS=false\n"
                ),
                "/run/systemd/network/10-netplan-eth0.network": (
                    "[Match]\nName=eth0\n\n[Network]\nDHCP=ipv4\n"
                ),
            },
            "eth0",
            {"/etc/netplan/50-cloud-init.yaml": "not readable by ops-ro"},
        ),
        (
            {
                "verify_networkd_contents": {"results": []},
                "verify_netplan_files": {"files": []},
                "verify_networkctl_list": {"rc": 0, "stdout": ""},
            },
            {},
            "eth0",
            {"/etc/netplan": "not readable by ops-ro"},
        ),
        (
            {
                "verify_networkd_contents": {
                    "results": [
                        {
                            "failed": True,
                            "msg": (
                                "file not found: "
                                "/run/systemd/network/10-netplan-eth0.network"
                            ),
                            "item": "/run/systemd/network/10-netplan-eth0.network",
                        },
                        {
                            "source": (
                                "/etc/systemd/network/"
                                "10-netplan-eth0.network.d/override.conf"
                            ),
                            "content": b64("[DHCPv4]\nUseDNS=false\n"),
                        },
                    ]
                },
            },
            {
                "/etc/systemd/network/10-netplan-eth0.network.d/override.conf": (
                    "[DHCPv4]\nUseDNS=false\n"
                ),
                "/run/systemd/network/10-netplan-eth0.network": "unreadable",
            },
            "eth0",
            {"/etc/netplan/50-cloud-init.yaml": "not readable by ops-ro"},
        ),
    ],
)
def test_ac2_networkd_and_netplan(
    overrides: dict[str, object],
    expected_networkd: dict[str, str],
    expected_uplink: str,
    expected_netplan: dict[str, str],
) -> None:
    report, _ = evaluate(overrides)
    assert report["networkd_files"] == expected_networkd
    assert report["uplink_link"] == expected_uplink
    assert report["networkctl_uplink_status"] == [
        "2: eth0",
        "Network File: /run/systemd/network/10-netplan-eth0.network",
        "Address: 192.0.2.10",
    ]
    assert report["netplan_files"] == expected_netplan


@pytest.mark.parametrize(
    ("probe_results", "expected"),
    [
        (
            [
                {"item": "1.1.1.1", "state": "started"},
                {"item": "1.0.0.1", "state": "started"},
                {"item": "9.9.9.9", "state": "started"},
            ],
            {"1.1.1.1": True, "1.0.0.1": True, "9.9.9.9": True},
        ),
        (
            [
                {"item": "1.1.1.1", "state": "started"},
                {"item": "1.0.0.1", "msg": "Timeout when waiting for 1.0.0.1:853"},
                {"item": "9.9.9.9", "state": "started"},
            ],
            {"1.1.1.1": True, "1.0.0.1": False, "9.9.9.9": True},
        ),
        (
            [
                {"item": "1.1.1.1", "msg": "Timeout when waiting for 1.1.1.1:853"},
                {"item": "1.0.0.1", "msg": "Timeout when waiting for 1.0.0.1:853"},
                {"item": "9.9.9.9", "msg": "Timeout when waiting for 9.9.9.9:853"},
            ],
            {"1.1.1.1": False, "1.0.0.1": False, "9.9.9.9": False},
        ),
    ],
)
def test_ac3_port_853_probes(
    probe_results: list[dict[str, object]], expected: dict[str, bool]
) -> None:
    report, _ = evaluate({"verify_dot_probes": {"results": probe_results}})
    assert report["dot_port_853"] == expected


@pytest.mark.parametrize(
    ("nft_results", "egress_state", "ss_stdout", "expected_nft", "expected_stub"),
    [
        (
            [
                {
                    "source": "/etc/nftables.d/actions-runner-egress.nft",
                    "content": b64("table inet actions_runner_egress {}\n"),
                },
                {
                    "source": "/etc/nftables.d/techops-ssh.nft",
                    "content": b64("table inet techops_ssh {}\n"),
                },
            ],
            "active",
            (
                "udp LISTEN 0 0 127.0.0.53%lo:53 0.0.0.0:*\n"
                "tcp LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*\n"
            ),
            {
                "/etc/nftables.d/actions-runner-egress.nft": (
                    "table inet actions_runner_egress {}\n"
                ),
                "/etc/nftables.d/techops-ssh.nft": "table inet techops_ssh {}\n",
            },
            True,
        ),
        (
            [],
            "inactive",
            "tcp LISTEN 0 4096 127.0.0.53:53 0.0.0.0:*\n",
            {},
            True,
        ),
        (
            [],
            "inactive",
            "tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
            {},
            False,
        ),
        (
            [
                {
                    "failed": True,
                    "msg": (
                        "Permission denied: /etc/nftables.d/actions-runner-egress.nft"
                    ),
                    "item": "/etc/nftables.d/actions-runner-egress.nft",
                },
                {
                    "source": "/etc/nftables.d/techops-ssh.nft",
                    "content": b64("table inet techops_ssh {}\n"),
                },
            ],
            "active",
            "udp LISTEN 0 0 127.0.0.53%lo:53 0.0.0.0:*\n",
            {
                "/etc/nftables.d/actions-runner-egress.nft": "unreadable",
                "/etc/nftables.d/techops-ssh.nft": "table inet techops_ssh {}\n",
            },
            True,
        ),
    ],
)
def test_ac4_egress_and_stub_listener(
    nft_results: list[dict[str, object]],
    egress_state: str,
    ss_stdout: str,
    expected_nft: dict[str, str],
    expected_stub: bool,
) -> None:
    report, _ = evaluate(
        {
            "verify_nftables_contents": {"results": nft_results},
            "verify_egress_firewall_active": {"rc": 0, "stdout": egress_state},
            "verify_stub_listener": {"rc": 0, "stdout": ss_stdout},
        }
    )
    assert report["egress_nftables_files"] == expected_nft
    assert report["egress_firewall_active"] == egress_state
    assert report["ssh_firewall_active"] == "active"
    assert report["resolver_stub_listening"] is expected_stub


def test_ac5_report_only_and_read_only_playbook() -> None:
    report, regressions = evaluate(
        {
            "verify_resolvectl": {"rc": 2},
            "verify_networkctl_list": {"rc": 2},
            "verify_networkctl_uplink": {"skipped": True},
            "verify_networkd_contents": {"results": []},
            "verify_netplan_files": {"files": []},
            "verify_dot_probes": {
                "results": [
                    {"item": "1.1.1.1", "msg": "Timeout"},
                    {"item": "1.0.0.1", "msg": "Timeout"},
                    {"item": "9.9.9.9", "msg": "Timeout"},
                ]
            },
            "verify_nftables_contents": {"results": []},
            "verify_egress_firewall_active": {"rc": 3, "stdout": "inactive"},
            "verify_stub_listener": {"rc": 0, "stdout": ""},
        }
    )
    assert regressions == []
    assert report["resolver_stub_listening"] is False
    assert report["dot_port_853"] == {
        "1.1.1.1": False,
        "1.0.0.1": False,
        "9.9.9.9": False,
    }

    play = cast("dict[str, object]", yaml.safe_load(PLAYBOOK.read_text())[0])
    assert play.get("gather_subset") == ["min"]
    assert not play.get("become")

    allowed_modules = {
        "ansible.builtin.slurp",
        "ansible.builtin.command",
        "ansible.builtin.stat",
        "ansible.builtin.find",
        "ansible.builtin.wait_for",
        "ansible.builtin.set_fact",
        "ansible.builtin.debug",
        "ansible.builtin.assert",
    }
    tasks = cast("list[dict[str, object]]", play["tasks"])
    for task in tasks:
        assert not task.get("become")
        modules = set(task) & allowed_modules
        assert len(modules) == 1
        if "ansible.builtin.slurp" in task:
            slurp = cast("dict[str, str]", task["ansible.builtin.slurp"])
            assert "netplan" not in slurp["src"]
