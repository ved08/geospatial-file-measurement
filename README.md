# Geospatial File Measurement API

## Setup

Live demo: https://geospatial-file-measurement-3l4f.onrender.com/docs (Render free tier: the first request after 15 minutes of inactivity can take 1–2 minutes while it wakes up).

To run locally, Docker is required.

```bash
docker compose up --build          # API on http://localhost:8000, Swagger UI at /docs
docker compose down                # stop (add -v to also delete stored files and the database)
docker compose run --rm tests      # run the test suite
```

## API

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/files/` | Upload a `.kml` or a `.zip` containing a Shapefile |
| GET | `/api/files/{id}/` | File information and processing status |
| GET | `/api/files/{id}/measurements/` | Features with their measurements |

The upload is validated straight away and processed in the background. It returns `201` with status `PENDING`, which moves to `PROCESSING` and then `COMPLETED` or `FAILED`. Poll the file info until it is `COMPLETED`, then fetch the measurements.

```bash
curl -F "file=@survey.kml" http://localhost:8000/api/files/
curl http://localhost:8000/api/files/a98996d0376445f39d0563a0a4eef6fd/
```

```json
{
  "id": "a98996d0376445f39d0563a0a4eef6fd",
  "filename": "survey.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "warnings": [],
  "created_at": "2026-10-10T17:27:40.248264Z",
  "processed_at": "2026-10-10T17:27:40.320350Z"
}
```

If processing fails, `status` is `FAILED` and `error` explains why, e.g. a shapefile without a `.prj`.

```bash
curl http://localhost:8000/api/files/a98996d0376445f39d0563a0a4eef6fd/measurements/
```

```json
{
  "file_id": "a98996d0376445f39d0563a0a4eef6fd",
  "crs": "EPSG:4326",
  "total": 3,
  "offset": 0,
  "limit": null,
  "summary": {
    "geometry_types": { "Polygon": 1, "LineString": 1, "Point": 1 },
    "without_geometry": 0,
    "measured": 2,
    "total_area_m2": 1201683.92,
    "total_length_m": 2437.08
  },
  "features": [
    {
      "id": 0,
      "layer": "survey",
      "geometry_type": "Polygon",
      "geometry": { "type": "Polygon", "coordinates": [[[77.59, 12.97], [77.6, 12.97], [77.6, 12.98], [77.59, 12.98], [77.59, 12.97]]] },
      "crs": "EPSG:4326",
      "properties": { "Name": "Farm" },
      "area_m2": 1201683.92,
      "perimeter_m": 4385.06,
      "length_m": null,
      "measurement_crs": "EPSG:32643",
      "method": "projected",
      "note": null
    }
  ]
}
```

The other two features in this file are a LineString (`length_m: 2437.08`) and a Point (all measurements `null`, `note: "No measurement for Point geometries"`). `geometry` is GeoJSON in the file's original CRS; `measurement_crs` and `method` (`projected` or `geodesic`) show how the feature was measured. Paging is optional: `?offset=0&limit=100`.

| Code | When |
|---|---|
| 201 | Upload accepted |
| 400 | Invalid file: malformed KML, corrupt zip, missing `.shp`/`.shx`/`.dbf`, empty file |
| 404 | Unknown file id |
| 409 | Measurements requested while the file is still processing, or after it failed |
| 413 | File over 50 MB, or a zip that extracts to over 500 MB |
| 415 | Extension is not `.kml` or `.zip` |
| 422 | Missing `file` field, or invalid `offset`/`limit` |

## Architecture

### Application structure

```
app/
├── main.py              app setup; creates tables, recovers interrupted jobs on startup
├── core/config.py       settings (GEO_* environment variables)
├── db.py, models.py     SQLAlchemy engine and the files/features tables
├── api/middleware.py    rejects oversized requests before the body is read
├── api/routes/files.py  the three endpoints
├── schemas/files.py     response models
└── services/
    ├── validation.py    file type, KML and zip checks, shapefile extraction
    ├── storage.py       saves the upload with a size limit
    ├── uploads.py       save + validate, clean up on failure
    ├── processing.py    reads features from the file
    ├── measurement.py   CRS selection and area/length calculation
    ├── pipeline.py      runs processing and measurement, updates the status
    └── repository.py    database access
