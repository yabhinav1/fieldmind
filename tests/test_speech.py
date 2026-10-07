"""Dictation: a recording is transcribed on the device, with no network."""

from pathlib import Path

from fastapi.testclient import TestClient

from fieldmind.api import create_app

CLIP = Path(__file__).parent / "fixtures" / "pump-note.wav"


def test_a_spoken_note_is_transcribed_on_the_device(speech):
    result = speech.transcribe(CLIP.read_bytes())
    text = result["text"].lower()
    assert "pump" in text and "bearing" in text and "vibration" in text and "7.2" in text, result
    assert result["seconds"] > 2 and result["took_ms"] < 10_000


def test_transcription_over_http_and_in_the_status(fleet):
    device = fleet("edge-a")
    with TestClient(create_app(device=device), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.get("/api/status").json()["engine"]["speech_model"] == "Whisper base (on device)"
        response = client.post("/api/transcribe", files={"file": ("dictation.wav", CLIP.read_bytes(), "audio/wav")})
        assert response.status_code == 200 and "vibration" in response.json()["text"].lower()
        bad = client.post("/api/transcribe", files={"file": ("x.bin", b"not audio at all", "application/octet-stream")})
        assert bad.status_code in (400, 503), "something that is not audio is refused"


def test_speech_can_be_turned_off(fleet):
    device = fleet("edge-a", speech=False)
    with TestClient(create_app(device=device), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.get("/api/status").json()["engine"]["speech_model"] is None
        assert client.post("/api/transcribe", files={"file": ("d.wav", CLIP.read_bytes(), "audio/wav")}).status_code == 409
