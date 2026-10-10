"""Measurement logic: area and perimeter for polygons, length for lines.

Nothing is measured in the file's own coordinates. Every feature is converted to
WGS84 longitude/latitude and then:

* normally projected to the WGS84 UTM zone of its centroid and measured there
  (planar maths in metres, conformal, under ~0.5% scale error inside the limits
  below). This is done for projected sources too: Web Mercator inflates areas
  (4x at 60 degrees latitude) and State Plane files may be in feet, so a
  per-feature UTM zone is the only choice that is right for every input. For
  data already in the matching UTM zone it is effectively a no-op;
* or, when no single UTM zone fits (too wide, crossing the antimeridian, or
  outside UTM's 80S-84N range), measured geodesically on the WGS84 ellipsoid.

Points have no area or length. Problems with a single feature are reported in
`Measurement.note` instead of raising, so one bad geometry can't fail a file.
"""

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import CRSError, ProjError
from shapely.errors import GEOSException
from shapely.geometry import LinearRing, LineString, Polygon
from shapely.geometry.base import BaseGeometry, BaseMultipartGeometry

METHOD_PROJECTED = "projected"
METHOD_GEODESIC = "geodesic"
GEODESIC_LABEL = "EPSG:4326 (geodesic)"

# UTM point scale is k = 0.9996 * (1 + x**2 / 2), where x is the angular
# distance from the zone's central meridian (~ delta_lon * cos(lat), radians).
# Up to x = 0.1 rad (~640 km; 5.7 degrees of longitude at the equator, more
# towards the poles) the length error at the worst vertex stays under 0.5%
# (area ~1%); the feature as a whole is much better than its worst vertex.
# Anything wider is measured geodesically.
MAX_DISTANCE_FROM_CENTRAL_MERIDIAN = 0.1

# UTM is only defined between 80S and 84N (polar regions use UPS).
UTM_MIN_LAT, UTM_MAX_LAT = -80.0, 84.0

# An edge is a straight line in the file's CRS but curves once reprojected, so
# long edges are split before transforming (sparse data such as a state border
# drawn along a parallel otherwise loses 0.3-0.5% of its area).
DENSIFY_DEGREES = 0.1
DENSIFY_METRES = 10_000.0

_WGS84 = CRS.from_epsg(4326)
# Geod only holds the ellipsoid constants, so one shared instance is safe.
_GEOD = Geod(ellps="WGS84")

# Errors from pyproj/shapely for one bad feature; anything else is a bug and raises.
_MEASURE_ERRORS = (ProjError, CRSError, GEOSException, ValueError)


@dataclass(frozen=True)
class Measurement:
    area_m2: float | None = None  # polygons
    perimeter_m: float | None = None  # polygons; includes the edges of holes
    length_m: float | None = None  # lines
    measurement_crs: str | None = None  # e.g. "EPSG:32643", or GEODESIC_LABEL
    method: str | None = None  # METHOD_PROJECTED or METHOD_GEODESIC
    note: str | None = None  # why nothing was measured, or that the geometry was repaired


class _NotMeasurable(Exception):
    """A feature that can't be measured; the message becomes the note."""


def measure_feature(geometry: BaseGeometry | None, crs: CRS) -> Measurement:
    """Measure one geometry given in `crs`. Never raises for bad geometries."""
    if geometry is None:
        return Measurement(note="Feature has no geometry")
    if geometry.is_empty:
        return Measurement(note="Geometry is empty")

    notes: list[str] = []
    try:
        return _measure(geometry, crs, notes)
    except _NotMeasurable as exc:
        return Measurement(note=_join([*notes, str(exc)]))
    except _MEASURE_ERRORS as exc:
        return Measurement(note=_join([*notes, f"Could not measure geometry: {exc}"]))


def _measure(geometry: BaseGeometry, crs: CRS, notes: list[str]) -> Measurement:
    # Measurements are 2D: Z (height) and M values are dropped.
    geometry = shapely.force_2d(geometry)
    polygons, lines = _split_parts(geometry)
    if not polygons and not lines:
        if geometry.geom_type == "GeometryCollection":
            raise _NotMeasurable("GeometryCollection has no polygon or line parts to measure")
        raise _NotMeasurable(f"No measurement for {geometry.geom_type} geometries")

    if not geometry.is_valid:
        polygons, lines = _repair(polygons, lines)
        notes.append("Geometry was invalid and was repaired before measuring")
        if not polygons and not lines:
            raise _NotMeasurable("Geometry has no area or length after repair")

    measurable = _collect(polygons, lines)
    step = _densify_step(crs)
    if step > 0:
        measurable = shapely.segmentize(measurable, step)
    lonlat = _transform(measurable, _to_wgs84(crs))
    if np.abs(shapely.get_coordinates(lonlat)[:, 1]).max() > 90:
        raise _NotMeasurable("Geometry has latitudes outside -90 to 90 degrees")
    zone = _utm_zone(lonlat)
    if zone is None:
        polygons, lines = _split_parts(lonlat)
        area, perimeter, length = _geodesic(polygons, lines)
        method, label = METHOD_GEODESIC, GEODESIC_LABEL
    else:
        polygons, lines = _split_parts(_transform(lonlat, _wgs84_to_utm(zone)))
        area = sum(polygon.area for polygon in polygons)
        perimeter = sum(polygon.length for polygon in polygons)
        length = sum(line.length for line in lines)
        method, label = METHOD_PROJECTED, f"EPSG:{zone}"

    return Measurement(
        area_m2=_finite(area) if polygons else None,
        perimeter_m=_finite(perimeter) if polygons else None,
        length_m=_finite(length) if lines else None,
        measurement_crs=label,
        method=method,
        note=_join(notes),
    )


