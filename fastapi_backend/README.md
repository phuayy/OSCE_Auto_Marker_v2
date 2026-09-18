# FastAPI Backend

The API + pipeline orchestration process. For architecture, directory layout,
the processing pipeline, the job queue, auth, and every environment variable,
see the project root [CLAUDE.md](../CLAUDE.md) — this file only covers running
the backend from here; it does not restate the architecture, since a second
copy is exactly what goes stale (see "CLAUDE.md is stale relative to itself"
in the root doc's own history).

## Run

```bash
uv sync                              # from the project root; deps live in pyproject.toml
uv run uvicorn app.main:app --reload --port 8787
# or: uv run python scripts/run_api.py --reload   (from the project root)
```

Environment variables load from the project root `.env`; copy `.env.example`
to `.env` for local development. Production secrets (`AUTH_SECRET`,
`NVIDIA_API_KEY`, `WHISPERX_HF_TOKEN`, provider keys, …) belong in the hosting
platform's secret manager, not in a committed file.

Production with `JOB_QUEUE_BACKEND=hatchet` also runs a worker process:

```bash
uv run python -m app.queue.hatchet_worker
```

## Tests

```bash
uv run pytest              # from this directory
# or: npm run test:api      # from the project root
```

## Structure

Mirrors the root CLAUDE.md's directory layout (`app/api/routes`, `app/services`,
`app/repositories`, `app/pipeline`, `app/database`, `app/llm`, `app/queue`,
`tests/`). Consult that file for what each module owns — this README does not
duplicate it.
