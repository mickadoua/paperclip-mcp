FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY paperclip_mcp ./paperclip_mcp
RUN pip install --no-cache-dir .
COPY policy.yaml ./
EXPOSE 9200
CMD ["paperclip-mcp"]
