"""Photos as memories: captured with a caption, found by what they show, synced with their note."""

import io

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from fieldmind.api import create_app

RED_CAPTION = "Pump P-102 coupling guard, inspection photo"
BLUE_CAPTION = "Panel MCC-2 door, inspection photo"
PRIVATE_CAPTION = "Technician Ravi Kumar's injured hand after the slip, sent to clinic"


def picture(color, with_exif=True) -> bytes:
    image = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle([180, 120, 460, 360], fill=(70, 70, 70))
    out = io.BytesIO()
    if with_exif:
        exif = Image.Exif()
        exif[0x010F], exif[0x0110] = "TestCam", "Model X"  # camera make and model
        image.save(out, format="JPEG", exif=exif.tobytes())
    else:
        image.save(out, format="JPEG")
    return out.getvalue()


def test_a_photo_follows_its_caption_and_is_found_by_what_it_shows(fleet):
    a = fleet("edge-a", photos=True)
    red = a.service.attach_photo(picture((200, 30, 30)), RED_CAPTION)
    blue = a.service.attach_photo(picture((30, 80, 220)), BLUE_CAPTION)
    assert red["memory"]["kind"] == "photo" and red["memory"]["scope"] == "shared"
    assert red["photo"]["sync_state"] == "pending" and red["memory"]["photos"][0]["id"] == red["photo"]["id"]

    stored = a.photos.read(red["photo"]["id"])
    assert stored.startswith(b"\xff\xd8"), "a plain JPEG comes back"
    assert not dict(Image.open(io.BytesIO(stored)).getexif()), "camera, position and time details are stripped"
    assert a.photos.path(red["photo"]["id"]).read_bytes().startswith(b"enc1:"), "the file on disk is sealed"

    def image_scores(query):
        return {r["id"]: r["matched"].get("image") for r in a.service.search(query)["results"]}

    # The photo signal sees colour the captions never mention.
    for_red, for_blue = image_scores("a red object"), image_scores("a blue object")
    assert for_red[red["memory"]["id"]] > for_red[blue["memory"]["id"]]
    assert for_blue[blue["memory"]["id"]] > for_blue[red["memory"]["id"]]
    # When caption and photo agree, the memory comes first and carries its photo.
    found = a.service.search("red pump coupling guard")
    assert found["results"][0]["id"] == red["memory"]["id"]
    assert found["results"][0]["photos"][0]["url"].startswith("/api/photos/")
    assert a.service.stats()["photos"] == 2
    assert found["network_calls"] == 0


def test_photos_travel_with_their_memory_and_private_ones_stay(fleet):
    a, b = fleet("edge-a", photos=True), fleet("edge-b", photos=True)
    shared = a.service.attach_photo(picture((200, 30, 30)), RED_CAPTION)
    private = a.service.attach_photo(picture((30, 80, 220)), PRIVATE_CAPTION)
    assert private["memory"]["scope"] == "private" and private["photo"]["sync_state"] == "private"

    pushed = a.sync.run_once()
    assert pushed["pushed"] == 2, "the shared note and its photo; nothing for the private one"
    assert a.cloud.media_count() == 1
    assert a.photos.get(shared["photo"]["id"])["sync_state"] == "synced"

    assert b.sync.run_once()["pulled"] == 2
    got = b.service.get(shared["memory"]["id"])
    assert len(got["photos"]) == 1 and got["photos"][0]["sync_state"] == "replica"
    assert b.photos.read(got["photos"][0]["id"]).startswith(b"\xff\xd8")
    assert b.service.search("a red object")["results"][0]["id"] == shared["memory"]["id"]
    assert b.service.find(private["memory"]["id"]) is None

    a.service.delete(shared["memory"]["id"])
    a.sync.run_once()
    assert a.cloud.media_count() == 0
    assert b.sync.run_once()["removed"] == 2
    assert b.service.find(shared["memory"]["id"]) is None and b.photos.get(got["photos"][0]["id"]) is None
    assert not b.photos.path(got["photos"][0]["id"]).exists()


def test_making_a_memory_private_withdraws_its_photo_too(fleet):
    a, b = fleet("edge-a", photos=True), fleet("edge-b", photos=True)
    saved = a.service.attach_photo(picture((200, 30, 30)), RED_CAPTION)
    memory_id = saved["memory"]["id"]
    a.sync.run_once()
    b.sync.run_once()
    assert len(b.service.get(memory_id)["photos"]) == 1

    a.service.edit(memory_id, scope="private")
    a.sync.run_once()
    b.sync.run_once()
    assert a.cloud.media_count() == 0 and b.service.find(memory_id) is None
    assert a.photos.get(saved["photo"]["id"])["sync_state"] == "private", "the photo itself stays on the device"

    a.service.edit(memory_id, scope="shared")
    a.sync.run_once()
    b.sync.run_once()
    assert a.cloud.media_count() == 1 and len(b.service.get(memory_id)["photos"]) == 1


def test_photos_over_http(fleet):
    device = fleet("edge-a", photos=True)
    with TestClient(create_app(device=device), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        saved = client.post("/api/photos", files={"file": ("guard.jpg", picture((200, 30, 30)), "image/jpeg")},
                            data={"caption": RED_CAPTION}).json()
        assert saved["created"] and saved["photo"]["url"].startswith("/api/photos/")
        image = client.get(saved["photo"]["url"])
        assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
        assert client.post("/api/photos", files={"file": ("x.txt", b"not an image", "text/plain")},
                           data={"caption": "text file"}).status_code == 400
        assert client.post("/api/photos", files={"file": ("guard.jpg", picture((1, 1, 1)), "image/jpeg")},
                           data={"caption": ""}).status_code == 400


def test_photos_are_off_unless_asked_for(fleet):
    a = fleet("edge-a")
    assert a.service.photos is None and "photos" not in a.service.search("anything")["results"]
    with TestClient(create_app(device=a), client=("127.0.0.1", 50000)) as client:
        client.post("/api/auth/setup", json={"pin": "2468"})
        assert client.post("/api/photos", files={"file": ("g.jpg", picture((1, 1, 1)), "image/jpeg")},
                           data={"caption": "x"}).status_code == 409
