"""What can be read from the device's files without its key."""

import stat


def disk_bytes(root) -> bytes:
    out = bytearray()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".key":
            out += path.read_bytes()
    return bytes(out)


def test_private_text_and_the_activity_log_are_sealed_on_disk(fleet):
    a = fleet("edge-a")
    key_file = a.settings.vault_key_file
    assert key_file.exists() and stat.S_IMODE(key_file.stat().st_mode) == 0o600

    health = "Technician Ravi Kumar reported chest pain during night shift, sent to clinic"
    fault = "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement"
    masked = "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting"
    for text in (health, fault, masked):
        a.service.capture(text)
    a.sync.run_once()
    a.close()

    raw = disk_bytes(a.settings.data_dir)
    assert b"chest pain" not in raw, "a private note's text is sealed"
    assert b"9876543210" not in raw and b"Anil Sharma" not in raw, "the unmasked original of a masked note is sealed"
    assert b"[person removed]" in raw, "the masked copy the cloud sees is plain"
    assert b"7.2 mm/s" in raw, "shared text is stored as written"
    assert b"Saved as private" not in raw, "activity lines are sealed"

    # The device itself reads everything back as written.
    again = fleet("edge-a")
    assert again.service.search("chest pain")["results"][0]["text"] == health
    assert any(e["message"].startswith("Saved as private") for e in again.journal.events())


def test_sealing_can_be_turned_off(fleet):
    a = fleet("edge-a", encrypt=False)
    assert a.settings.vault_key_file is None and not a.vault.active
    a.service.capture("Remind me to call home after shift and pick up my keys")
    a.service.local.flush()
    assert b"call home" in disk_bytes(a.settings.data_dir)
