# AI Job Coach Next

An independent rewrite of AI Job Coach for preparing a job-specific resume, a skills blueprint, supporting guides, and exports from user-selected material.

The V2 workbench takes one target job description and optional source material. It lets you confirm facts, generate a job resume and a learning blueprint with their own interview guides, review revision history, and download drafts.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Node.js for development checks
- Docker Compose is optional

## Try the V2 workbench

```sh
uv sync --locked --python 3.12
uv run --locked --no-sync python scripts/start_v2_preview.py
```

Open [the local workbench](http://127.0.0.1:18849/v2). The preview starts its own background worker, uses an isolated SQLite database under `data/v2-preview/`, and runs a local demo model that requires no API key. Use fictional material when trying the workflow.

V2 supports draft HTML and DOCX exports. V2 PDF and the PDF-inclusive bundle require the pinned macOS LibreOffice runtime used by the preview. The bundled CJK font carries its own license.

## Run the original workbench

```sh
uv sync --locked --python 3.12
uv run --locked --no-sync alembic upgrade head
uv run --locked --no-sync uvicorn app.api:app --host 127.0.0.1 --port 8827
```

The local provider is the default and needs no external API key. The development server binds to loopback. Start the background worker in a second terminal when testing asynchronous work:

```sh
uv run --locked --no-sync python -m app.worker
```

For the personal Docker installation of the original workbench, run `./scripts/coach.sh init` followed by `./scripts/coach.sh up`. For an existing installation, stop its writers and back up its database and deletion journal before applying migrations. The upgrade reconciles both historical layouts with Alembic revision `0004`.

## Checks

Run the offline development checks with:

```sh
uv run --locked --no-sync python scripts/check_dev.py
```

The check validates repository boundaries, the dependency lock, unit tests, browser JavaScript syntax, and SQLite migrations. It does not establish real-model quality, production security, or readiness for real applicant data.

## Privacy and configuration

Use only documents you are authorized to process. Keep real resumes, generated exports, databases, environment files, and service credentials out of version control. See `.env.example` for environment variable names when configuring an external provider.

This project is an independent rewrite. The older `ai-job-coach` project and its history are not part of this repository.

## License

The root MIT license covers project code authored for this repository. See [Content and rights](CONTENT-NOTICES.md) for the separate font license and content terms.
