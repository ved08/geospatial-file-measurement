# Geospatial File Measurement API

A FastAPI service that accepts a Shapefile (`.zip`) or `.kml` file, extracts its
features, and returns area/length measurements computed in a projected CRS.

> Status: project skeleton — parsing and measurement are not implemented yet.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Interactive API docs: http://127.0.0.1:8000/docs

Run tests:

```bash
pytest
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/files/` | Upload a `.zip` (Shapefile) or `.kml` file |
| `GET` | `/api/files/{id}/` | File info and processing status |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements |

## Project structure

```
app/
├── main.py              # FastAPI app factory + health check
├── core/config.py       # Settings (env vars prefixed GEO_)
├── api/
│   ├── router.py        # Mounts all routers under /api
│   └── routes/files.py  # File upload / info / measurements endpoints
├── schemas/files.py     # Pydantic request/response models
└── services/
    ├── storage.py       # Saves uploads to disk
    ├── repository.py    # File + measurement records (in-memory for now)
    ├── processing.py    # Reads features from .zip/.kml (TODO)
    └── measurement.py   # CRS reprojection + area/length (TODO)
tests/                   # pytest suite
```

## Architecture

_TODO_

## Design decisions

_TODO_

## Learnings & future scope

_TODO_
