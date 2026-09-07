#!/usr/bin/env python3

"""Generate persistent, device-local company network overlays."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / ".local" / "company.local.json"
DEFAULT_OUTPUT = ROOT / ".local" / "generated"
LOCAL_ROOT = ROOT / ".local"
EXAMPLE_CONFIG = ROOT / "config" / "company.local.example.json"
ALLOWED_TUNNELS = {"split", "full", "unknown"}
PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16")
)


class ConfigError(ValueError):
    pass


def _require_object(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    return value


def _check_keys(value: Dict[str, Any], allowed: Iterable[str], name: str) -> None:
    unexpected = sorted(set(value) - set(allowed))
    if unexpected:
        raise ConfigError(f"{name} contains unknown fields: {', '.join(unexpected)}")


def _require_list(value: Any, name: str) -> List[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be an array")
    return value


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(values))


def normalize_domain(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("company.domains entries must be strings")
    domain = value.strip().lower().rstrip(".")
    for prefix in ("*.", "+."):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    if not domain or any(character in domain for character in "/,: "):
        raise ConfigError(f"invalid company domain: {value!r}")
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ConfigError(f"invalid company domain: {value!r}") from exc
    try:
        ipaddress.ip_address(ascii_domain)
    except ValueError:
        pass
    else:
        raise ConfigError(f"company domain must not be an IP address: {value!r}")
    if len(ascii_domain) > 253:
        raise ConfigError(f"company domain is too long: {value!r}")
    label_pattern = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
    if any(not label_pattern.fullmatch(label) for label in ascii_domain.split(".")):
        raise ConfigError(f"invalid company domain: {value!r}")
    return ascii_domain


def normalize_cidr(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("company.cidrs entries must be strings")
    try:
        network = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise ConfigError(f"invalid company CIDR: {value!r}") from exc
    if network.version != 4:
        raise ConfigError(
            "IPv6 company CIDRs are unsupported while the published profiles disable IPv6"
        )
    containers = [
        container
        for container in PRIVATE_IPV4_NETWORKS
        if network.subnet_of(container)
    ]
    if not containers:
        raise ConfigError(
            f"company CIDR must be inside a private or shared IPv4 range: {value!r}"
        )
    if network in containers:
        raise ConfigError(
            f"company CIDR is too broad; configure the actual company subnet: {value!r}"
        )
    return str(network)


def normalize_dns_server(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("dns.servers entries must be strings")
    server = value.strip().lower()
    if server == "system":
        return server
    try:
        address = ipaddress.ip_address(server)
    except ValueError as exc:
        raise ConfigError(f"DNS server must be 'system' or an IP address: {value!r}") from exc
    if address.version != 4:
        raise ConfigError(
            "IPv6 company DNS is unsupported while the published profiles disable IPv6"
        )
    if not any(address in network for network in PRIVATE_IPV4_NETWORKS):
        raise ConfigError(
            f"company DNS must be 'system' or a private IPv4 address: {value!r}"
        )
    return str(address)


def normalize_ssid(value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError("wifi.ssids entries must be strings")
    ssid = value.strip()
    if (
        not ssid
        or "," in ssid
        or "\n" in ssid
        or "\r" in ssid
        or len(ssid.encode("utf-8")) > 32
    ):
        raise ConfigError(f"invalid Wi-Fi SSID: {value!r}")
    return ssid


def normalize_config(raw: Any) -> Dict[str, Any]:
    config = _require_object(raw, "configuration")
    _check_keys(config, {"version", "company", "dns", "wifi", "vpn"}, "configuration")
    if config.get("version") != 1:
        raise ConfigError("version must be 1")

    company = _require_object(config.get("company"), "company")
    _check_keys(company, {"domains", "cidrs"}, "company")
    domains = _unique(
        normalize_domain(value)
        for value in _require_list(company.get("domains", []), "company.domains")
    )
    cidrs = _unique(
        normalize_cidr(value)
        for value in _require_list(company.get("cidrs", []), "company.cidrs")
    )
    if not domains and not cidrs:
        raise ConfigError("configure at least one company domain or CIDR")

    dns = _require_object(config.get("dns", {}), "dns")
    _check_keys(dns, {"servers"}, "dns")
    dns_servers = _unique(
        normalize_dns_server(value)
        for value in _require_list(dns.get("servers", ["system"]), "dns.servers")
    )
    if not dns_servers:
        dns_servers = ["system"]
    if "system" in dns_servers and len(dns_servers) != 1:
        raise ConfigError("dns.servers must use either 'system' or fixed private IPs, not both")

    wifi = _require_object(config.get("wifi", {}), "wifi")
    _check_keys(wifi, {"ssids"}, "wifi")
    ssids = _unique(
        normalize_ssid(value)
        for value in _require_list(wifi.get("ssids", []), "wifi.ssids")
    )

    vpn = _require_object(config.get("vpn", {}), "vpn")
    _check_keys(vpn, {"client", "tunnel"}, "vpn")
    client = vpn.get("client", "")
    if not isinstance(client, str) or "\n" in client or "\r" in client:
        raise ConfigError("vpn.client must be a single-line string")
    tunnel = vpn.get("tunnel", "unknown")
    if tunnel not in ALLOWED_TUNNELS:
        raise ConfigError("vpn.tunnel must be split, full, or unknown")

    return {
        "version": 1,
        "company": {"domains": domains, "cidrs": cidrs},
        "dns": {"servers": dns_servers},
        "wifi": {"ssids": ssids},
        "vpn": {"client": client.strip(), "tunnel": tunnel},
    }


def load_config(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    return normalize_config(raw)


def _direct_cidrs(config: Dict[str, Any]) -> List[str]:
    cidrs = list(config["company"]["cidrs"])
    networks = [ipaddress.ip_network(cidr) for cidr in cidrs]
    for server in config["dns"]["servers"]:
        if server == "system":
            continue
        address = ipaddress.ip_address(server)
        if not any(address in network for network in networks):
            cidr = f"{address}/32"
            cidrs.append(cidr)
            networks.append(ipaddress.ip_network(cidr))
    return cidrs


def _domain_patterns(config: Dict[str, Any]) -> List[str]:
    return [
        item
        for domain in config["company"]["domains"]
        for item in (domain, f"*.{domain}")
    ]


def _rule_lines(config: Dict[str, Any]) -> List[str]:
    lines = [
        f"DOMAIN-SUFFIX,{domain},DIRECT"
        for domain in config["company"]["domains"]
    ]
    for cidr in _direct_cidrs(config):
        lines.append(f"IP-CIDR,{cidr},DIRECT,no-resolve")
    return lines


def render_surge(config: Dict[str, Any]) -> str:
    dns_servers = ",".join(config["dns"]["servers"])
    lines = [
        "#!name=Company Network",
        "#!desc=Device-local company routing and split DNS overlay",
        "# Generated by scripts/configure_company.py. Keep this file local.",
    ]
    domains = config["company"]["domains"]
    if domains:
        lines.extend(
            [
                "",
                "[General]",
                f"always-real-ip = %APPEND% {', '.join(_domain_patterns(config))}",
                "",
                "[Host]",
            ]
        )
        for domain in domains:
            lines.append(f"{domain} = server:{dns_servers}")
            lines.append(f"*.{domain} = server:{dns_servers}")
    lines.extend(["", "[Rule]", *_rule_lines(config), ""])
    return "\n".join(lines)


def render_shadowrocket(config: Dict[str, Any]) -> str:
    domains = config["company"]["domains"]
    dns_servers = ",".join(config["dns"]["servers"])
    real_ip = ", ".join(_domain_patterns(config))
    use_system_dns = config["dns"]["servers"] == ["system"]
    lines = [
        "#!name=Company Network",
        "#!desc=Device-local company routing and split DNS overlay",
        "# Generated by scripts/configure_company.py. Keep this module local.",
        "[General]",
        f"dns-direct-system = {'true' if use_system_dns else 'false'}",
        "dns-direct-fallback-proxy = false",
        "private-ip-answer = true",
    ]
    if real_ip:
        lines.append(f"always-real-ip = {real_ip}")
        lines.extend(["", "[Host]"])
        for domain in domains:
            lines.append(f"{domain} = server:{dns_servers}")
            lines.append(f"*.{domain} = server:{dns_servers}")
    lines.extend(["", "[Rule]", *_rule_lines(config), ""])
    return "\n".join(lines)


def render_mihomo_script(config: Dict[str, Any]) -> str:
    domains = config["company"]["domains"]
    cidrs = _direct_cidrs(config)
    dns_servers = config["dns"]["servers"]
    return f"""// Generated by scripts/configure_company.py. Keep this script local.
