import math

import numpy as np
import pytest
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.geometry.polygon import orient

from app.services.measurement import (
    GEODESIC_LABEL,
    METHOD_GEODESIC,
    METHOD_PROJECTED,
    Measurement,
    measure_feature,
)

WGS84 = CRS.from_epsg(4326)
GEOD = Geod(ellps="WGS84")

# ~1.11 km x 1.08 km near Bangalore (UTM zone 43N).
SQUARE = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])
LINE = LineString([(77.59, 12.97), (77.60, 12.98)])

# UTM scale error is up to ~0.1% in length (~0.2% in area) at a zone's edge.
UTM_TOLERANCE = 2e-3


def reproject(geometry, crs):
    transformer = Transformer.from_crs(WGS84, crs, always_xy=True)
    return shapely.transform(geometry, lambda c: np.column_stack(transformer.transform(c[:, 0], c[:, 1])))


def geodesic_area(polygon):
    return abs(GEOD.geometry_area_perimeter(orient(polygon))[0])


def assert_finite(measurement: Measurement):
    for value in (measurement.area_m2, measurement.perimeter_m, measurement.length_m):
        assert value is None or math.isfinite(value)


# --- projected (UTM) measurements ------------------------------------------------


def test_polygon_in_degrees_is_measured_in_metres():
    result = measure_feature(SQUARE, WGS84)

    assert result.method == METHOD_PROJECTED
    assert result.measurement_crs == "EPSG:32643"
    assert result.area_m2 == pytest.approx(geodesic_area(SQUARE), rel=UTM_TOLERANCE)
    assert result.perimeter_m == pytest.approx(GEOD.geometry_area_perimeter(SQUARE)[1], rel=UTM_TOLERANCE)
    assert result.area_m2 > 1_000_000  # nowhere near the naive 0.0001 "square degrees"
    assert result.length_m is None
    assert result.note is None


@pytest.mark.parametrize("crs", ["EPSG:4269", "EPSG:3857", "EPSG:32643", "EPSG:24383"])
def test_same_polygon_in_any_crs_gives_the_same_area(crs):
    expected = measure_feature(SQUARE, WGS84).area_m2
    result = measure_feature(reproject(SQUARE, crs), CRS(crs))
    assert result.measurement_crs == "EPSG:32643"
    assert result.area_m2 == pytest.approx(expected, rel=1e-4)


def test_web_mercator_area_is_not_inflated():
    square = box(10.0, 60.0, 10.02, 60.01)  # near Oslo, where Mercator area is ~4x too big
    in_mercator = reproject(square, "EPSG:3857")
    result = measure_feature(in_mercator, CRS("EPSG:3857"))

    assert in_mercator.area / result.area_m2 == pytest.approx(4, rel=0.01)
    assert result.area_m2 == pytest.approx(geodesic_area(square), rel=UTM_TOLERANCE)


def test_projected_crs_in_us_feet_is_converted_to_metres():
    square = box(-73.99, 40.75, -73.98, 40.76)  # Manhattan
    in_feet = reproject(square, "EPSG:2263")  # NY State Plane, US survey feet
    result = measure_feature(in_feet, CRS("EPSG:2263"))

    assert result.measurement_crs == "EPSG:32618"
    assert result.area_m2 == pytest.approx(geodesic_area(square), rel=UTM_TOLERANCE)
    assert in_feet.area / result.area_m2 == pytest.approx(10.76, rel=0.01)  # ft² per m²


def test_line_length():
    result = measure_feature(LINE, WGS84)
    assert result.method == METHOD_PROJECTED
    assert result.length_m == pytest.approx(GEOD.geometry_length(LINE), rel=UTM_TOLERANCE)
    assert result.area_m2 is None and result.perimeter_m is None


def test_multilinestring_lengths_are_summed():
    other = LineString([(77.61, 12.97), (77.62, 12.97)])
    result = measure_feature(MultiLineString([LINE, other]), WGS84)
    expected = GEOD.geometry_length(LINE) + GEOD.geometry_length(other)
    assert result.length_m == pytest.approx(expected, rel=UTM_TOLERANCE)


