FROM python:3.11-slim

WORKDIR /app

# Install gh CLI for knowledge base builder
RUN apt-get update && apt-get install -y curl gnupg libheif-dev libde265-dev && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Store supplemental KB in image at a path not shadowed by the data volume
RUN cp /app/data/supplemental_kb.json /app/supplemental_kb_image.json 2>/dev/null || true

# On container start: sync supplemental KB into volume, then rebuild KB if needed
CMD ["sh", "-c", "\
  if [ -f /app/supplemental_kb_image.json ]; then \
    cp /app/supplemental_kb_image.json /app/data/supplemental_kb.json; \
  fi && \
  if [ ! -f /app/data/knowledge_base.json ]; then \
    echo 'Building knowledge base...' && \
    python /app/backend/build_knowledge_base.py; \
  fi && \
  python /app/backend/app.py \
"]
