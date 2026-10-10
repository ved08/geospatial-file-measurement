import json
from datetime import date

import geopandas as gpd
import pytest
from shapely.geometry import (
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)

from app.core.config import settings
from app.services.processing import ProcessingError, crs_label, read_dataset

SQUARE = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])


def write_shapefile(path, geometries, crs="EPSG:4326", **columns):
    columns = columns or {"name": [f"f{i}" for i in range(len(geometries))]}
    gpd.GeoDataFrame(columns, geometry=geometries, crs=crs).to_file(path)
    return path


def write_kml(path, body):
    path.write_text(f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{body}</Document></kml>')
    return path


def placemark(name, geometry_xml, extended=""):
    return f"<Placemark><name>{name}</name>{extended}{geometry_xml}</Placemark>"


POINT_XML = "<Point><coordinates>77.59,12.97</coordinates></Point>"
LINE_XML = "<LineString><coordinates>77.59,12.97 77.60,12.98</coordinates></LineString>"
POLYGON_XML = (
    "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
    "77.59,12.97 77.60,12.97 77.60,12.98 77.59,12.98 77.59,12.97"
    "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
)


# --- Shapefile -----------------------------------------------------------------


def test_reads_shapefile_features(tmp_path):
    path = write_shapefile(tmp_path / "farms.shp", [SQUARE, SQUARE], name=["A", "B"], acres=[12, 30])
    dataset = read_dataset(path)

    assert crs_label(dataset.crs) == "EPSG:4326"
    assert dataset.layers == ["farms"]
    assert [f.id for f in dataset.features] == [0, 1]
    first = dataset.features[0]
    assert first.geometry_type == "Polygon"
    assert first.geojson["type"] == "Polygon"
    assert first.properties == {"name": "A", "acres": 12}
    assert crs_label(first.crs) == "EPSG:4326"


@pytest.mark.parametrize(
    "geometry",
    [
        Point(77.59, 12.97),
        MultiPoint([(77.59, 12.97), (77.6, 12.98)]),
        LineString([(77.59, 12.97), (77.6, 12.98)]),
        MultiLineString([[(77.59, 12.97), (77.6, 12.98)], [(77.7, 12.9), (77.8, 12.95)]]),
        SQUARE,
        MultiPolygon([SQUARE, Polygon([(78, 13), (78.1, 13), (78.1, 13.1)])]),
    ],
    ids=lambda g: g.geom_type,
)
def test_reads_every_shapefile_geometry_type(tmp_path, geometry):
    dataset = read_dataset(write_shapefile(tmp_path / "data.shp", [geometry]))
    assert dataset.features[0].geometry_type == geometry.geom_type
    assert dataset.features[0].geometry.equals(geometry)


@pytest.mark.parametrize("crs", ["EPSG:4269", "EPSG:32643", "EPSG:3857"])
def test_keeps_the_files_own_crs(tmp_path, crs):
    geometry = gpd.GeoSeries([SQUARE], crs="EPSG:4326").to_crs(crs)[0]
    dataset = read_dataset(write_shapefile(tmp_path / "data.shp", [geometry], crs=crs))
    assert crs_label(dataset.crs) == crs


def test_keeps_z_coordinates(tmp_path):
    polygon_z = Polygon([(77.59, 12.97, 900), (77.6, 12.97, 905), (77.6, 12.98, 910)])
    feature = read_dataset(write_shapefile(tmp_path / "data.shp", [polygon_z])).features[0]
    assert feature.geometry.has_z
    assert feature.geojson["coordinates"][0][0] == (77.59, 12.97, 900)


def test_keeps_features_without_geometry(tmp_path):
    dataset = read_dataset(write_shapefile(tmp_path / "data.shp", [SQUARE, None]))
    missing = dataset.features[1]
    assert missing.geometry is None
    assert missing.geometry_type is None
    assert missing.geojson is None
    assert missing.properties == {"name": "f1"}


def test_shapefile_nulls_are_kept_and_values_are_json_safe(tmp_path):
    path = write_shapefile(
        tmp_path / "data.shp",
        [SQUARE, SQUARE],
        label=["a", None],
        ratio=[0.5, float("nan")],
        surveyed=[date(2024, 1, 15), None],
    )
    features = read_dataset(path).features

    assert features[0].properties == {"label": "a", "ratio": 0.5, "surveyed": "2024-01-15"}
    assert features[1].properties == {"label": None, "ratio": None, "surveyed": None}
    json.dumps([f.properties for f in features], allow_nan=False)


def test_empty_shapefile_has_no_features(tmp_path):
    path = write_shapefile(tmp_path / "data.shp", [SQUARE])
    gpd.read_file(path).iloc[0:0].to_file(path)
    dataset = read_dataset(path)
    assert dataset.features == []
    assert dataset.crs is None


# --- missing CRS -----------------------------------------------------------------


def test_shapefile_without_prj_fails_by_default(tmp_path):
    path = write_shapefile(tmp_path / "data.shp", [SQUARE])
    (tmp_path / "data.prj").unlink()
    with pytest.raises(ProcessingError, match="does not define a coordinate reference system"):
        read_dataset(path)


def test_shapefile_without_prj_uses_configured_default_crs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "default_crs", "EPSG:4326")
    path = write_shapefile(tmp_path / "data.shp", [SQUARE])
    (tmp_path / "data.prj").unlink()

    dataset = read_dataset(path)
    assert crs_label(dataset.crs) == "EPSG:4326"
    assert dataset.warnings == ["No coordinate reference system in the file; assumed EPSG:4326"]