def test_polygon_hole_is_subtracted():
    hole = box(77.593, 12.973, 77.597, 12.977)
    with_hole = Polygon(SQUARE.exterior, [hole.exterior])
    result = measure_feature(with_hole, WGS84)

    expected = geodesic_area(SQUARE) - geodesic_area(hole)
    assert result.area_m2 == pytest.approx(expected, rel=UTM_TOLERANCE)
    # The perimeter includes the hole's edge.
    assert result.perimeter_m > measure_feature(SQUARE, WGS84).perimeter_m


def test_overlapping_multipolygon_parts_are_counted_once():
    a = box(77.59, 12.97, 77.60, 12.98)
    b = box(77.595, 12.97, 77.605, 12.98)
    result = measure_feature(MultiPolygon([a, b]), WGS84)
    assert "repaired" in result.note
    assert result.area_m2 == pytest.approx(geodesic_area(a.union(b)), rel=UTM_TOLERANCE)


@pytest.mark.parametrize(
    "geometry, zone",
    [
        (box(15.0, 78.0, 15.1, 78.05), "EPSG:32633"),  # Svalbard
        (box(151.2, -33.87, 151.21, -33.86), "EPSG:32756"),  # Sydney: southern zone
        (box(-0.13, 51.50, -0.12, 51.51), "EPSG:32630"),  # London, west of Greenwich
        (box(179.5, -17.0, 179.6, -16.9), "EPSG:32760"),  # Fiji, last zone
    ],
)
def test_picks_the_utm_zone_of_the_centroid(geometry, zone):
    result = measure_feature(geometry, WGS84)
    assert result.measurement_crs == zone
    assert result.area_m2 == pytest.approx(geodesic_area(geometry), rel=UTM_TOLERANCE)


@pytest.mark.parametrize(
    "polygon, method",
    [(box(-106.0, 39.0, -101.0, 49.0), METHOD_PROJECTED), (box(10.0, 40.0, 30.0, 50.0), METHOD_GEODESIC)],
)
def test_long_edges_follow_the_source_crs(polygon, method):
    # Edges along a parallel are straight in lon/lat but curve once projected (or
    # become geodesics); without densifying, these lose 0.3% / 0.5% of their area.
    reference = geodesic_area(shapely.segmentize(polygon, 0.01))
    result = measure_feature(polygon, WGS84)
    assert result.method == method
    assert result.area_m2 == pytest.approx(reference, rel=5e-4)


# --- geodesic fallback -----------------------------------------------------------


def test_wide_polygon_is_measured_geodesically():
    wide = box(10.0, 0.0, 30.0, 10.0)
    result = measure_feature(wide, WGS84)

    assert result.method == METHOD_GEODESIC
    assert result.measurement_crs == GEODESIC_LABEL
    assert result.area_m2 == pytest.approx(geodesic_area(shapely.segmentize(wide, 0.01)), rel=1e-4)


def test_wide_line_is_measured_geodesically():
    equator = LineString([(0.0, 0.0), (20.0, 0.0)])
    result = measure_feature(equator, WGS84)
    assert result.method == METHOD_GEODESIC
    assert result.length_m == pytest.approx(20 * 111_319.49, rel=1e-6)  # 20 degrees of equator


def test_geodesic_hole_is_subtracted_whatever_the_winding_order():
    outer, hole = box(0.0, 0.0, 20.0, 10.0), box(5.0, 2.0, 15.0, 8.0)
    expected = geodesic_area(shapely.segmentize(outer, 0.01)) - geodesic_area(shapely.segmentize(hole, 0.01))
    for sign in (1.0, -1.0):
        polygon = orient(Polygon(outer.exterior, [hole.exterior]), sign=sign)
        result = measure_feature(polygon, WGS84)
        assert result.method == METHOD_GEODESIC
        assert result.area_m2 == pytest.approx(expected, rel=1e-4)


def test_antimeridian_multipolygon_is_measured_geodesically():
    # How shapefiles store shapes such as Fiji: one part each side of 180 degrees.
    east, west = box(179.0, -18.0, 180.0, -16.0), box(-180.0, -18.0, -179.0, -16.0)
    result = measure_feature(MultiPolygon([east, west]), WGS84)

    assert result.method == METHOD_GEODESIC
    assert result.area_m2 == pytest.approx(geodesic_area(east) + geodesic_area(west), rel=1e-4)
    assert result.area_m2 == pytest.approx(2 * 106_000 * 221_000, rel=0.02)  # ~2 x (1 deg x 2 deg)


