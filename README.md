# Geospatial File Measurement API

A FastAPI service that accepts a Shapefile (`.zip`) or `.kml` file, extracts its
features, and returns area/length measurements computed in a projected CRS.

> Status: project skeleton — parsing and measurement are not implemented yet.

## Setup

### Option A: Docker (recommended)

Only Docker is needed; GDAL/PROJ come bundled in the image.

```bash
docker compose up --build          # API on http://127.0.0.1:8000
docker compose run --rm tests      # run the test suite in a container
docker compose down                # stop (uploads persist in the `uploads` volume)
```

### Option B: Local Python (tested on 3.13)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # geo wheels bundle GDAL/PROJ, no system libs needed
uvicorn app.main:app --reload
pytest
```

Interactive API docs: http://127.0.0.1:8000/docs

### Configuration

Environment variables (prefix `GEO_`):

| Variable | Default | Meaning |
|---|---|---|
| `GEO_UPLOAD_DIR` | `uploads` | Where uploaded files are stored |
| `GEO_MAX_UPLOAD_SIZE_MB` | `50` | Maximum upload size |
| `GEO_MAX_EXTRACTED_SIZE_MB` | `500` | Maximum total size of a zip's contents |
| `GEO_MAX_ZIP_ENTRIES` | `1000` | Maximum number of entries in a zip |
| `GEO_DEFAULT_CRS` | unset | CRS to assume for files that don't define one (e.g. a shapefile without `.prj`). Unset means such files fail. |

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/files/` | Upload a `.zip` (Shapefile) or `.kml` file |
| `GET` | `/api/files/{id}/` | File info and processing status |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements |

### Upload validation

Uploads are validated before any geospatial parsing; rejected uploads are
deleted and return `{"detail": "<reason>"}`.

| Check | Status |
|---|---|
| Extension is not `.zip` / `.kml` | `415` |
| File larger than `GEO_MAX_UPLOAD_SIZE_MB` (default 50) — rejected by middleware as the body arrives, before it is fully received | `413` |
| Empty file | `400` |
| KML is not well-formed XML, has no `<kml>` root, or declares entities (XXE / "billion laughs") | `400` |
| Zip is corrupt, encrypted, or contains `..`/absolute paths | `400` |
| Zip has more than `GEO_MAX_ZIP_ENTRIES` entries | `400` |
| Zip extracts to more than `GEO_MAX_EXTRACTED_SIZE_MB` (zip bomb) | `413` |
| Zip has no `.shp`, more than one `.shp`, or the `.shp` lacks a `.shx`/`.dbf` in the same folder | `400` |

Saving and validation run in a worker thread so a large upload never blocks other requests.
A missing `.prj` is allowed at upload time (the CRS is handled during processing).
`__MACOSX/` and hidden files are ignored, and shapefiles may sit in a subfolder.

## Project structure

```
app/
├── main.py              # FastAPI app factory + health check
├── core/config.py       # Settings (env vars prefixed GEO_)
├── api/
│   ├── middleware.py    # Rejects oversized request bodies early
│   ├── router.py        # Mounts all routers under /api
│   └── routes/files.py  # File upload / info / measurements endpoints
├── schemas/files.py     # Pydantic request/response models
└── services/
    ├── uploads.py       # Save + validate an upload, clean up on failure
    ├── storage.py       # Copies uploads to disk with a size limit
    ├── validation.py    # File-type, KML and shapefile-zip validation
    ├── repository.py    # File + measurement records (in-memory for now)
    ├── processing.py    # Reads all layers/features from a .kml or .shp
    └── measurement.py   # CRS reprojection + area/length (TODO)
tests/                   # pytest suite
```

## Architecture

_TODO_

## Design decisions

_TODO_

## Learnings & future scope

_TODO_
