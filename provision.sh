#!/bin/bash

# =============================================================
# Oplify - Oracle Cloud VM Provisioner
# One attempt per invocation. The GitHub retry runner handles clear rejections.
# Defaults to a read-only preflight; launch requires PROVISION_MODE=launch.
# =============================================================

set -euo pipefail

export OCI_REGION="${OCI_REGION:-ap-hyderabad-1}"
if [[ "$OCI_REGION" != "ap-hyderabad-1" ]]; then
    echo "[ERROR] Only the Hyderabad region (ap-hyderabad-1) is allowed."
    exit 1
fi
PROVISION_MODE="${PROVISION_MODE:-preflight}"
if [[ "$PROVISION_MODE" != "preflight" && "$PROVISION_MODE" != "launch" ]]; then
    echo "[ERROR] PROVISION_MODE must be preflight or launch."
    exit 1
fi

# ---- Validate required env vars ----------------------------
REQUIRED_VARS="OCI_TENANCY OCI_USER OCI_FINGERPRINT OCI_PRIVATE_KEY OCI_REGION \
               OCI_COMPARTMENT_ID OCI_SUBNET_ID \
               VM_SSH_PUBLIC_KEY"

export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::FutureWarning}"

MISSING=0
for VAR in $REQUIRED_VARS; do
    if [[ -z "${!VAR:-}" ]]; then
        echo "[ERROR] Missing required environment variable: $VAR"
        MISSING=$((MISSING+1))
    fi
done
if [[ $MISSING -gt 0 ]]; then
    echo ""
    echo "Set all required GitHub Actions secrets."
    echo "See README.md for the full list."
    exit 1
fi

# ---- Write OCI config from env vars ------------------------
umask 077
WORK_DIR="$(mktemp -d)"
trap 'rm -rf -- "$WORK_DIR"' EXIT
export OCI_CLI_CONFIG_FILE="$WORK_DIR/config"
cat > "$OCI_CLI_CONFIG_FILE" << EOF
[DEFAULT]
user=${OCI_USER}
fingerprint=${OCI_FINGERPRINT}
tenancy=${OCI_TENANCY}
region=${OCI_REGION}
key_file=$WORK_DIR/private_key.pem
EOF

# Accept multiline PEM or a PEM encoded with literal \n separators.
printf '%s\n' "$OCI_PRIVATE_KEY" | sed 's/\\n/\n/g' > "$WORK_DIR/private_key.pem"

# ---- Fixed VM settings -------------------------------------
COMPARTMENT_ID="${OCI_COMPARTMENT_ID}"
SUBNET_ID="${OCI_SUBNET_ID}"
SSH_PUB="${VM_SSH_PUBLIC_KEY}"
SSH_PUB_FILE="$WORK_DIR/ssh-public-key"
printf '%s\n' "$SSH_PUB" > "$SSH_PUB_FILE"
chmod 600 "$SSH_PUB_FILE"

DISPLAY_NAME="${VM_DISPLAY_NAME:-oplify-agent}"
SHAPE="${VM_SHAPE:-VM.Standard.A1.Flex}"
OCPUS="${VM_OCPUS:-2}"
MEMORY_GB="${VM_MEMORY_GB:-12}"
BOOT_VOLUME_GB="${VM_BOOT_VOLUME_GB:-200}"
ASSIGN_PUBLIC_IP="${ASSIGN_PUBLIC_IP:-true}"
FREE_TIER_STRICT="${FREE_TIER_STRICT:-true}"
OCI_IMAGE_OS="${OCI_IMAGE_OS:-Canonical Ubuntu}"
OCI_IMAGE_OS_VERSION="${OCI_IMAGE_OS_VERSION:-22.04}"

# ---- Helpers -----------------------------------------------
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"; }

die() {
    log "ERROR: $1"
    exit 1
}

is_true() {
    [[ "${1,,}" == "true" || "$1" == "1" || "${1,,}" == "yes" ]]
}

validate_number() {
    local name="$1" value="$2"
    if ! [[ "$value" =~ ^(0|[1-9][0-9]*)([.][0-9]+)?$ ]]; then
        die "$name must be a number. Got: $value"
    fi
}