def test_invalid_default_crs_setting_is_rejected():
    with pytest.raises(ValueError, match="Invalid default_crs"):
        type(settings)(default_crs="EPSG:not-a-code")


# --- corrupt files -----------------------------------------------------------------


def test_corrupt_shapefile_raises_processing_error_without_server_path(tmp_path):
    path = write_shapefile(tmp_path / "data.shp", [SQUARE])
    path.write_bytes(b"garbage" * 20)
    with pytest.raises(ProcessingError) as exc_info:
        read_dataset(path)
    assert exc_info.value.message.startswith("Could not read data.shp")
    assert str(tmp_path) not in exc_info.value.message


# --- KML -------------------------------------------------------------------------


def test_reads_kml_features_and_crs(tmp_path):
    path = write_kml(
        tmp_path / "survey.kml",
        placemark("Well", POINT_XML) + placemark("Road", LINE_XML) + placemark("Farm", POLYGON_XML),
    )
    dataset = read_dataset(path)

    assert crs_label(dataset.crs) == "EPSG:4326"
    assert [f.geometry_type for f in dataset.features] == ["Point", "LineString", "Polygon"]
    assert [f.properties["Name"] for f in dataset.features] == ["Well", "Road", "Farm"]


def test_reads_every_kml_folder_including_nested_ones(tmp_path):
    path = write_kml(
        tmp_path / "survey.kml",
        placemark("Loose", POINT_XML)
        + "<Folder><name>Farms</name>" + placemark("Farm", POLYGON_XML)
        + "<Folder><name>Roads</name>" + placemark("Road", LINE_XML) + "</Folder>"
        + "</Folder>",
    )
    dataset = read_dataset(path)

    assert {f.properties["Name"]: f.layer for f in dataset.features} == {
        "Loose": "survey",
        "Farm": "Farms",
        "Road": "Roads",
    }
    assert sorted(f.id for f in dataset.features) == [0, 1, 2]


def test_kml_drops_rendering_fields_and_missing_values(tmp_path):
    extended = (
        "<ExtendedData><Data name=\"owner\"><value>Ravi</value></Data></ExtendedData>"
    )
    path = write_kml(
        tmp_path / "survey.kml",
        placemark("Farm", POLYGON_XML, extended) + placemark("Well", POINT_XML),
    )
    farm, well = read_dataset(path).features

    assert farm.properties == {"Name": "Farm", "owner": "Ravi"}
    # "owner" exists only on the farm placemark, so the well must not get owner=None
    assert well.properties == {"Name": "Well"}


def test_kml_multigeometry_is_kept_as_one_feature(tmp_path):
    path = write_kml(
        tmp_path / "survey.kml",
        placemark("Farm and well", f"<MultiGeometry>{POLYGON_XML}{POINT_XML}</MultiGeometry>"),
    )
    (feature,) = read_dataset(path).features
    assert feature.geometry_type == "GeometryCollection"
    assert [g.geom_type for g in feature.geometry.geoms] == ["Polygon", "Point"]


def test_kml_placemark_without_geometry_is_kept(tmp_path):
    path = write_kml(tmp_path / "survey.kml", placemark("Note only", ""))
    (feature,) = read_dataset(path).features
    assert feature.geometry is None
    assert feature.properties == {"Name": "Note only"}


def test_kml_without_placemarks_has_no_features(tmp_path):
    dataset = read_dataset(write_kml(tmp_path / "empty.kml", ""))
    assert dataset.features == []
    assert dataset.layers == []
