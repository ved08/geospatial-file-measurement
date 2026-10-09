import io
import zipfile

import pytest

from app.core.config import settings

VALID_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Placemark><name>Farm</name><Point><coordinates>77.59,12.97</coordinates></Point></Placemark>
</kml>"""

SHAPEFILE = {"farms.shp": b"shp", "farms.shx": b"shx", "farms.dbf": b"dbf", "farms.prj": b"prj"}


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def upload(client, filename: str, content: bytes):
    return client.post("/api/files/", files={"file": (filename, content)})


# --- file type ---------------------------------------------------------------


@pytest.mark.parametrize("filename", ["notes.txt", "data.geojson", "noextension"])
def test_rejects_unsupported_file_type(client, filename):
    response = upload(client, filename, b"data")
    assert response.status_code == 415
    assert "Unsupported file type" in response.json()["detail"]


def test_extension_check_is_case_insensitive(client):
    assert upload(client, "SURVEY.KML", VALID_KML).status_code == 201


def test_rejects_empty_file(client):
    response = upload(client, "survey.kml", b"")
    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is empty"


def test_rejects_file_over_size_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    response = upload(client, "survey.kml", b"x" * (1024 * 1024 + 1))
    assert response.status_code == 413


def test_rejected_upload_leaves_nothing_on_disk(client):
    upload(client, "survey.kml", b"not xml")
    assert list(settings.upload_dir.iterdir()) == []


# --- KML -----------------------------------------------------------------------


def test_accepts_valid_kml(client):
    response = upload(client, "survey.kml", VALID_KML)
    assert response.status_code == 201
    assert response.json()["filename"] == "survey.kml"


def test_rejects_malformed_kml(client):
    response = upload(client, "survey.kml", b"<kml><Placemark></kml>")
    assert response.status_code == 400
    assert "not well-formed XML" in response.json()["detail"]


def test_rejects_xml_that_is_not_kml(client):
    response = upload(client, "survey.kml", b"<html><body/></html>")
    assert response.status_code == 400
    assert "expected <kml> root element, found <html>" in response.json()["detail"]


def test_rejects_kml_with_entity_expansion(client):
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE kml [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>
<kml>&b;</kml>"""
    response = upload(client, "survey.kml", payload)
    assert response.status_code == 400
    assert "entity" in response.json()["detail"]


# --- Shapefile zip -------------------------------------------------------------


def test_accepts_valid_shapefile_zip_and_extracts_it(client):
    response = upload(client, "farms.zip", make_zip(SHAPEFILE))
    assert response.status_code == 201
    extracted = settings.upload_dir / response.json()["id"] / "extracted"
    assert sorted(p.name for p in extracted.iterdir()) == sorted(SHAPEFILE)


def test_accepts_shapefile_in_subfolder_and_ignores_macos_junk(client):
    entries = {f"data/{name}": data for name, data in SHAPEFILE.items()}
    entries["__MACOSX/data/._farms.shp"] = b"junk"
    entries["data/.DS_Store"] = b"junk"
    assert upload(client, "farms.zip", make_zip(entries)).status_code == 201


def test_accepts_uppercase_component_extensions(client):
    entries = {name.upper(): data for name, data in SHAPEFILE.items()}
    response = upload(client, "farms.zip", make_zip(entries))
    assert response.status_code == 201
    extracted = settings.upload_dir / response.json()["id"] / "extracted"
    assert (extracted / "FARMS.shp").exists()


def test_rejects_file_that_is_not_a_zip(client):
    response = upload(client, "farms.zip", b"definitely not a zip")
    assert response.status_code == 400
    assert "not a valid zip archive" in response.json()["detail"]


def test_rejects_zip_without_shapefile(client):
    response = upload(client, "farms.zip", make_zip({"readme.txt": b"hi"}))
    assert response.status_code == 400
    assert "No shapefile (.shp) found" in response.json()["detail"]


@pytest.mark.parametrize("missing", [".shx", ".dbf"])
def test_rejects_shapefile_missing_required_component(client, missing):
    entries = {name: data for name, data in SHAPEFILE.items() if not name.endswith(missing)}
    response = upload(client, "farms.zip", make_zip(entries))
    assert response.status_code == 400
    assert f"missing required component(s): {missing}" in response.json()["detail"]


def test_components_must_share_folder_with_shp(client):
    entries = {"a/farms.shp": b"shp", "a/farms.shx": b"shx", "b/farms.dbf": b"dbf"}
    response = upload(client, "farms.zip", make_zip(entries))
    assert response.status_code == 400
    assert ".dbf" in response.json()["detail"]


def test_accepts_shapefile_without_prj(client):
    entries = {name: data for name, data in SHAPEFILE.items() if not name.endswith(".prj")}
    assert upload(client, "farms.zip", make_zip(entries)).status_code == 201


def test_rejects_zip_with_multiple_shapefiles(client):
    entries = dict(SHAPEFILE)
    entries.update({"roads.shp": b"shp", "roads.shx": b"shx", "roads.dbf": b"dbf"})
    response = upload(client, "farms.zip", make_zip(entries))
    assert response.status_code == 400
    assert "multiple shapefiles" in response.json()["detail"]


def test_rejects_zip_with_path_traversal(client):
    entries = {f"../{name}": data for name, data in SHAPEFILE.items()}
    response = upload(client, "farms.zip", make_zip(entries))
    assert response.status_code == 400
    assert "unsafe path" in response.json()["detail"]


def test_rejects_zip_exceeding_extracted_size_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "max_extracted_size_mb", 1)
    entries = dict(SHAPEFILE, **{"farms.dbf": b"\0" * (1024 * 1024 + 1)})
    assert upload(client, "farms.zip", make_zip(entries)).status_code == 413


def test_rejects_zip_with_too_many_entries(client, monkeypatch):
    monkeypatch.setattr(settings, "max_zip_entries", 3)
    response = upload(client, "farms.zip", make_zip(SHAPEFILE))
    assert response.status_code == 400
    assert "too many entries" in response.json()["detail"]


# --- request size limit (middleware) -------------------------------------------


def test_rejects_oversized_request_from_content_length_header(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    response = upload(client, "survey.kml", b"x" * (3 * 1024 * 1024))
    assert response.status_code == 413
    assert response.json() == {"detail": "File exceeds the maximum upload size of 1 MB"}


def test_rejects_oversized_chunked_request_without_content_length(client, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    boundary = "test-boundary"

    def body():
        yield (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.kml\"\r\n\r\n"
        ).encode()
        for _ in range(3):
            yield b"x" * (1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        "/api/files/",
        content=body(),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "File exceeds the maximum upload size of 1 MB"


# --- storage -------------------------------------------------------------------


def test_record_uses_sanitised_filename(client):
    response = upload(client, "../../etc/survey.kml", VALID_KML)
    assert response.status_code == 201
    assert response.json()["filename"] == "survey.kml"


def test_unexpected_error_still_cleans_up_upload(client, monkeypatch):
    def broken_validate(*args):
        raise OSError("disk full")

    monkeypatch.setattr("app.services.uploads.validate_upload", broken_validate)
    with pytest.raises(OSError):
        upload(client, "survey.kml", VALID_KML)
    assert list(settings.upload_dir.iterdir()) == []