def test_shape_drawn_across_the_antimeridian_is_measured_geodesically():
    across = box(179.5, -17.0, 180.5, -16.0)  # longitudes past 180
    result = measure_feature(across, WGS84)
    assert result.method == METHOD_GEODESIC
    assert result.area_m2 == pytest.approx(geodesic_area(box(-0.5, -17.0, 0.5, -16.0)), rel=1e-4)


def test_polar_polygon_is_measured_geodesically():
    polar = box(10.0, 85.0, 10.5, 85.1)  # north of UTM's 84N limit
    result = measure_feature(polar, WGS84)
    assert result.method == METHOD_GEODESIC
    assert result.area_m2 == pytest.approx(geodesic_area(polar), rel=1e-4)


# --- geometries without a measurement ----------------------------------------------


@pytest.mark.parametrize(
    "geometry, note",
    [
        (None, "Feature has no geometry"),
        (Polygon(), "Geometry is empty"),
        (Point(77.59, 12.97), "No measurement for Point geometries"),
        (MultiPoint([(77.59, 12.97), (77.6, 12.98)]), "No measurement for MultiPoint geometries"),
        (GeometryCollection([Point(0, 0)]), "GeometryCollection has no polygon or line parts to measure"),
    ],
)
def test_unmeasurable_geometries_return_a_note(geometry, note):
    assert measure_feature(geometry, WGS84) == Measurement(note=note)


def test_geometry_collection_sums_its_parts():
    point = Point(77.595, 12.975)
    result = measure_feature(GeometryCollection([SQUARE, LINE, point]), WGS84)

    assert result.area_m2 == pytest.approx(measure_feature(SQUARE, WGS84).area_m2)
    assert result.length_m == pytest.approx(measure_feature(LINE, WGS84).length_m)


def test_z_coordinates_are_ignored():
    with_z = Polygon([(x, y, 900.0) for x, y in SQUARE.exterior.coords])
    result = measure_feature(with_z, WGS84)
    assert result.area_m2 == pytest.approx(measure_feature(SQUARE, WGS84).area_m2)


def test_bow_tie_polygon_is_repaired():
    bow_tie = Polygon([(77.59, 12.97), (77.60, 12.98), (77.60, 12.97), (77.59, 12.98)])
    result = measure_feature(bow_tie, WGS84)

    assert result.note == "Geometry was invalid and was repaired before measuring"
    assert result.area_m2 == pytest.approx(geodesic_area(SQUARE) / 2, rel=UTM_TOLERANCE)
    assert result.length_m is None  # repair artefacts aren't reported as lines


def test_collapsed_polygon_has_no_area_after_repair():
    collapsed = Polygon([(77.5, 12.5), (77.75, 12.75), (78.0, 13.0)])  # exactly collinear
    result = measure_feature(collapsed, WGS84)
    assert result.area_m2 is None and result.length_m is None
    assert result.note == (
        "Geometry was invalid and was repaired before measuring; "
        "Geometry has no area or length after repair"
    )


@pytest.mark.parametrize(
    "geometry",
    [
        Polygon([(0, 0), (float("inf"), 0), (0, 1)]),
        Polygon([(0, 0), (float("nan"), 0), (0, 1)]),
        box(10.0, 95.0, 11.0, 96.0),  # latitude past the pole
    ],
    ids=["inf", "nan", "bad-latitude"],
)
def test_broken_coordinates_return_a_note_instead_of_raising(geometry):
    result = measure_feature(geometry, WGS84)
    assert result.area_m2 is None and result.length_m is None
    assert result.note


def test_coordinates_outside_the_projection_return_a_note():
    result = measure_feature(box(1e30, 1e30, 2e30, 2e30), CRS("EPSG:3857"))
    assert result.area_m2 is None
    assert result.note


def test_original_geometry_is_not_modified():
    with_z = Polygon([(x, y, 900.0) for x, y in SQUARE.exterior.coords])
    before = shapely.to_wkb(with_z)
    measure_feature(with_z, WGS84)
    assert shapely.to_wkb(with_z) == before


@pytest.mark.parametrize("geometry", [SQUARE, LINE, box(10.0, 0.0, 30.0, 10.0)])
def test_results_are_finite(geometry):
    assert_finite(measure_feature(geometry, WGS84))
