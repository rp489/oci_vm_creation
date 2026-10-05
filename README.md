# Hyderabad Always Free VM provisioner

GitHub Actions retries OCI provisioning at five-minute intervals, with a total budget of up to 1,000 further attempts. Railway is not required. Each launch run makes up to 60 attempts, then passes its remaining budget to an automatic continuation while provisioning remains enabled. A direct invocation of `provision.sh` still makes only one attempt.

## Resource limits

The default request is one `VM.Standard.A1.Flex` Arm VM with 2 OCPUs and 12 GB RAM in `ap-hyderabad-1`. The boot volume uses up to 200 GB, reduced to the remaining free storage allowance when other boot or block volumes exist. It requires at least 50 GB of remaining storage and requests the Balanced boot-volume performance level (10 VPUs per GB).

As checked on 5 October 2026, Oracle documents a combined A1 allowance of 2 OCPUs and 12 GB RAM for an Always Free tenancy, and 200 GB of combined boot and block volumes. These are tenancy-wide limits. The provisioner counts non-terminated instances, including stopped instances, and all non-terminated boot and block volumes across the root compartment and its subcompartments in Hyderabad. Inventory failures prevent launch.

Hyderabad must be the tenancy's home region. Another region, an unknown home region, a paid shape, a disabled free-tier guard, or insufficient remaining resources prevents launch. The provisioner does not change the account plan, existing VMs, networks, permissions, or other infrastructure.

This is a conservative resource check, not a billing guarantee. Confirm the account's actual entitlement and monthly consumption before enabling launch. Concurrent changes made outside this workflow can alter the remaining allowance. OCI can reclaim idle Always Free instances, and retries cannot guarantee that Hyderabad capacity will become available.

Sources:

- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle regions](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm)

## OCI access

Use an OCI API-signing key rather than sharing a console password. An API-signing key is separate from the SSH key used to access the VM.

If you already have a registered OCI API key, use its existing configuration. Otherwise, open the OCI user's API Keys page, add an API key pair, download the private key, and retain the supplied configuration snippet. Registering a new key or granting permissions needs the account owner's approval. Keep the private key outside this repository and never paste it into chat, source code, commits, or logs.

The API user needs read access to tenancy region subscriptions, availability domains, and the full compartment hierarchy; read access to instances, boot volumes, and block volumes throughout the tenancy; read access to the selected platform image and subnet; and permission to launch an instance, create its boot volume and VNIC, and use the selected subnet. Incomplete inventory access stops the preflight. The workflow does not grant these permissions itself.

Use an existing Hyderabad subnet. The default request assigns a public IP, so that subnet must permit it. Network routes and SSH access rules must already be configured appropriately; this script does not open ports.

[Oracle API-signing key instructions](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)

## GitHub setup

Add these repository Actions secrets under Settings > Secrets and variables > Actions:

| Secret | Value |
|---|---|
| `OCI_TENANCY` | Tenancy Oracle Cloud Identifier (OCID) from the API-key configuration |
| `OCI_USER` | API user's OCID from that configuration |
| `OCI_FINGERPRINT` | Registered API-key fingerprint |
| `OCI_PRIVATE_KEY` | The API-signing private key in PEM format |
| `OCI_COMPARTMENT_ID` | Existing target compartment OCID |
| `OCI_SUBNET_ID` | Existing Hyderabad subnet OCID |
| `VM_SSH_PUBLIC_KEY` | SSH public key to install on the VM; keep the SSH private key locally |

The API private key can contain actual newlines or literal `\n` separators. The provisioner creates temporary files with restrictive permissions and removes them on exit.

Optional secrets:

- `OCI_AVAILABILITY_DOMAIN`: if supplied, it must match the single Hyderabad availability domain discovered from OCI.
- `OCI_IMAGE_ID`: if supplied, it must appear in the verified list of available, compatible Ubuntu platform images in Hyderabad. Custom and Marketplace images are rejected.

Optional repository variables:

- `VM_DISPLAY_NAME`: defaults to `oplify-agent`. Keep this name unchanged while provisioning or after success, since it identifies the existing target VM.
- `OCI_PROVISIONING_ENABLED`: absent by default. Set it to exactly `true` only after the launch configuration has been approved.

Publish the workflow on the repository's default branch. Publishing alone does not start provisioning.

1. Publish the reviewed changes to the intended repository.
2. Enter the OCI secrets and confirm Hyderabad is the home region.
3. Open Actions > Provision Hyderabad Always Free VM > Run workflow, leaving the mode as `preflight`. This reads OCI without creating resources.
4. Check that the preflight succeeds and approve the specific VM creation configuration.
5. Set `OCI_PROVISIONING_ENABLED=true` for automatic provisioning, then manually run the workflow with mode `launch` and `remaining_attempts=1000` to start immediately. Clear capacity or rate-limit rejections are retried within the active run. Do not start another launch chain while one is active.

