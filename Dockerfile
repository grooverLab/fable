# fable — MCP server image
#
# Lets Glama (and anyone) run fable's MCP server in a sandbox to verify it
# starts and answers introspection (initialize / tools/list). fable is
# stdlib-only Python, so the image is tiny: install the package, launch the
# stdio MCP server, done.
#
#   docker build -t fable-mcp .
#   docker run -i --rm fable-mcp        # speaks MCP JSON-RPC over stdin/stdout
#
# initialize + tools/list need no database; tools/call uses ~/.fable/fable.db
# at runtime if present (mount it to enable search/recall).
FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

ENV HOME=/root
ENTRYPOINT ["fable", "mcp"]