// Attach it as a subscription extension script; subscription refreshes will
// reapply it without replacing the local company settings.
const companyDomains = {json.dumps(domains, ensure_ascii=True, indent=2)};
const directCidrs = {json.dumps(cidrs, ensure_ascii=True, indent=2)};
const companyDns = {json.dumps(dns_servers, ensure_ascii=True, indent=2)};

function unique(values) {{
  return Array.from(new Set(values));
}}

function main(config) {{
  config.dns = config.dns || {{}};

  const realIpDomains = companyDomains.flatMap((domain) => [
    domain,
    `*.${{domain}}`,
  ]);
  config.dns["fake-ip-filter"] = unique(
    (config.dns["fake-ip-filter"] || []).concat(realIpDomains),
  );

  const nameserverPolicy = config.dns["nameserver-policy"] || {{}};
  for (const domain of companyDomains) {{
    nameserverPolicy[`+.${{domain}}`] = companyDns.slice();
  }}
  config.dns["nameserver-policy"] = nameserverPolicy;

  const localRules = companyDomains
    .map((domain) => `DOMAIN-SUFFIX,${{domain}},DIRECT`)
    .concat(
      directCidrs.map((cidr) => `IP-CIDR,${{cidr}},DIRECT,no-resolve`),
    );
  config.rules = unique(localRules.concat(config.rules || []));
  return config;
}}
"""


def render_instructions(config: Dict[str, Any]) -> str:
    vpn = config["vpn"]
    client = vpn["client"] or "the company VPN client"
    ssids = ", ".join(config["wifi"]["ssids"]) or "the company Wi-Fi"
    full_tunnel_warning = (
        "The VPN is marked full tunnel. Follow company policy and disable the "
        "personal proxy if the VPN blocks or forbids it."
        if vpn["tunnel"] == "full"
        else "The VPN is not marked full tunnel; verify its routes before using TUN."
    )
    return f"""Company network local overlays