```

Routes only handle HTTP; the services have no FastAPI code, so the pipeline could run in a separate worker unchanged. Uploads are kept under `uploads/<id>/` and results in SQLite. In Docker both are on volumes, so they survive restarts.

### File-processing flow

1. A middleware rejects bodies over the size limit (413) before they are fully received.
2. The file is saved and validated in a worker thread, so other requests aren't blocked. KML must be well-formed XML with a `<kml>` root (parsed with `defusedxml`). A zip must be valid, unencrypted, free of `../` paths and within size limits, and contain exactly one `.shp` with its `.shx` and `.dbf`; only those files are extracted.
3. A `PENDING` record is saved and `201` is returned.
4. A background task reads every layer with GDAL (pyogrio; each KML folder is a separate layer), keeps each feature's id, geometry, CRS and properties, measures it, saves the results and sets `COMPLETED`. Properties are converted to plain JSON, and Google Earth display fields are dropped from KML.
5. An unreadable file or missing CRS sets `FAILED` with a clear error. Unexpected errors set `FAILED` with a generic message and are logged, and files interrupted by a restart are marked `FAILED` on startup.

### Measurement calculation flow

For each feature: Z values are dropped, invalid geometry (e.g. a self-intersecting polygon) is repaired with `shapely.make_valid`, long edges are densified so their shape survives reprojection, and the geometry is reprojected (see CRS handling). Then:

- Polygon / MultiPolygon: area and perimeter
- LineString / MultiLineString: length
- GeometryCollection: the sum of its polygon areas and line lengths
- Point / MultiPoint, missing or empty geometry: no measurement, with a note

A problem with one feature gives that feature a note; it never fails the whole file.

### CRS handling

The source CRS comes from the shapefile's `.prj`; KML is always EPSG:4326. A shapefile without a `.prj` fails rather than being guessed (set `GEO_DEFAULT_CRS=EPSG:4326` to assume a CRS instead).

Every feature is measured in the UTM zone of its centroid (zone = floor((lon + 180) / 6) + 1, EPSG:326xx north, 327xx south), whatever the source CRS. Already-projected files are reprojected too, because some projected CRSs are bad for measuring: Web Mercator inflates areas, and some CRSs use feet.

UTM is only accurate near the centre of its zone, so features are measured geodesically on the WGS84 ellipsoid (`pyproj.Geod`) when:
- any vertex is more than ~637 km from the zone's central meridian, where UTM's error reaches ~0.5% in length and ~0.9% in area;
- they cross the 180° meridian (e.g. Fiji, whose centroid falls near 0°, in the wrong zone);
- they extend beyond UTM's range of 80°S to 84°N.

## Design Decisions

- **FastAPI over Django + DRF.** Three endpoints don't need Django's admin site or ORM. FastAPI is lighter, validates with Pydantic and generates the API docs.
- **Local UTM zone per feature.** One CRS for the whole file breaks for large files such as all US states. An equal-area projection distorts length. Geodesic everywhere is the most accurate, but the assignment asks for a projected CRS. So UTM is the main method, with geodesic only where UTM isn't accurate enough.
- **Background processing.** Large files take time, so the upload returns immediately and the `status` field reports progress. FastAPI `BackgroundTasks` keeps the setup simple compared with Celery or RQ with Redis. The pipeline doesn't depend on FastAPI, so switching to a queue is a one-line change. The cost is that tasks are lost on a restart, which is why such files are marked `FAILED` on startup.
- **409 when measurements aren't ready.** A 200 with an empty list would look like a file with no features. 409 says the file exists but isn't ready, and the message says why.
- **Fail on a missing `.prj`.** Assuming EPSG:4326 is friendlier but silently wrong for projected data, so assuming a CRS is opt-in.
- **SQLite through SQLAlchemy.** No setup is needed, and Postgres only needs a different `GEO_DATABASE_URL`. Properties and geometry are JSON columns because every file has different attributes. PostGIS isn't used because there are no spatial queries.
- **Keep the original geometry.** Features are stored and returned in their own CRS; only a copy is reprojected for measuring.

## Learnings

**Degrees are not a unit of distance.** A degree of latitude is always about 111 km, but a degree of longitude shrinks with cos(latitude), down to zero at the poles. So the same 0.01° × 0.01° square covers very different areas:

| Location | East–west | North–south | Real area |
|---|---|---|---|
| Bangalore (13°N) | 1,085 m | 1,106 m | 1,200,290 m² |
| Oslo (60°N) | 560 m | 1,114 m | 623,267 m² |

Measured in degrees, both are 0.0001 "square degrees", which hides that Oslo's square is half the size. Using a fixed 111 km per degree gives 1,239,214 m² for both: 3% too high in Bangalore and 99% too high in Oslo. The error depends on where the data is, which makes it easy to miss. Reprojected to their UTM zones, this service gives 1,201,684 m² and 622,907 m², both within 0.12% of the geodesic value.

**Projected doesn't mean accurate.** Web Mercator (EPSG:3857), used by most web maps, is in metres but stretches distances by 1/cos(latitude). The same squares come out 6% too large in Bangalore and almost 4 times too large in Oslo, which is why projected files are reprojected too.

**UTM has limits too.** Wide features, features crossing the 180° meridian and polar regions need geodesic measurement. Reprojecting only the corners of long edges also loses accuracy, because a straight line in one CRS is a curve in another. Densifying edges reduced the error on a 20° × 10° box from 0.5% to under 0.05%.

**The CRS has to be known.** A `.shp` stores only numbers; whether they are degrees, metres or feet is in the `.prj`. Without it, the same numbers could be places thousands of kilometres apart.

**Real data catches what tests miss.** Inland US states matched official Census areas within 0.1%. Coastal states looked far off until it turned out the official figures include water that the boundary file doesn't. Google's KML sample showed that reading only the default layer drops most features.

**Blocking code in async routes stalls the server.** Zip extraction inside an `async` route made a health check take 1.08 s instead of 0.06 s; a thread pool brought it to 0.014 s. FastAPI also reads the whole upload before the route runs, so the size limit had to be in middleware.

## Future Scope

- Separate worker processes (Redis with RQ or Celery) for retries and durability
- Object storage (S3) for uploads, and Postgres with Alembic migrations
- KMZ, GeoJSON, GeoPackage, and zips with several shapefiles
- Correcting UTM's scale error near zone edges
- Authentication and rate limiting
- PostGIS, if spatial queries are needed
- Processing very large files in chunks, with keyset pagination
