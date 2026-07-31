FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

# In-network service mode: MCP over streamable HTTP on 8000,
# human approvals page on 8765.
ENV APPROVALS_HOST=0.0.0.0 MCP_HOST=0.0.0.0
EXPOSE 8000 8765

# Debug + audit logs. Mount a volume here: the audit trail is the record of
# what was ordered and must outlive the container.
ENV STORELINK_LOG_DIR=/app/logs
VOLUME ["/app/logs"]

CMD ["python", "-m", "src.main", "--transport", "http"]
