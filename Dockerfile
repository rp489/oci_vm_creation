FROM python:3.11-slim

# Install system deps
RUN apt-get update && apt-get install -y \
    curl \
    openssh-client \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Install OCI CLI
RUN curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh \
    | bash -s -- --accept-all-defaults \
    && echo 'export PATH="/root/bin:$PATH"' >> /root/.bashrc

ENV PATH="/root/bin:$PATH"

# Copy provisioner script
COPY provision.sh /provision.sh
COPY preflight.py /preflight.py
RUN chmod +x /provision.sh

CMD ["/bin/bash", "/provision.sh"]
