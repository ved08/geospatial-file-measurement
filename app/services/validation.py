"""Validation for uploaded geospatial files.

Runs before any geospatial parsing so malformed or hostile uploads are rejected
with a clear 4xx error instead of failing deep inside the GIS libraries.
"""

import shutil
import zipfile
import zlib
from enum import Enum
from pathlib import Path, PurePosixPath
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import parse as parse_xml

from app.core.config import settings

SHAPEFILE_REQUIRED_COMPONENTS = (".shp", ".shx", ".dbf")


class FileType(str, Enum):
    KML = "KML"
    SHAPEFILE = "SHAPEFILE"


_EXTENSION_TYPES = {".kml": FileType.KML, ".zip": FileType.SHAPEFILE}


class UploadValidationError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class UploadTooLargeError(UploadValidationError):
    def __init__(self) -> None:
        super().__init__(
            f"File exceeds the maximum upload size of {settings.max_upload_size_mb} MB",
            status_code=413,
        )


def detect_file_type(filename: str | None) -> FileType:
    if not filename:
        raise UploadValidationError("Uploaded file must have a filename")
    extension = Path(filename).suffix.lower()
    try:
        return _EXTENSION_TYPES[extension]
    except KeyError:
        raise UploadValidationError(
            f"Unsupported file type '{extension or filename}'. Upload a .zip (Shapefile) or .kml file.",
            status_code=415,
        ) from None


def validate_upload(path: Path, file_type: FileType, extract_dir: Path) -> Path:
    """Validate a saved upload and return the path of the dataset to read.

    For KML this is the uploaded file itself; for a zip it is the extracted .shp.
    """
    if path.stat().st_size == 0:
        raise UploadValidationError("Uploaded file is empty")
    if file_type is FileType.KML:
        _validate_kml(path)
        return path
    return _extract_shapefile(path, extract_dir)


def _validate_kml(path: Path) -> None:
    try:
        root = parse_xml(path).getroot()
    except DefusedXmlException:
        raise UploadValidationError(
            "Invalid KML: entity declarations and external references are not allowed"
        ) from None
    except ParseError as exc:
        raise UploadValidationError(f"Invalid KML: file is not well-formed XML ({exc})") from None

    root_name = _local_name(root.tag)
    if root_name != "kml":
        raise UploadValidationError(f"Invalid KML: expected <kml> root element, found <{root_name}>")


def _local_name(tag: str) -> str:
    """Strip the XML namespace, e.g. '{http://www.opengis.net/kml/2.2}kml' -> 'kml'."""
    return tag.rsplit("}", 1)[-1]


def _extract_shapefile(path: Path, extract_dir: Path) -> Path:
    if not zipfile.is_zipfile(path):
        raise UploadValidationError("Invalid .zip: file is not a valid zip archive")
    try:
        with zipfile.ZipFile(path) as archive:
            members = _list_archive_files(archive)
            components = _find_shapefile(members)
            return _extract_components(archive, components, extract_dir)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError) as exc:
        raise UploadValidationError(f"Invalid .zip: archive is corrupt or unsupported ({exc})") from None


def _list_archive_files(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Return the archive's regular files, rejecting unsafe archives."""
    infos = archive.infolist()
    if len(infos) > settings.max_zip_entries:
        raise UploadValidationError(f"Zip contains too many entries (limit {settings.max_zip_entries})")
    if sum(info.file_size for info in infos) > settings.max_extracted_size_mb * 1024 * 1024:
        raise UploadValidationError(
            f"Zip contents exceed the maximum extracted size of {settings.max_extracted_size_mb} MB",
            status_code=413,
        )

    files = []
    for info in infos:
        name = _member_path(info)
        if name.is_absolute() or ".." in name.parts:
            raise UploadValidationError(f"Zip contains an unsafe path: '{info.filename}'")
        if info.is_dir() or _is_junk(name):
            continue
        if info.flag_bits & 0x1:
            raise UploadValidationError("Encrypted zip archives are not supported")
        files.append(info)
    return files


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    # Some Windows tools write backslash separators despite the zip spec.
    return PurePosixPath(info.filename.replace("\\", "/"))


def _is_junk(name: PurePosixPath) -> bool:
    """macOS resource forks (__MACOSX/, ._file) and other hidden files."""
    return "__MACOSX" in name.parts or name.name.startswith(".")


def _find_shapefile(files: list[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
    """Group files by folder + basename and return the components of the single shapefile."""
    groups: dict[tuple[str, str], dict[str, zipfile.ZipInfo]] = {}
    for info in files:
        name = _member_path(info)
        key = (str(name.parent).lower(), name.stem.lower())
        groups.setdefault(key, {})[name.suffix.lower()] = info

    shapefiles = [components for components in groups.values() if ".shp" in components]
    if not shapefiles:
        raise UploadValidationError("No shapefile (.shp) found in the zip archive")
    if len(shapefiles) > 1:
        names = ", ".join(sorted(components[".shp"].filename for components in shapefiles))
        raise UploadValidationError(
            f"Zip contains multiple shapefiles ({names}); upload one shapefile per archive"
        )

    components = shapefiles[0]
    missing = [ext for ext in SHAPEFILE_REQUIRED_COMPONENTS if ext not in components]
    if missing:
        raise UploadValidationError(
            f"Shapefile '{components['.shp'].filename}' is missing required component(s): {', '.join(missing)}"
        )
    return components


def _extract_components(
    archive: zipfile.ZipFile, components: dict[str, zipfile.ZipInfo], extract_dir: Path
) -> Path:
    """Extract the shapefile's components flat into extract_dir.

    Writing to <stem><suffix> (never the archive's own path) prevents zip-slip,
    and lower-casing suffixes lets GDAL find sidecar files on case-sensitive filesystems.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)
    stem = _member_path(components[".shp"]).stem
    for suffix, info in components.items():
        with archive.open(info) as src, (extract_dir / f"{stem}{suffix}").open("wb") as dst:
            shutil.copyfileobj(src, dst)
    return extract_dir / f"{stem}.shp"