================================

These files are local artifacts. Updating the remote subscriptions must not
replace or regenerate them.

Important: Surge pre-matching reject rules run before a module's DIRECT rules.
Use Surge's rule test after installation. If a company domain matches a managed
pre-matching reject set, that remote rule must be changed before the module can
allow the domain.

Company Wi-Fi ({ssids})
-----------------------
1. Enable the Work layer.
2. TUN or Surge Enhanced Mode may remain enabled after DNS and routing tests pass.

Company VPN ({client}, {vpn['tunnel']} tunnel)
----------------------------------------------
1. On a desktop, connect the company VPN first.
2. Keep the Work layer enabled.
3. Disable Surge Enhanced Mode or Clash Verge Rev TUN.
4. Enable the client's system proxy mode. DIRECT traffic then follows the VPN's
   system routes and split DNS.

{full_tunnel_warning}

On iPhone and iPad, do not assume Shadowrocket can coexist with a separate
company packet-tunnel VPN. Use the company VPN by itself unless its administrator
documents a supported coexistence or per-app arrangement.

Install once
------------
* Surge: install and enable Surge-Company.sgmodule on the existing local or
  managed profile. Refresh the managed profile normally; the module stays local.
* Shadowrocket: import and enable Shadowrocket-Company.module. Update the main
  Shadowrocket subscription normally; do not turn this local module into a
  remote configuration.
* Clash Verge Rev: create a subscription extension script, paste the contents of
  ClashVerge-Company.js, and associate it only with the Work subscription/profile.

