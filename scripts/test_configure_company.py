#!/usr/bin/env python3

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.configure_company import (
    ConfigError,
    ROOT,
    normalize_config,
    render_artifacts,
    write_artifacts,
)


SAMPLE = {
    "version": 1,
    "company": {
        "domains": ["Corp.Example.com", "*.corp.example.com"],
        "cidrs": ["10.20.1.3/16"],
    },
    "dns": {"servers": ["system"]},
    "wifi": {"ssids": ["Company WiFi"]},
    "vpn": {"client": "Example VPN", "tunnel": "split"},
}


class CompanyOverlayTests(unittest.TestCase):
    def test_normalizes_and_deduplicates_input(self) -> None:
        config = normalize_config(SAMPLE)
        self.assertEqual(config["company"]["domains"], ["corp.example.com"])
        self.assertEqual(config["company"]["cidrs"], ["10.20.0.0/16"])

    def test_accepts_private_host_address_as_32(self) -> None:
        raw = json.loads(json.dumps(SAMPLE))
        raw["company"]["cidrs"] = ["10.20.1.25"]
        config = normalize_config(raw)
        self.assertEqual(config["company"]["cidrs"], ["10.20.1.25/32"])
        self.assertIn(
            "IP-CIDR,10.20.1.25/32,DIRECT,no-resolve",
            render_artifacts(config)["Surge-Company.sgmodule"],
        )

    def test_renders_persistent_layers_without_replacing_base_rules(self) -> None:
        artifacts = render_artifacts(normalize_config(SAMPLE))

        surge = artifacts["Surge-Company.sgmodule"]
        local_rule = "DOMAIN-SUFFIX,corp.example.com,DIRECT"
        self.assertIn(
            "always-real-ip = %APPEND% corp.example.com, *.corp.example.com",
            surge,
        )
        self.assertIn("corp.example.com = server:system", surge)
        self.assertIn(local_rule, surge)
        self.assertNotIn("[SSID Setting]", surge)
        self.assertNotIn("#!include", surge)

        shadowrocket = artifacts["Shadowrocket-Company.module"]
        self.assertIn("dns-direct-system = true", shadowrocket)
        self.assertIn("corp.example.com = server:system", shadowrocket)
        self.assertNotIn("fallback-dns-server", shadowrocket)
        self.assertIn("private-ip-answer = true", shadowrocket)
        self.assertIn("dns-direct-fallback-proxy = false", shadowrocket)
        self.assertIn(
            "always-real-ip = corp.example.com, *.corp.example.com",
            shadowrocket,
        )

        mihomo = artifacts["ClashVerge-Company.js"]
        self.assertIn('(config.dns["fake-ip-filter"] || []).concat(realIpDomains)', mihomo)
        self.assertIn("localRules.concat(config.rules || [])", mihomo)
        self.assertNotIn('config.rules = localRules;', mihomo)

    def test_render_is_deterministic(self) -> None:
        config = normalize_config(SAMPLE)
        self.assertEqual(render_artifacts(config), render_artifacts(config))

    def test_rejects_unknown_fields_and_invalid_networks(self) -> None:
        invalid = json.loads(json.dumps(SAMPLE))
        invalid["company"]["unexpected"] = []
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["company"]["cidrs"] = ["10.20.0.999/24"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["company"]["domains"] = ["10.20.0.1"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["wifi"]["ssids"] = ["Company\rInjected"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["company"]["cidrs"] = ["fd00:20::/64"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["dns"]["servers"] = ["8.8.8.8"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["dns"]["servers"] = ["system", "10.20.0.53"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["company"]["cidrs"] = ["203.0.113.7/0"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

        invalid = json.loads(json.dumps(SAMPLE))
        invalid["company"]["cidrs"] = ["10.0.0.0/8"]
        with self.assertRaises(ConfigError):
            normalize_config(invalid)

    def test_fixed_dns_is_domain_scoped_and_direct(self) -> None:
        raw = json.loads(json.dumps(SAMPLE))
        raw["dns"]["servers"] = ["10.30.0.53"]
        artifacts = render_artifacts(normalize_config(raw))

        surge = artifacts["Surge-Company.sgmodule"]
        self.assertIn("corp.example.com = server:10.30.0.53", surge)
        self.assertIn("IP-CIDR,10.30.0.53/32,DIRECT,no-resolve", surge)

        shadowrocket = artifacts["Shadowrocket-Company.module"]
        self.assertIn("dns-direct-system = false", shadowrocket)
        self.assertIn("corp.example.com = server:10.30.0.53", shadowrocket)
        self.assertNotIn("dns-server =", shadowrocket)

        mihomo = artifacts["ClashVerge-Company.js"]
        self.assertIn('"10.30.0.53/32"', mihomo)
        self.assertIn('"10.30.0.53"', mihomo)

    def test_refuses_unignored_repository_output(self) -> None:
        config = normalize_config(SAMPLE)
        with self.assertRaises(ConfigError):
            write_artifacts(config, ROOT / "profiles")

    def test_example_is_check_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "configure_company.py"),
                    "--config",
                    str(ROOT / "config" / "company.local.example.json"),
                    "--output-dir",
                    directory,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("ERROR: configuration path", completed.stderr)

    def test_installation_warns_about_surge_pre_matching(self) -> None:
        instructions = render_artifacts(normalize_config(SAMPLE))["INSTALL.txt"]
        self.assertIn("pre-matching reject rules run before", instructions)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_generated_mihomo_script_merges_runtime_config(self) -> None:
        script = render_artifacts(normalize_config(SAMPLE))["ClashVerge-Company.js"]
        harness = """
const result = main({
  dns: {"fake-ip-filter": ["*.local"], "nameserver-policy": {"+.lan": ["system"]}},
  rules: ["MATCH,FINAL"]
});
console.log(JSON.stringify(result));
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.js"
            path.write_text(script + harness, encoding="utf-8")
            completed = subprocess.run(
                ["node", str(path)], check=True, capture_output=True, text=True
            )
        result = json.loads(completed.stdout)
        self.assertEqual(result["rules"][0], "DOMAIN-SUFFIX,corp.example.com,DIRECT")
        self.assertEqual(result["rules"][-1], "MATCH,FINAL")
        self.assertIn("*.local", result["dns"]["fake-ip-filter"])
        self.assertIn("*.corp.example.com", result["dns"]["fake-ip-filter"])
        self.assertEqual(
            result["dns"]["nameserver-policy"]["+.corp.example.com"], ["system"]
        )


if __name__ == "__main__":
    unittest.main()
