"""Faces in photos are hidden in the copy that leaves the device."""

import io
from pathlib import Path

from PIL import Image

from fieldmind.config import Settings
from fieldmind.faces import FaceFinder, hide
from tests.test_photos import picture

PORTRAIT = (Path(__file__).parent / "fixtures" / "face.jpg").read_bytes()
CAPTION = "Contractor at the MCC-2 panel before the lockout, inspection photo"


def test_the_detector_finds_a_face_and_not_a_box():
    finder = FaceFinder(Settings().models_dir)
    assert finder.available, finder.error
    boxes = finder.find(PORTRAIT)
    assert len(boxes) == 1
    left, top, right, bottom = boxes[0]
    assert right - left > 80 and bottom - top > 100, boxes
    assert finder.find(picture((200, 30, 30))) == []
    assert finder.find(hide(PORTRAIT, boxes)) == [], "once hidden, there is no face left to find"


def test_the_fleet_gets_the_photo_with_faces_hidden_and_the_owner_keeps_it_whole(fleet):
    a, b = fleet("edge-a", photos=True), fleet("edge-b", photos=True)
    result = a.service.attach_photo(PORTRAIT, CAPTION)
    photo = result["photo"]
    assert result["memory"]["scope"] == "shared" and photo["faces"] == 1

    kept, shared = a.photos.read(photo["id"]), a.photos.read(photo["id"], shared=True)
    assert kept != shared and a.photos.shared_path(photo["id"]).read_bytes().startswith(b"enc1:")
    finder = a.photos.faces
    assert finder.find(kept) and not finder.find(shared)
    assert Image.open(io.BytesIO(shared)).size == Image.open(io.BytesIO(kept)).size

    a.sync.run_once()
    b.sync.run_once()
    received = b.photos.for_memory(result["memory"]["id"])
    assert len(received) == 1 and received[0]["faces"] == 1
    assert b.photos.read(received[0]["id"]) == shared, "the other device never sees the face"
    assert not finder.find(b.photos.read(received[0]["id"]))

    a.service.delete(result["memory"]["id"])
    assert not a.photos.path(photo["id"]).exists() and not a.photos.shared_path(photo["id"]).exists()


def test_a_photo_without_faces_travels_unchanged(fleet):
    a = fleet("edge-a", photos=True)
    result = a.service.attach_photo(picture((30, 80, 220)), "Panel MCC-2 door, inspection photo")
    assert result["photo"]["faces"] == 0
    assert not a.photos.shared_path(result["photo"]["id"]).exists()
    assert a.photos.read(result["photo"]["id"], shared=True) == a.photos.read(result["photo"]["id"])
