"""Dictation: a recording is transcribed on the device, with no network."""

from pathlib import Path

from fastapi.testclient import TestClient

from fieldmind.api import create_app

CLIP = Path(__file__).parent / "fixtures" / "pump-note.wav"
HINDI_CLIP = Path(__file__).parent / "fixtures" / "pump-note-hindi.wav"


def test_a_spoken_note_is_transcribed_on_the_device(speech):
    result = speech.transcribe(CLIP.read_bytes())
    text = result["text"].lower()
    assert "pump" in text and "bearing" in text and "vibration" in text and "7.2" in text, result
    assert result["seconds"] > 2 and result["took_ms"] < 10_000
    assert result["language"] == "en", "the language is detected, not assumed"


def test_a_note_spoken_in_hindi_comes_back_in_hindi(speech):
    result = speech.transcribe(HINDI_CLIP.read_bytes())
    assert result["language"] == "hi", result
    assert result["model"] == "small" and result["provisional"] is False, "re-read by the larger model"
    assert any("\u0900" <= ch <= "\u097f" for ch in result["text"]), "Devanagari, not a translation or a garble"
    assert any(w in result["text"] for w in ("बेरिंग", "बीरिंग", "कमपन", "कंपन", "बदलने")), result
    assert result["took_ms"] < 15_000


def test_english_stays_on_the_fast_model(speech):
    result = speech.transcribe(CLIP.read_bytes())
    assert result["model"] == "base" and result["provisional"] is False


def test_transcription_over_http_and_in_the_status(fleet):
    device = fleet("edge-a")
    with TestClient(create_app(device=device), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.get("/api/status").json()["engine"]["speech_model"].startswith("Whisper base")
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
