"""Read-only OCI checks. Refuse to launch when inventory cannot be verified."""

import json
import math
import os
import subprocess
import sys


REGION = "ap-hyderabad-1"


def cli(*args):
    try:
        result = subprocess.run(
            ["oci", *args, "--region", REGION, "--no-retry",
             "--connection-timeout", "10", "--read-timeout", "45"],
            capture_output=True, text=True, timeout=70, check=True,
        )
        # OCI CLI suppresses the JSON output for successful empty lists.
        # Failed requests still raise above, and empty get responses are errors.
        if args[2] == "list" and not result.stdout.strip():
            return []
        return json.loads(result.stdout)["data"]
    except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
        # CLI errors can contain account identifiers. Report only the operation.
        raise ValueError(f"Unable to verify {' '.join(args[:3])}; check OCI access.") from exc


def active(resources, deleted_state):
    return [r for r in resources if r["lifecycle-state"] != deleted_state]


def number(value):
    try:
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise ValueError("OCI returned invalid resource usage.") from exc


def check(display_name, shape, ocpus, memory, boot_gb, image_os, image_version, public_ip):
    tenancy = os.environ["OCI_TENANCY"]
    target = os.environ["OCI_COMPARTMENT_ID"]
    regions = cli("iam", "region-subscription", "list", "--tenancy-id", tenancy)
    home = [r for r in regions if r["is-home-region"]]
    if len(home) != 1 or home[0]["region-name"] != REGION or home[0]["status"] != "READY":
        raise ValueError("Hyderabad must be the verified, ready tenancy home region.")

    domains = cli("iam", "availability-domain", "list", "--compartment-id", tenancy, "--all")
    if len(domains) != 1 or "AP-HYDERABAD-1-AD-1" not in domains[0]["name"].upper():
        raise ValueError("Unable to verify the single Hyderabad availability domain.")
    ad = domains[0]["name"]
    if os.environ.get("OCI_AVAILABILITY_DOMAIN", ad) not in ("", ad):
        raise ValueError("Configured availability domain does not match Hyderabad.")

    subnet = cli("network", "subnet", "get", "--subnet-id", os.environ["OCI_SUBNET_ID"])
    if subnet["lifecycle-state"] != "AVAILABLE" or subnet["availability-domain"] not in (None, ad):
        raise ValueError("The subnet must be available in Hyderabad.")
    if public_ip == "true" and subnet["prohibit-public-ip-on-vnic"]:
        raise ValueError("The selected subnet does not allow a public IP address.")

    compartments = cli("iam", "compartment", "list", "--compartment-id", tenancy,
                       "--compartment-id-in-subtree", "true", "--access-level", "ANY", "--all")
    compartment_ids = {tenancy, *(c["id"] for c in active(compartments, "DELETED"))}
    if target not in compartment_ids:
        raise ValueError("The target compartment is not in the verified tenancy.")
    if subnet["compartment-id"] not in compartment_ids:
        raise ValueError("The subnet is not in the verified tenancy.")

    instances = []
    volumes = []
    for compartment in sorted(compartment_ids):
        instances.extend(active(cli("compute", "instance", "list", "--compartment-id", compartment, "--all"), "TERMINATED"))
        volumes.extend(active(cli("bv", "volume", "list", "--compartment-id", compartment, "--all"), "TERMINATED"))
        volumes.extend(active(cli("bv", "boot-volume", "list", "--compartment-id", compartment,
                                  "--availability-domain", ad, "--all"), "TERMINATED"))

    matches = [i for i in instances if i["compartment-id"] == target and i["display-name"] == display_name]
    if len(matches) > 1:
        raise ValueError("Multiple target VMs exist. Resolve them manually before provisioning.")
    if matches and (matches[0]["shape"] != shape or matches[0]["availability-domain"] != ad):
        raise ValueError("An existing target VM conflicts with the requested Hyderabad A1 VM.")

    a1 = [i for i in instances if i["shape"] == "VM.Standard.A1.Flex"]
    used_cpu = sum(number(i["shape-config"]["ocpus"]) for i in a1)
    used_memory = sum(number(i["shape-config"]["memory-in-gbs"]) for i in a1)
    used_disk = sum(number(v["size-in-gbs"]) for v in volumes)
    if not all(math.isfinite(n) and n >= 0 for n in (used_cpu, used_memory, used_disk)):
        raise ValueError("OCI returned invalid resource usage.")
    extra_cpu, extra_memory = (0, 0) if matches else (float(ocpus), float(memory))
    if used_cpu + extra_cpu > 2 or used_memory + extra_memory > 12:
        raise ValueError("Existing and requested A1 resources exceed 2 OCPUs or 12 GB RAM.")
    if used_disk > 200:
        raise ValueError("Existing boot and block volumes exceed the 200 GB free allowance.")
    remaining_boot = min(int(boot_gb), math.floor(200 - used_disk))
    if not matches and remaining_boot < 50:
        raise ValueError("Less than 50 GB remains in the account-wide free volume allowance.")
    if matches:
        return dict(existing_state=matches[0]["lifecycle-state"], availability_domain=ad,
                    image_id=None, boot_volume_gb=0)

    if image_os != "Canonical Ubuntu":
        raise ValueError("Only the Ubuntu platform image is supported by this provisioner.")
    images = cli("compute", "image", "list", "--compartment-id", target,
                 "--operating-system", image_os, "--operating-system-version", image_version,
                 "--shape", shape, "--all")
    eligible = [i for i in images if i["lifecycle-state"] == "AVAILABLE"
                and i["compartment-id"] is None and i.get("listing-type") in (None, "NONE")
                and i["operating-system"] == image_os and i["operating-system-version"] == image_version]
    selected_id = os.environ.get("OCI_IMAGE_ID")
    if selected_id:
        eligible = [i for i in eligible if i["id"] == selected_id]
    if not eligible:
        raise ValueError("No compatible Ubuntu platform image was verified in Hyderabad.")
    image = max(eligible, key=lambda i: i["time-created"])
    return dict(existing_state=None, availability_domain=ad,
                image_id=image["id"], boot_volume_gb=remaining_boot)


if __name__ == "__main__":
    try:
        print(json.dumps(check(*sys.argv[1:])))
    except (ValueError, KeyError, TypeError, IndexError) as error:
        message = str(error) if isinstance(error, ValueError) else "Incomplete OCI inventory; no launch is allowed."
        print(f"[ERROR] {message}", file=sys.stderr)
        sys.exit(1)
