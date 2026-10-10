"""File processing: read every feature out of a validated .kml or .shp.

The output is format-agnostic: geometries stay in the file's own CRS as Shapely
objects, and properties are converted to JSON-safe values. Measuring is done
separately (see measurement.py).
"""

import base64
import math
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyogrio
import pyogrio.errors
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from app.core.config import settings

# Fields the LIBKML driver adds to every KML layer. They describe how Google
# Earth draws a placemark, not the data itself, and are mostly placeholders
# (tessellate=-1, drawOrder=NaN, ...).
KML_RENDERING_FIELDS = frozenset(
    {"altitudeMode", "tessellate", "extrude", "visibility", "drawOrder", "icon"}
)

_READ_ERRORS = (
    pyogrio.errors.DataSourceError,
    pyogrio.errors.DataLayerError,
    pyogrio.errors.FeatureError,
    pyogrio.errors.FieldError,
    pyogrio.errors.GeometryError,
    pyogrio.errors.CRSError,
    CRSError,
)


class ProcessingError(Exception):
    """The file passed validation but its contents can't be read."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ParsedFeature:
    id: int  # position across all layers, starting at 0
    layer: str
    geometry: BaseGeometry | None  # in the source CRS; None if the feature has no geometry
    crs: CRS
    properties: dict[str, Any]

    @property
    def geometry_type(self) -> str | None:
        return self.geometry.geom_type if self.geometry is not None else None

    @property
    def geojson(self) -> dict[str, Any] | None:
        return mapping(self.geometry) if self.geometry is not None else None


@dataclass
class ParsedDataset:
    crs: CRS | None  # None if the file has no features or its layers disagree
    layers: list[str]
    features: list[ParsedFeature]
    warnings: list[str] = field(default_factory=list)


def crs_label(crs: CRS) -> str:
    """Short name for a CRS, e.g. 'EPSG:4326'; falls back to its full name."""
    authority = crs.to_authority()
    return f"{authority[0]}:{authority[1]}" if authority else crs.name


def read_dataset(path: Path) -> ParsedDataset:
    """Read all layers of a .kml or .shp into format-agnostic features."""
    is_kml = path.suffix.lower() == ".kml"
    dataset = ParsedDataset(crs=None, layers=[], features=[])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            # KML puts each <Folder> in its own layer; read them all, not just the first.
            for index, (layer_name, _) in enumerate(pyogrio.list_layers(path)):
                _read_layer(path, index, str(layer_name), is_kml, dataset)
        except _READ_ERRORS as exc:
            raise ProcessingError(f"Could not read {path.name}: {_hide_path(str(exc), path)}") from exc

    for warning in caught:
        message = _hide_path(str(warning.message), path)
        if message not in dataset.warnings:
            dataset.warnings.append(message)

    layer_crs = {feature.crs for feature in dataset.features}
    if len(layer_crs) == 1:
        dataset.crs = layer_crs.pop()
    elif len(layer_crs) > 1:
        dataset.warnings.append("Layers use different coordinate reference systems")
    return dataset


def _read_layer(path: Path, index: int, name: str, is_kml: bool, dataset: ParsedDataset) -> None:
    table = pyogrio.read_dataframe(path, layer=index)
    dataset.layers.append(name)
    if table.empty:
        return

    crs = _resolve_crs(table.crs, dataset)
    attributes = table.drop(columns=table.geometry.name).to_dict("records")
    for geometry, attrs in zip(table.geometry, attributes):
        dataset.features.append(
            ParsedFeature(
                id=len(dataset.features),
                layer=name,
                geometry=geometry,
                crs=crs,
                properties=_clean_properties(attrs, is_kml),
            )
        )


def _resolve_crs(crs: CRS | None, dataset: ParsedDataset) -> CRS:
    if crs is not None:
        return crs
    if settings.default_crs is None:
        raise ProcessingError(
            "The file does not define a coordinate reference system "
            "(for a shapefile, include the .prj file in the zip)"
        )
    warning = f"No coordinate reference system in the file; assumed {settings.default_crs}"
    if warning not in dataset.warnings:
        dataset.warnings.append(warning)
    return CRS.from_user_input(settings.default_crs)


def _clean_properties(attrs: dict[str, Any], is_kml: bool) -> dict[str, Any]:
    properties = {}
    for key, value in attrs.items():
        if is_kml and key in KML_RENDERING_FIELDS:
            continue
        value = _json_safe(value)
        # KML has no fixed schema: a null means this placemark simply doesn't have
        # the field (it exists on another placemark in the layer), so drop it.
        # Shapefiles do have a schema, so their nulls are real values and are kept.
        if is_kml and value is None:
            continue
        properties[str(key)] = value
    return properties


def _json_safe(value: Any) -> Any:
    """Convert pandas/numpy values to plain JSON types."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if pd.isna(value):  # NaN, NaT, pd.NA
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if isinstance(value, (datetime, date, time)):  # includes pd.Timestamp
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(value).decode("ascii")
    return str(value)


def _hide_path(message: str, path: Path) -> str:
    """Strip server paths from library messages before they reach API clients."""
    return message.replace(str(path.parent) + "/", "").replace(str(path.parent), "")
