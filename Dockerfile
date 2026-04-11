FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# stdio transport — the MCP host (Claude Code) launches this as a subprocess
CMD ["python", "server.py"]
