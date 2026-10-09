def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_upload_kml_creates_file_record(client):
    response = client.post("/api/files/", files={"file": ("survey.kml", b"<kml/>")})
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "survey.kml"
    assert body["status"] == "PENDING"

    info = client.get(f"/api/files/{body['id']}/")
    assert info.status_code == 200
    assert info.json()["id"] == body["id"]


def test_unknown_file_returns_404(client):
    assert client.get("/api/files/missing/").status_code == 404
    assert client.get("/api/files/missing/measurements/").status_code == 404


def test_measurements_empty_until_processing_is_implemented(client):
    file_id = client.post("/api/files/", files={"file": ("survey.kml", b"<kml/>")}).json()["id"]
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 200
    assert response.json() == {"file_id": file_id, "feature_count": 0, "features": []}
