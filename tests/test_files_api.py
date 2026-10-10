VALID_KML = b"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>Farm</name><Point><coordinates>77.59,12.97</coordinates></Point></Placemark>
</Document></kml>"""


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_upload_responds_pending_and_record_is_retrievable(client):
    response = client.post("/api/files/", files={"file": ("survey.kml", VALID_KML)})
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "survey.kml"
    # Processing starts after the response is sent.
    assert body["status"] == "PENDING"
    assert body["feature_count"] is None

    info = client.get(f"/api/files/{body['id']}/")
    assert info.status_code == 200
    # TestClient runs background tasks before post() returns.
    assert info.json()["id"] == body["id"]
    assert info.json()["status"] == "COMPLETED"


def test_unknown_file_returns_404(client):
    assert client.get("/api/files/missing/").status_code == 404
    assert client.get("/api/files/missing/measurements/").status_code == 404


def test_unreadable_kml_fails_and_measurements_return_409(client):
    # Passes validation (well-formed, <kml> root) but GDAL can't read it.
    file_id = client.post("/api/files/", files={"file": ("survey.kml", b"<kml/>")}).json()["id"]
    info = client.get(f"/api/files/{file_id}/").json()
    assert info["status"] == "FAILED"
    assert info["error"].startswith("Could not read survey.kml")

    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 409
    assert response.json()["detail"].startswith("File processing failed: Could not read")
