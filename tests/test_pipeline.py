import io
import json
import math
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from app.schemas.files import FileInfo, FileStatus
from app.services import pipeline
from app.services.pipeline import INTERNAL_ERROR_MESSAGE, process_file

SQUARE = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])
SQUARE_AREA_M2 = 1_201_684  # 0.01 x 0.01 degrees at 12.97N

POINT_XML = "<Point><coordinates>77.59,12.97</coordinates></Point>"
LINE_XML = "<LineString><coordinates>77.59,12.97 77.60,12.98</coordinates></LineString>"
POLYGON_XML = (
    "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
    "77.59,12.97 77.60,12.97 77.60,12.98 77.59,12.98 77.59,12.97"
    "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
)
SURVEY_KML = f"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>Well</name>{POINT_XML}</Placemark>
<Placemark><name>Road</name>{LINE_XML}</Placemark>
<Placemark><name>Farm</name>{POLYGON_XML}</Placemark>
<Placemark><name>Mixed</name><MultiGeometry>{POINT_XML}{POLYGON_XML}</MultiGeometry></Placemark>
</Document></kml>""".encode()


def upload(client, filename, content):
    response = client.post("/api/files/", files={"file": (filename, content)})
    assert response.status_code == 201
    assert response.json()["status"] == "PENDING"
    return response.json()["id"]


def shapefile_zip(tmp_path, geometries, crs="EPSG:4326", include_prj=True, **columns):
    folder = tmp_path / "shp"
    folder.mkdir()
    columns = columns or {"name": [f"f{i}" for i in range(len(geometries))]}
    gpd.GeoDataFrame(columns, geometry=geometries, crs=crs).to_file(folder / "farms.shp")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in folder.iterdir():
            if include_prj or path.suffix != ".prj":
                archive.write(path, path.name)
    return buffer.getvalue()


def measurements(client, file_id, **params):
    response = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert response.status_code == 200, response.text
    # Strict JSON: no NaN/Infinity anywhere in the body.
    return json.loads(response.text, parse_constant=lambda c: pytest.fail(f"non-JSON {c}"))


def add_pending(repository, file_id="f1"):
    repository.add(
        FileInfo(id=file_id, filename="x.kml", status=FileStatus.PENDING, created_at=datetime.now(timezone.utc))
    )
    return file_id


# --- end to end ----------------------------------------------------------------


def test_kml_end_to_end(client):
    file_id = upload(client, "survey.kml", SURVEY_KML)

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "COMPLETED"
    assert info["feature_count"] == 4
    assert info["crs"] == "EPSG:4326"
    assert info["error"] is None
    assert info["processed_at"] is not None

    body = measurements(client, file_id)
    assert body["total"] == 4 and body["offset"] == 0 and body["limit"] is None
    point, line, polygon, collection = body["features"]

    assert point["geometry_type"] == "Point"
    assert point["geometry"]["type"] == "Point"
    assert point["crs"] == "EPSG:4326"
    assert point["properties"]["Name"] == "Well"
    assert point["area_m2"] is None and point["length_m"] is None
    assert point["note"] == "No measurement for Point geometries"

    assert line["geometry_type"] == "LineString"
    assert line["length_m"] == pytest.approx(1_543, rel=0.005)
    assert line["area_m2"] is None

    assert polygon["geometry_type"] == "Polygon"
    assert polygon["area_m2"] == pytest.approx(SQUARE_AREA_M2, rel=0.001)
    assert polygon["perimeter_m"] == pytest.approx(4_385, rel=0.005)
    assert polygon["measurement_crs"] == "EPSG:32643"
    assert polygon["method"] == "projected"

    assert collection["geometry_type"] == "GeometryCollection"
    assert collection["area_m2"] == pytest.approx(SQUARE_AREA_M2, rel=0.001)

    summary = body["summary"]
    assert summary["geometry_types"] == {
        "Point": 1, "LineString": 1, "Polygon": 1, "GeometryCollection": 1
    }
    assert summary["measured"] == 3
    assert summary["without_geometry"] == 0
    assert summary["total_area_m2"] == pytest.approx(2 * SQUARE_AREA_M2, rel=0.001)
    assert summary["total_length_m"] == pytest.approx(line["length_m"])


def test_shapefile_zip_end_to_end(client, tmp_path):
    content = shapefile_zip(tmp_path, [SQUARE, None], name=["A", "B"], acres=[297, 0])
    file_id = upload(client, "farms.zip", content)

    info = client.get(f"/api/files/{file_id}/").json()
    assert (info["status"], info["feature_count"], info["crs"]) == ("COMPLETED", 2, "EPSG:4326")

    square, empty = measurements(client, file_id)["features"]
    assert square["layer"] == "farms"
    assert square["properties"] == {"name": "A", "acres": 297}
    assert square["geometry"]["type"] == "Polygon"
    assert square["area_m2"] == pytest.approx(SQUARE_AREA_M2, rel=0.001)
    assert square["perimeter_m"] == pytest.approx(4_385, rel=0.005)
    assert empty["geometry"] is None and empty["note"] == "Feature has no geometry"


def test_line_shapefile_is_measured(client, tmp_path):
    line = LineString([(77.59, 12.97), (77.6, 12.98)])
    file_id = upload(client, "roads.zip", shapefile_zip(tmp_path, [line]))
    feature = measurements(client, file_id)["features"][0]
    assert feature["geometry_type"] == "LineString"
    assert feature["length_m"] == pytest.approx(1_543, rel=0.005)
    assert feature["area_m2"] is None


def test_projected_shapefile_is_measured_in_metres(client, tmp_path):
    square_3857 = gpd.GeoSeries([SQUARE], crs="EPSG:4326").to_crs("EPSG:3857")[0]
    file_id = upload(client, "farms.zip", shapefile_zip(tmp_path, [square_3857], crs="EPSG:3857"))

    assert client.get(f"/api/files/{file_id}/").json()["crs"] == "EPSG:3857"
    feature = measurements(client, file_id)["features"][0]
    assert feature["crs"] == "EPSG:3857"
    # Not the (inflated) Web Mercator planar area.
    assert feature["area_m2"] == pytest.approx(SQUARE_AREA_M2, rel=0.001)


def test_shapefile_without_prj_fails(client, tmp_path):
    file_id = upload(client, "farms.zip", shapefile_zip(tmp_path, [SQUARE], include_prj=False))

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED"
    assert "does not define a coordinate reference system" in info["error"]
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    assert "does not define a coordinate reference system" in response.json()["detail"]


def test_corrupt_shp_in_valid_zip_fails(client):
    entries = {"farms.shp": b"garbage", "farms.shx": b"garbage", "farms.dbf": b"garbage"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    file_id = upload(client, "farms.zip", buffer.getvalue())

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED"
    assert info["error"].startswith("Could not read farms.shp")
    assert "/" not in info["error"]  # no server paths leak
    assert client.get(f"/api/files/{file_id}/measurements/").status_code == 409


def test_unexpected_error_marks_file_failed(client, monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "read_dataset", broken)
    file_id = upload(client, "survey.kml", SURVEY_KML)

    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED"
    assert info["error"] == INTERNAL_ERROR_MESSAGE  # details stay in the log
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    assert INTERNAL_ERROR_MESSAGE in response.json()["detail"]


@pytest.mark.parametrize("status", [FileStatus.PENDING, FileStatus.PROCESSING])
def test_measurements_409_while_not_processed(client, repository, status):
    file_id = add_pending(repository, "not-ready-" + status.value)
    repository.update(file_id, status=status)
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    assert response.json()["detail"] == f"File is still being processed (status: {status.value})"


def test_pagination(client, tmp_path):
    points = [Point(77.59 + i * 0.001, 12.97) for i in range(6)]
    file_id = upload(client, "pts.zip", shapefile_zip(tmp_path, points))

    page = measurements(client, file_id, offset=2, limit=2)
    assert (page["total"], page["offset"], page["limit"]) == (6, 2, 2)
    assert [f["id"] for f in page["features"]] == [2, 3]
    # The summary covers the whole file, not the page.
    assert page["summary"]["geometry_types"] == {"Point": 6}
    assert page["summary"]["measured"] == 0
    assert page["summary"]["total_area_m2"] == 0

    assert [f["id"] for f in measurements(client, file_id, offset=4)["features"]] == [4, 5]
    assert measurements(client, file_id, offset=10)["features"] == []


@pytest.mark.parametrize("params", [{"offset": -1}, {"limit": 0}, {"limit": 10_001}])
def test_pagination_rejects_invalid_params(client, params):
    file_id = upload(client, "survey.kml", SURVEY_KML)
    assert client.get(f"/api/files/{file_id}/measurements/", params=params).status_code == 422


# --- process_file directly -----------------------------------------------------


def test_process_file_stores_results(tmp_path, repository):
    path = tmp_path / "survey.kml"
    path.write_bytes(SURVEY_KML)
    file_id = add_pending(repository, "direct-ok")

    process_file(file_id, path)

    info = repository.get(file_id)
    assert info.status is FileStatus.COMPLETED
    assert (info.feature_count, info.crs) == (4, "EPSG:4326")
    assert repository.count_measurements(file_id) == 4
    assert [f.id for f in repository.get_measurements(file_id, 1, 2)] == [1, 2]
    assert repository.get_summary(file_id).measured == 3


def test_process_file_records_processing_error(tmp_path, repository):
    file_id = add_pending(repository, "direct-missing")
    process_file(file_id, tmp_path / "missing.kml")
    info = repository.get(file_id)
    assert info.status is FileStatus.FAILED
    assert info.error.startswith("Could not read missing.kml")
    assert repository.count_measurements(file_id) == 0


def test_process_file_sets_processing_while_running(tmp_path, repository, monkeypatch):
    file_id = add_pending(repository, "direct-status")
    seen = []

    def spy(path: Path):
        seen.append(repository.get(file_id).status)
        raise RuntimeError("stop")

    monkeypatch.setattr(pipeline, "read_dataset", spy)
    process_file(file_id, tmp_path / "x.kml")
    assert seen == [FileStatus.PROCESSING]
    assert repository.get(file_id).status is FileStatus.FAILED


def test_non_finite_coordinates_are_omitted(tmp_path, repository, monkeypatch):
    from app.services.processing import ParsedDataset, ParsedFeature
    from pyproj import CRS

    crs = CRS.from_epsg(4326)
    bad = ParsedFeature(id=0, layer="l", geometry=Point(float("nan"), 1), crs=crs, properties={})
    monkeypatch.setattr(
        pipeline, "read_dataset", lambda path: ParsedDataset(crs=crs, layers=["l"], features=[bad])
    )
    file_id = add_pending(repository, "direct-nan")
    process_file(file_id, tmp_path / "x.kml")

    feature = repository.get_measurements(file_id)[0]
    assert feature.geometry is None
    assert "non-finite coordinates" in feature.note
    json.dumps(feature.model_dump(mode="json"), allow_nan=False)
    assert math.isfinite(repository.get_summary(file_id).total_area_m2)
