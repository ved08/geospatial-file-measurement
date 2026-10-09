"""Measurement logic: area for polygons, length for lines.

TODO:
  - pick a projected CRS (e.g. UTM zone from the feature centroid)
  - reproject geographic geometries before measuring
  - return a note instead of raising for unsupported/empty geometries
"""


def measure_feature(geometry, source_crs):
    raise NotImplementedError("Measurement is not implemented yet")
