FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \ 
    && apt-get install -y --no-install-recommends git \ 
    && rm -rf /var/lib/apt/lists/*

# Clone the TikTok Ads MCP server source
RUN git clone --depth 1 https://github.com/AdsMCP/tiktok-ads-mcp-server.git /app/src

WORKDIR /app/src

# Install dependencies (six required at runtime)
RUN pip install --no-cache-dir --upgrade pip \ 
    && pip install --no-cache-dir six \ 
    && pip install --no-cache-dir -e .

EXPOSE 8001

ENV HOST=0.0.0.0 \
    PORT=8001

CMD ["python", "run_server.py"]
