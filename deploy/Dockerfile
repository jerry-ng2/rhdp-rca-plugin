FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    python3 \
    python3-venv \
    python3-pip \
    openssh-client \
    rsync \
    jq \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Install Python dependencies for skills and batch automation
COPY skills/root-cause-analysis/requirements.txt /tmp/skill-requirements.txt
COPY deploy/batch-rca-automation/requirements.txt /tmp/batch-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    -r /tmp/skill-requirements.txt \
    -r /tmp/batch-requirements.txt \
    "mlflow[genai]>=3.4" \
    && rm /tmp/skill-requirements.txt /tmp/batch-requirements.txt

# Stage files at /opt/app so PVC mount at /workspace cannot shadow them.
# The init container copies these onto the PVC at runtime.
COPY skills/ /opt/app/skills/
COPY deploy/batch-rca-automation/ /opt/app/batch/

RUN mkdir -p /opt/app/plugins-cache \
    && cp -r /opt/app/skills/logs-fetcher/scripts/* /opt/app/plugins-cache/ \
    && chmod -R g+rwX /opt/app \
    && chgrp -R 0 /opt/app

WORKDIR /workspace

RUN mkdir -p /workspace/.config/gcloud /workspace/.claude /workspace/.ssh \
    && touch /workspace/.config/gcloud/application_default_credentials.json \
    && chmod -R g+rwX /workspace \
    && chgrp -R 0 /workspace

ENV HOME=/workspace

ENTRYPOINT ["/bin/bash"]
