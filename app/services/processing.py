"""File parsing: turns an uploaded .zip (Shapefile) or .kml into features.

TODO: implement with GeoPandas/Fiona.
  - .zip: safely extract, locate the .shp, read with its .prj CRS
  - .kml: read placemarks (CRS is always EPSG:4326)
"""

from pathlib import Path


def read_features(path: Path):
    raise NotImplementedError("File parsing is not implemented yet")
