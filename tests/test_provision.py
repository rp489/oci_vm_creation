"""Test quota and launch decisions using synthetic OCI responses only."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight", ROOT / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
AD = "test:AP-HYDERABAD-1-AD-1"
ENV = dict(OCI_TENANCY="test-tenancy", OCI_COMPARTMENT_ID="test-compartment",
           OCI_SUBNET_ID="test-subnet", OCI_REGION="ap-hyderabad-1",
           OCI_USER="test-user", OCI_FINGERPRINT="test-fingerprint",
           OCI_PRIVATE_KEY="synthetic-test-key", VM_SSH_PUBLIC_KEY="synthetic-public-key")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.regions = [{"is-home-region": True, "region-name": preflight.REGION, "status": "READY"}]
        self.compartments = [{"id": "test-compartment", "lifecycle-state": "ACTIVE"}]
        self.subnet = {"lifecycle-state": "AVAILABLE", "availability-domain": None,
                       "prohibit-public-ip-on-vnic": False, "compartment-id": "test-compartment"}
        self.instances = {}
        self.volumes = {}
        self.boot_volumes = {}
        self.images = [{"id": "test-image", "lifecycle-state": "AVAILABLE", "compartment-id": None,
                        "operating-system": "Canonical Ubuntu", "operating-system-version": "22.04",
                        "time-created": "2026-09-01", "listing-type": "NONE"}]
        self.calls = []
        self.mock = patch.object(preflight, "cli", side_effect=self.cli)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def cli(self, *args):
        self.calls.append(args)
        operation = args[:3]
        if operation == ("iam", "region-subscription", "list"):
            return self.regions
        if operation == ("iam", "availability-domain", "list"):
            return [{"name": AD}]
        if operation == ("network", "subnet", "get"):
            return self.subnet
        if operation == ("iam", "compartment", "list"):
            return self.compartments
        if operation == ("compute", "image", "list"):
            return self.images
        compartment = args[args.index("--compartment-id") + 1]
        if operation == ("compute", "instance", "list"):
            return self.instances.get(compartment, [])
        if operation == ("bv", "volume", "list"):
            return self.volumes.get(compartment, [])
        if operation == ("bv", "boot-volume", "list"):
            return self.boot_volumes.get(compartment, [])
        raise AssertionError("Unexpected mock command")

    def check(self):
        return preflight.check("oplify-agent", "VM.Standard.A1.Flex", "2", "12", "200",
                               "Canonical Ubuntu", "22.04", "true")

    def instance(self, name="other", state="RUNNING", cpu=1, memory=6):
        return {"display-name": name, "lifecycle-state": state, "shape": "VM.Standard.A1.Flex",
                "availability-domain": AD, "compartment-id": "test-compartment",
                "shape-config": {"ocpus": cpu, "memory-in-gbs": memory}}

    def test_empty_tenancy_uses_current_maximum(self):
        result = self.check()
        self.assertIsNone(result["existing_state"])
        self.assertEqual(result["boot_volume_gb"], 200)
        self.assertEqual(result["availability_domain"], AD)
        self.assertTrue(any(c[:3] == ("compute", "instance", "list") and "test-tenancy" in c for c in self.calls))
        self.assertTrue(all("launch" not in c for c in self.calls))

    def test_wrong_home_region_blocks(self):
        self.regions[0]["region-name"] = "ap-mumbai-1"
        with self.assertRaisesRegex(ValueError, "home region"):
            self.check()

    def test_missing_home_region_blocks(self):
        self.regions.clear()
        with self.assertRaises(ValueError):
            self.check()

    def test_wrong_availability_domain_blocks(self):
        os.environ["OCI_AVAILABILITY_DOMAIN"] = "test:AP-MUMBAI-1-AD-1"
        with self.assertRaisesRegex(ValueError, "availability domain"):
            self.check()

    def test_private_subnet_with_public_ip_blocks(self):
        self.subnet["prohibit-public-ip-on-vnic"] = True
        with self.assertRaisesRegex(ValueError, "public IP"):
            self.check()

    def test_subnet_in_other_tenancy_blocks(self):
        self.subnet["compartment-id"] = "other-tenancy"
        with self.assertRaisesRegex(ValueError, "subnet"):
            self.check()

    def test_existing_vm_is_not_created_again(self):
        self.instances["test-compartment"] = [self.instance("oplify-agent", cpu=2, memory=12)]
        self.assertEqual(self.check()["existing_state"], "RUNNING")
        self.assertFalse(any(c[:3] == ("compute", "image", "list") for c in self.calls))

    def test_provisioning_and_stopped_targets_block_duplicates(self):
        for state in ("PROVISIONING", "STARTING", "STOPPED", "TERMINATING"):
            with self.subTest(state=state):
                self.instances["test-compartment"] = [self.instance("oplify-agent", state, cpu=2, memory=12)]
                self.assertEqual(self.check()["existing_state"], state)

    def test_multiple_targets_block(self):
        self.instances["test-compartment"] = [self.instance("oplify-agent"), self.instance("oplify-agent")]
        with self.assertRaisesRegex(ValueError, "Multiple"):
            self.check()

    def test_stopped_a1_in_nested_compartment_counts(self):
        self.compartments.append({"id": "nested-compartment", "lifecycle-state": "ACTIVE"})
        self.instances["nested-compartment"] = [self.instance(state="STOPPED")]
        with self.assertRaisesRegex(ValueError, "exceed 2 OCPUs"):
            self.check()

    def test_terminated_instances_do_not_consume_compute_budget(self):
        self.instances["test-compartment"] = [self.instance(state="TERMINATED", cpu=4, memory=24)]
        self.assertIsNone(self.check()["existing_state"])

    def test_boot_and_block_volumes_in_other_compartments_count(self):
        self.volumes["test-tenancy"] = [{"lifecycle-state": "AVAILABLE", "size-in-gbs": 50}]
        self.boot_volumes["test-compartment"] = [{"lifecycle-state": "AVAILABLE", "size-in-gbs": 50}]
        self.assertEqual(self.check()["boot_volume_gb"], 100)

    def test_insufficient_disk_blocks(self):
        self.volumes["test-tenancy"] = [{"lifecycle-state": "AVAILABLE", "size-in-gbs": 151}]
        with self.assertRaisesRegex(ValueError, "Less than 50"):
            self.check()

    def test_incomplete_inventory_blocks(self):
        self.instances["test-tenancy"] = [{"shape": "VM.Standard.A1.Flex"}]
        with self.assertRaises(KeyError):
            self.check()

    def test_nonfinite_usage_blocks(self):
        self.instances["test-compartment"] = [self.instance(cpu="nan")]
        with self.assertRaisesRegex(ValueError, "invalid resource usage"):
            self.check()

    def test_latest_platform_image_selected(self):
        newer = copy.deepcopy(self.images[0])
        newer.update(id="newer-image", **{"time-created": "2026-10-01"})
        self.images.append(newer)
        self.assertEqual(self.check()["image_id"], "newer-image")

    def test_custom_or_marketplace_image_rejected(self):
        for changes in ({"compartment-id": "test-compartment"}, {"listing-type": "COMMUNITY"}):
            with self.subTest(changes=changes):
                self.images[0] = dict(self.images[0], **changes)
                with self.assertRaisesRegex(ValueError, "platform image"):
                    self.check()
                self.images[0].update({"compartment-id": None, "listing-type": "NONE"})

    def test_explicit_incompatible_image_rejected(self):
        os.environ["OCI_IMAGE_ID"] = "unverified-image"
        with self.assertRaisesRegex(ValueError, "platform image"):
            self.check()

    def test_cli_failure_does_not_expose_response(self):
        self.mock.stop()
        error = subprocess.CalledProcessError(1, ["oci"], stderr="synthetic-sensitive-response")
        with patch.object(preflight.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(ValueError, "Unable to verify") as result:
                preflight.cli("iam", "compartment", "list")
        self.assertNotIn("synthetic-sensitive-response", str(result.exception))

    def test_cli_successful_empty_list_is_valid(self):
        self.mock.stop()
        result = subprocess.CompletedProcess(["oci"], 0, stdout="", stderr="")
        with patch.object(preflight.subprocess, "run", return_value=result):
            self.assertEqual(preflight.cli("iam", "compartment", "list"), [])

    def test_cli_successful_empty_get_is_rejected(self):
        self.mock.stop()
        result = subprocess.CompletedProcess(["oci"], 0, stdout="", stderr="")
        with patch.object(preflight.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "Unable to verify"):
                preflight.cli("network", "subnet", "get")

    def test_cli_malformed_success_response_is_rejected(self):
        self.mock.stop()
        result = subprocess.CompletedProcess(["oci"], 0, stdout="invalid response", stderr="")
        with patch.object(preflight.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(ValueError, "Unable to verify"):
                preflight.cli("iam", "compartment", "list")


class ShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = os.environ.get("TEST_BASH") or shutil.which("bash")
        if not cls.bash:
            raise RuntimeError("Bash is required to verify the provisioning shell script.")

    def run_script(self, settings=None, result=None, error=None, existing=None):
        with tempfile.TemporaryDirectory(prefix="oci-provision-test-") as directory:
            temp = Path(directory)
            data = dict(existing_state=existing, availability_domain=AD,
                        image_id="test-image", boot_volume_gb=200)
            (temp / "preflight.json").write_text(json.dumps(data), encoding="utf-8")
            (temp / "result.json").write_text(json.dumps(result or {"data": {"id": "test-instance", "lifecycle-state": "PROVISIONING"}}), encoding="utf-8")
            (temp / "error.json").write_text(json.dumps(error) if isinstance(error, dict) else error or "", encoding="utf-8")
            # Shell functions replace all OCI calls and preflight execution. The
            # real preflight's inventory decisions are tested separately above.
            (temp / "mock.sh").write_text('''
python3() {
    if [[ "$1" == */preflight.py ]]; then cat "$TEST_DIR/preflight.json";
    else python "$@" | tr -d '\\r'; fi
}
oci() {
    printf '%s\\n' "$*" >> "$TEST_DIR/calls"
    if [[ -s "$TEST_DIR/error.json" ]]; then cat "$TEST_DIR/error.json" >&2; return 1;
    else cat "$TEST_DIR/result.json"; fi
}
export -f python3 oci
''', encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("OCI_", "VM_", "PROVISION_", "FREE_TIER_"))}
            env.update(ENV)
            env.update(PROVISION_MODE="launch", BASH_ENV=str(temp / "mock.sh").replace("\\", "/"),
                       TEST_DIR=str(temp).replace("\\", "/"), GITHUB_OUTPUT=str(temp / "outputs").replace("\\", "/"))
            env.update(settings or {})
            process = subprocess.run([self.bash, str(ROOT / "provision.sh").replace("\\", "/")],
                                     env=env, capture_output=True, text=True, timeout=30)
            calls = (temp / "calls").read_text() if (temp / "calls").exists() else ""
            outputs = (temp / "outputs").read_text() if (temp / "outputs").exists() else ""
            return process, calls, outputs

    def test_default_preflight_never_launches(self):
        process, calls, outputs = self.run_script({"PROVISION_MODE": "preflight"})
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(calls, "")
        self.assertEqual(outputs, "")

    def test_launch_uses_current_resources_once(self):
        process, calls, outputs = self.run_script()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(calls.splitlines()), 1)
        self.assertIn('"ocpus": 2', calls)
        self.assertIn('"memoryInGBs": 12', calls)
        self.assertIn("--region ap-hyderabad-1", calls)
        self.assertIn('"bootVolumeVpusPerGB":10', calls)
        self.assertIn("pause=true", outputs)

    def test_wrong_region_or_paid_settings_never_launch(self):
        for settings in ({"OCI_REGION": "ap-mumbai-1"}, {"VM_OCPUS": "4"},
                         {"VM_MEMORY_GB": "24"}, {"VM_BOOT_VOLUME_GB": "201"},
                         {"VM_MEMORY_GB": "08"}, {"VM_OCPUS": "0"},
                         {"VM_BOOT_VOLUME_GB": "18446744073709551816"},
                         {"VM_OCPUS": "2", "VM_MEMORY_GB": "1"},
                         {"FREE_TIER_STRICT": "false"}, {"VM_SHAPE": "VM.Standard.A2.Flex"}):
            with self.subTest(settings=settings):
                process, calls, _ = self.run_script(settings)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(calls, "")

    def test_existing_vm_never_launches_and_pauses(self):
        process, calls, outputs = self.run_script(existing="PROVISIONING")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(calls, "")
        self.assertIn("pause=true", outputs)

    def test_known_capacity_and_throttle_rejections_allow_next_run(self):
        for error in ({"status": 500, "code": "InternalError", "message": "Out of host capacity."},
                      {"status": 429, "code": "TooManyRequests", "message": "Rate limited"}):
            with self.subTest(error=error):
                process, calls, outputs = self.run_script(error=error)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(len(calls.splitlines()), 1)
                self.assertTrue(outputs.endswith("pause=false\n"))

    def test_ambiguous_and_auth_errors_pause_without_exposing_response(self):
        for error in ("synthetic-sensitive-timeout", {"status": 500, "code": "InternalError", "message": "synthetic-sensitive-server-error"},
                      {"status": 401, "code": "NotAuthenticated", "message": "synthetic-sensitive-auth-error"}):
            with self.subTest(error=error):
                process, calls, outputs = self.run_script(error=error)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(len(calls.splitlines()), 1)
                self.assertIn("pause=true", outputs)
                self.assertNotIn("synthetic-sensitive", process.stdout + process.stderr)

    def test_invalid_success_response_pauses(self):
        process, calls, outputs = self.run_script(result={"data": {}})
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(len(calls.splitlines()), 1)
        self.assertIn("pause=true", outputs)


if __name__ == "__main__":
    unittest.main()