def _split_parts(geometry: BaseGeometry) -> tuple[list[Polygon], list[LineString]]:
    """Flatten (nested) multi-geometries into polygons and lines; points are skipped."""
    polygons: list[Polygon] = []
    lines: list[LineString] = []
    if geometry.is_empty:
        pass
    elif isinstance(geometry, BaseMultipartGeometry):  # Multi* and GeometryCollection
        for part in geometry.geoms:
            part_polygons, part_lines = _split_parts(part)
            polygons += part_polygons
            lines += part_lines
    elif isinstance(geometry, Polygon):
        polygons.append(geometry)
    elif isinstance(geometry, (LineString, LinearRing)):
        lines.append(geometry)
    return polygons, lines


def _collect(polygons: list[Polygon], lines: list[LineString]) -> BaseGeometry:
    return shapely.GeometryCollection([*polygons, *lines])


def _repair(
    polygons: list[Polygon], lines: list[LineString]
) -> tuple[list[Polygon], list[LineString]]:
    """make_valid the polygonal and linear parts separately.

    Each group keeps only its own kind of result: a polygon repair can leave
    collapsed slivers as lines (and a degenerate line can collapse to a point);
    those are artefacts and must not turn up as a length (or vanish silently).
    Polygons use the "structure" method, which unions overlapping parts of a
    MultiPolygon (counted once) instead of the default "linework" method's
    even-odd rule, which would cut the overlap out as a hole.
    """
    if polygons:
        merged = shapely.MultiPolygon(polygons)
        if not merged.is_valid:
            repaired = shapely.make_valid(merged, method="structure", keep_collapsed=False)
            polygons = _split_parts(repaired)[0]
    if lines:
        merged = shapely.MultiLineString(lines)
        if not merged.is_valid:
            lines = _split_parts(shapely.make_valid(merged))[1]
    return polygons, lines


def _densify_step(crs: CRS) -> float:
    """Longest edge to keep, in the CRS's own units (degrees, metres, feet...)."""
    axis = crs.axis_info[0] if crs.axis_info else None
    if axis is None or not axis.unit_conversion_factor:
        return 0.0  # unknown units: don't densify
    if crs.is_geographic:  # factor is radians per unit
        return math.radians(DENSIFY_DEGREES) / axis.unit_conversion_factor
    return DENSIFY_METRES / axis.unit_conversion_factor  # factor is metres per unit


def _transform(geometry: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    def project(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    result = shapely.transform(geometry, project)
    if not np.isfinite(shapely.get_coordinates(result)).all():
        raise _NotMeasurable("Geometry has coordinates outside the valid range of its CRS")
    return result


# Transformers are slow to create and a file can have thousands of features, so
# they're cached. Since pyproj 3.1 a Transformer keeps a per-thread copy of its
# PROJ object, so the cached instances are safe to share between threads.
@lru_cache(maxsize=64)
def _to_wgs84(crs: CRS) -> Transformer:
    return Transformer.from_crs(crs.to_2d(), _WGS84, always_xy=True)


@lru_cache(maxsize=120)  # 60 zones x 2 hemispheres
def _wgs84_to_utm(epsg: int) -> Transformer:
    return Transformer.from_crs(_WGS84, CRS.from_epsg(epsg), always_xy=True)


def _utm_zone(lonlat: BaseGeometry) -> int | None:
    """EPSG code of the UTM zone for the geometry, or None if no zone fits."""
    coords = shapely.get_coordinates(lonlat)
    lon, lat = coords[:, 0], coords[:, 1]
    # Parts on both sides of the antimeridian (e.g. Fiji, Russia) show up as a
    # longitude span of nearly 360 degrees; lon beyond +-180 means the shape was
    # drawn across it. Either way no single zone holds it.
    if lon.max() - lon.min() > 180 or np.abs(lon).max() >= 180:
        return None
    if lat.min() < UTM_MIN_LAT or lat.max() > UTM_MAX_LAT:
        return None

    centroid = lonlat.centroid
    zone = min(int((centroid.x + 180) // 6) + 1, 60)
    central_meridian = -183 + 6 * zone
    distance = np.radians(np.abs(lon - central_meridian)) * np.cos(np.radians(lat))
    if distance.max() > MAX_DISTANCE_FROM_CENTRAL_MERIDIAN:
        return None
    return (32600 if centroid.y >= 0 else 32700) + zone


def _geodesic(polygons: list[Polygon], lines: list[LineString]) -> tuple[float, float, float]:
    area = perimeter = 0.0
    for polygon in polygons:
        # Rings are measured one at a time so the result doesn't depend on their
        # winding order: holes are always subtracted.
        outer_area, outer_perimeter = _ring_area_perimeter(polygon.exterior)
        area += outer_area
        perimeter += outer_perimeter
        for ring in polygon.interiors:
            hole_area, hole_perimeter = _ring_area_perimeter(ring)
            area -= hole_area
            perimeter += hole_perimeter
    length = sum(_GEOD.geometry_length(line) for line in lines)
    return max(area, 0.0), perimeter, length


def _ring_area_perimeter(ring: LinearRing) -> tuple[float, float]:
    lon, lat = ring.xy
    area, perimeter = _GEOD.polygon_area_perimeter(lon, lat)
    return abs(area), perimeter


def _finite(value: float) -> float:
    if not math.isfinite(value):
        raise _NotMeasurable("Measurement produced a non-finite value")
    return float(value)


def _join(notes: list[str]) -> str | None:
    return "; ".join(notes) or None
