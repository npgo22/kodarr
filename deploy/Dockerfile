# Build and runtime python minors MUST match: the venv bakes site-packages
# under lib/python3.X, and a different runtime interpreter silently can't see
# it (ModuleNotFoundError at boot — shipped broken for a day once).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim-bookworm
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" KODARR_CONFIG=/config/config.toml
# boot smoke test: a broken venv/interpreter pairing fails the build, not prod
RUN kodarr --help >/dev/null
USER 568:5000
WORKDIR /config
ENTRYPOINT ["kodarr"]
CMD ["run"]