The workflow starts immediately when dispatched and handles five-minute waits itself. It has no cron trigger, so a delayed scheduled run cannot accidentally start a new 1,000-attempt budget. GitHub runner startup and queuing can still delay execution.

Within a launch run, the next attempt starts no sooner than five minutes after the previous attempt started. Every attempt repeats the inventory and duplicate checks. After at most 60 rejected attempts, the workflow uses its built-in token to dispatch a new run with `automatic=true` and the reduced `remaining_attempts`; that run requires `OCI_PROVISIONING_ENABLED=true`. The final run stops and disables the workflow when the total budget reaches zero. Runner startup and GitHub queuing can delay transitions. Jobs have a 330-minute timeout, below GitHub's six-hour runner limit. An unexpected job failure stops that run and does not dispatch a continuation.

The initial `attempts_per_run` can be set between 1 and 60; continuations use 60. Setting the initial window to 1 permits a live handoff check after the first rejection and its five-minute wait, without increasing the total budget. At five-minute intervals, 1,000 rejected attempts span about 83 hours plus runner startup delays. Capacity is not guaranteed.

To stop an active retry loop, disable this workflow in GitHub Actions. The loop checks that state before each attempt. Set `OCI_PROVISIONING_ENABLED=false` to block automatic continuations; this flag does not interrupt an active run. Cancel the active run if an immediate stop is required, and inspect OCI for an unresolved launch before restarting.

Standard GitHub-hosted runners are free for public repositories. Private repositories use the owner's Actions allowance; a five-minute schedule can exceed it. Check the account's plan and spending controls before enabling a private repository's schedule.

- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

## Launch results and duplicate prevention

Only one workflow run operates at a time. Before launching, the provisioner checks for an existing non-terminated instance with the target display name in the target compartment. Existing instances, including provisioning or stopped instances, prevent a second launch. A matching name with a conflicting shape or availability domain, or multiple matching instances, fails the preflight.

A clear out-of-host-capacity or rate-limit rejection allows the next attempt after the five-minute interval. Every other launch error, including an uncertain timeout or server error, stops retries and requests that the workflow be disabled for manual investigation. A successful launch response also disables the workflow immediately; OCI acceptance does not prove the VM has reached RUNNING. Confirm that state in the OCI Console.

The workflow uses its built-in GitHub token with `actions: write` to check its state, disable itself, and dispatch capacity-retry continuations. It does not store a PAT. If disabling or continuation fails, the run reports failure; inspect OCI and the run before resuming. Never enable another provisioner for the same target concurrently. If a run is interrupted during a launch, inspect OCI before enabling or re-running it.

To resume after investigation, first confirm no matching VM or unresolved launch exists, then enable the workflow in GitHub Actions. Do not change the display name to bypass the duplicate check.

## GitHub publication credentials

A PAT used to publish workflow files must have access to the intended repository and permission to write repository contents and workflows. Running or enabling workflows also requires Actions write permission. If Codex is to configure secrets or variables, the PAT additionally needs the corresponding repository permissions. You can enter those values yourself instead.

A PAT for another repository is not sufficient. Share its local file path rather than the token value. The Actions jobs themselves use the built-in GitHub token; they do not need your PAT as an OCI credential.

## Direct invocation and verification

With the required environment variables and OCI CLI installed, `bash provision.sh` defaults to read-only preflight. To explicitly permit one launch, set `PROVISION_MODE=launch`. Local invocations exit after one attempt; they do not provide their own scheduler. The existing Dockerfile also runs once and defaults to preflight.

Direct invocation retains these optional environment settings: `VM_OCPUS=2`, `VM_MEMORY_GB=12`, `VM_BOOT_VOLUME_GB=200`, `ASSIGN_PUBLIC_IP=true`, `OCI_IMAGE_OS=Canonical Ubuntu`, and `OCI_IMAGE_OS_VERSION=22.04`. Values can be reduced within the documented limits. The default image is the newest verified Ubuntu 22.04 Arm platform image available in Hyderabad, preserving the existing OS version.

The repository verification workflow runs Bash syntax checks and mocked tests without OCI credentials or cloud access:

```bash
bash -n provision.sh
python -B -m unittest discover -s tests -v
```

The provisioning workflow installs OCI CLI 3.94.1 into an isolated environment on its temporary Linux runner. No local package installation is required to run the mocked tests.
