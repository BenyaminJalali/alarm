FROM python:3.11-slim

WORKDIR /app

# Install gh CLI for knowledge base builder
RUN apt-get update && apt-get install -y curl gnupg && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y gh && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build knowledge base on container start if not already present
CMD ["sh", "-c", "\
  if [ ! -f /app/data/knowledge_base.json ]; then \
    echo 'Building knowledge base...' && \
    python /app/backend/build_knowledge_base.py; \
  fi && \
  python /app/backend/app.py \
"]