Outside company networks, use the Personal profile or disable the company module
or extension so internal domain names are not sent to public DNS resolvers.
"""


def render_artifacts(config: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Surge-Company.sgmodule": render_surge(config),
        "Shadowrocket-Company.module": render_shadowrocket(config),
        "ClashVerge-Company.js": render_mihomo_script(config),
        "INSTALL.txt": render_instructions(config),
    }


def _secure_local_directories(path: Path) -> None:
    """Keep repository-local private data inaccessible to other local users."""
    try:
        relative = path.resolve().relative_to(LOCAL_ROOT.resolve())
    except ValueError:
        return

    current = LOCAL_ROOT.resolve()
    os.chmod(current, 0o700)
    for part in relative.parts:
        current /= part
        os.chmod(current, 0o700)


def _secure_local_file(path: Path) -> None:
    try:
        path.resolve().relative_to(LOCAL_ROOT.resolve())
    except ValueError:
        return
    _secure_local_directories(path.parent)
    os.chmod(path, 0o600)


def _assert_private_repo_path(path: Path, label: str) -> None:
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(ROOT)
    except ValueError:
        return

    try:
        repository = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        repository = None

    if repository is None or repository.returncode != 0:
        try:
            resolved.relative_to(LOCAL_ROOT.resolve())
        except ValueError as exc:
            raise ConfigError(
                f"{label} must be under {LOCAL_ROOT} when running outside a Git checkout"
            ) from exc
        return

    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(relative)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if tracked.returncode == 0:
        raise ConfigError(f"{label} is tracked by Git: {relative}")

    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", "--", str(relative)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if ignored.returncode != 0:
        raise ConfigError(
            f"{label} must be outside the repository or ignored by Git: {relative}"
        )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_local_directories(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_artifacts(config: Dict[str, Any], output_dir: Path) -> List[Path]:
    _assert_private_repo_path(output_dir, "output directory")
    paths = []
    for name, content in render_artifacts(config).items():
        path = output_dir / name
        _atomic_write(path, content)
        paths.append(path)
    return paths


def _split_prompt(prompt: str) -> List[str]:
    return [value.strip() for value in input(prompt).split(",") if value.strip()]


def prompt_config() -> Dict[str, Any]:
    print("Create a device-local company network overlay.")
    domains = _split_prompt("Company domain suffixes (comma-separated): ")
    cidrs = _split_prompt(
        "Company private IPv4 addresses or CIDRs (comma-separated, optional): "
    )
    ssids = _split_prompt("Company Wi-Fi SSIDs (comma-separated, optional): ")
    dns_servers = _split_prompt("Company DNS IPs (blank uses current system DNS): ")
    client = input("Company VPN client name (optional): ").strip()
    tunnel = input("VPN tunnel type [split/full/unknown] (default unknown): ").strip().lower()
    return normalize_config(
        {
            "version": 1,
            "company": {"domains": domains, "cidrs": cidrs},
            "dns": {"servers": dns_servers or ["system"]},
            "wifi": {"ssids": ssids},
            "vpn": {"client": client, "tunnel": tunnel or "unknown"},
        }
    )


def save_config(config: Dict[str, Any], path: Path) -> None:
    _assert_private_repo_path(path, "configuration path")
    _atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate local company overlays for Surge, Shadowrocket, and Clash Verge Rev."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and render in memory without writing artifacts",
    )
    return parser.parse_args(argv)


def main(argv: List[str] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    args.config = args.config.expanduser()
    args.output_dir = args.output_dir.expanduser()
    try:
        if args.config.is_file():
            if not (args.check and args.config.resolve() == EXAMPLE_CONFIG.resolve()):
                _assert_private_repo_path(args.config, "configuration path")
                _secure_local_file(args.config)
            config = load_config(args.config)
        elif args.check:
            raise ConfigError(f"configuration not found: {args.config}")
        elif sys.stdin.isatty():
            config = prompt_config()
            save_config(config, args.config)
            print(f"Saved local configuration: {args.config}")
        else:
            raise ConfigError(
                f"configuration not found: {args.config}; run interactively to create it"
            )

        artifacts = render_artifacts(config)
        if args.check:
            print(
                f"Company configuration is valid; {len(artifacts)} artifacts render successfully."
            )
            return 0

        paths = write_artifacts(config, args.output_dir)
        print("Generated local company overlays:")
        for path in paths:
            print(f"  {path}")
        return 0
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