validate_integer() {
    local name="$1" value="$2"
    if ! [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
        die "$name must be an integer. Got: $value"
    fi
}

float_lte() {
    python3 - "$1" "$2" <<'PY'
import sys
print("1" if float(sys.argv[1]) <= float(sys.argv[2]) else "0")
PY
}

# ---- Free-tier guardrails ----------------------------------
validate_integer "VM_OCPUS" "$OCPUS"
validate_number "VM_MEMORY_GB" "$MEMORY_GB"
validate_integer "VM_BOOT_VOLUME_GB" "$BOOT_VOLUME_GB"

is_true "$FREE_TIER_STRICT" || die "Free-tier checks cannot be disabled."
[[ "$SHAPE" == "VM.Standard.A1.Flex" ]] || die "Only the Always Free Ampere A1 shape is allowed."
[[ "$(float_lte "$OCPUS" "2")" == "1" && "$(float_lte "1" "$OCPUS")" == "1" ]] || die "VM_OCPUS must be between 1 and 2."
[[ "$(float_lte "$MEMORY_GB" "12")" == "1" && "$(float_lte "1" "$MEMORY_GB")" == "1" ]] || die "VM_MEMORY_GB must be between 1 and 12."
[[ "$(float_lte "$OCPUS" "$MEMORY_GB")" == "1" ]] || die "Allocate at least 1 GB of memory per OCPU."
[[ "$(float_lte "50" "$BOOT_VOLUME_GB")" == "1" && "$(float_lte "$BOOT_VOLUME_GB" "200")" == "1" ]] || die "Boot volume must be between 50 and 200 GB."
[[ "$ASSIGN_PUBLIC_IP" == "true" || "$ASSIGN_PUBLIC_IP" == "false" ]] || die "ASSIGN_PUBLIC_IP must be true or false."
[[ -n "$DISPLAY_NAME" ]] || die "VM_DISPLAY_NAME cannot be empty."

# The preflight checks the home region, all compartments, subnet, image,
# existing instances and the remaining account-wide resource allowance.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/preflight.py" "$DISPLAY_NAME" "$SHAPE" "$OCPUS" \
    "$MEMORY_GB" "$BOOT_VOLUME_GB" "$OCI_IMAGE_OS" "$OCI_IMAGE_OS_VERSION" \
    "$ASSIGN_PUBLIC_IP" > "$WORK_DIR/preflight.json" || die "OCI preflight failed; no launch was attempted."

read -r EXISTING_STATE AVAILABILITY_DOMAIN IMAGE_ID BOOT_VOLUME_GB < <(
    python3 - "$WORK_DIR/preflight.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print(d["existing_state"] or "NONE", d["availability_domain"], d["image_id"] or "NONE", d["boot_volume_gb"])
PY
)

set_output() {
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
        printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"
    fi
}

if [[ "$EXISTING_STATE" != "NONE" ]]; then
    log "An existing target VM was found in state $EXISTING_STATE. No launch was attempted."
    if [[ "$PROVISION_MODE" == "launch" ]]; then
        set_output pause true
    fi
    exit 0
fi

log "Preflight passed: Hyderabad | $OCPUS OCPUs | ${MEMORY_GB}GB RAM | ${BOOT_VOLUME_GB}GB boot volume."
if [[ "$PROVISION_MODE" == "preflight" ]]; then
    log "Read-only preflight completed. No resources were created."
    exit 0
fi

# ---- Single launch attempt ---------------------------------
log "Attempting to create one Always Free VM in Hyderabad."
LAUNCH_ARGS=(
    compute instance launch
    --compartment-id "$COMPARTMENT_ID"
    --availability-domain "$AVAILABILITY_DOMAIN"
    --display-name "$DISPLAY_NAME"
    --shape "$SHAPE"
    --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY_GB}"
    --subnet-id "$SUBNET_ID"
    --assign-public-ip "$ASSIGN_PUBLIC_IP"
    --source-details "{\"sourceType\":\"image\",\"imageId\":\"$IMAGE_ID\",\"bootVolumeSizeInGBs\":$BOOT_VOLUME_GB,\"bootVolumeVpusPerGB\":10}"
    --ssh-authorized-keys-file "$SSH_PUB_FILE"
    --region ap-hyderabad-1
    --no-retry
    --connection-timeout 10
    --read-timeout 60
)

# Keep uncertain failures paused, including interruption during the request.
set_output pause true
if oci "${LAUNCH_ARGS[@]}" > "$WORK_DIR/launch.json" 2> "$WORK_DIR/launch-error"; then
    if python3 - "$WORK_DIR/launch.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
    if not d.get("id") or d.get("lifecycle-state") not in ("PROVISIONING", "STARTING", "RUNNING"):
        raise ValueError()
except (ValueError, KeyError, TypeError):
    sys.exit(1)
PY
    then
        log "OCI accepted the VM creation request. Confirm RUNNING in the OCI Console."
        exit 0
    fi
    die "Launch response was uncertain. Provisioning is paused; check OCI before resuming."
fi

# Retry only clear rejections. A timeout or server error might follow a launch
# that OCI accepted, so pause those cases instead of risking another VM.
if python3 - "$WORK_DIR/launch-error" <<'PY'
import json, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
    error = json.loads(text[text.index("{"):])
    capacity = error.get("status") == 500 and error.get("code") == "InternalError" and "out of host capacity" in error.get("message", "").lower()
    throttled = error.get("status") == 429 and error.get("code") in ("TooManyRequests", "RateLimitExceeded")
    sys.exit(0 if capacity or throttled else 1)
except (ValueError, TypeError):
    sys.exit(1)
PY
then
    set_output pause false
    log "OCI rejected the request because of capacity or rate limiting. The next scheduled run may retry."
    exit 0
fi
die "OCI launch failed or returned an uncertain result. Provisioning is paused; check OCI before resuming."
