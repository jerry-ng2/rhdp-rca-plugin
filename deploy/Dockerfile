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

WORKDIR /workspace

# Install Python dependencies for skills
COPY skills/root-cause-analysis/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Copy skills into the marketplace path expected by the init container
COPY skills/ /workspace/.claude/plugins/marketplaces/aiops-marketplace/skills/

# Also place skills in the plugins/cache path expected by batch_rca_headless.sh
RUN mkdir -p /workspace/.claude/plugins/cache \
    && cp -r /workspace/.claude/plugins/marketplaces/aiops-marketplace/skills/logs-fetcher/scripts/* \
             /workspace/.claude/plugins/cache/

# Copy batch automation scripts
COPY deploy/batch-rca-automation/ /workspace/

RUN mkdir -p /workspace/.config/gcloud /workspace/.claude /workspace/.ssh \
    && touch /workspace/.config/gcloud/application_default_credentials.json \
    && chmod -R g+rwX /workspace \
    && chgrp -R 0 /workspace

ENV HOME=/workspace

ENTRYPOINT ["/bin/bash"]
