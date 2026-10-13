FROM node:24.18.0-bookworm-slim
RUN apt-get update && apt-get install --yes --no-install-recommends python3 python3-venv git gh tmux tini ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/panopticon
ENV PATH="/opt/panopticon/bin:${PATH}"
WORKDIR /opt/source
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . 'uvicorn[standard]' && npm install --global --ignore-scripts @ai-outfitter/outfitter@1.18.0
RUN mkdir /workspace && chown node:node /workspace
USER node
ENV HOME=/workspace
ENV OUTFITTER_TELEMETRY=0
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "panopticon.container.workspace"]
