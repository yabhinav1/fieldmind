# Demo video script

Target length: 3 minutes. Record at 1920×1080 with both dashboards side by side, edge-a on the left and edge-b on the right. Use light mode; it reads better on a projector.

## Before recording

```bash
python -m fieldmind reset-all --pin 2468           # clean slate, both devices restarted (or .\scripts\reset.ps1 -Pin 2468)
python -m fieldmind doctor                         # every line should say ok
```

Unlock both dashboards with the PIN. Ask one throwaway question on edge-a so the language model is loaded, then clear the search box. Close other apps: the model needs the GPU.

## Shots

| Time | What to do | What to say |
|---|---|---|
| 0:00 | Show both dashboards, empty. | "FieldMind is a memory for field technicians. It runs on Qdrant Edge and has to work with no network." |
| 0:10 | edge-a: click *Publish headquarters manuals to the cloud*. *Received from cloud* goes to 8. | "Headquarters publishes manuals to Qdrant Server. The device pulls them into a local replica shard." |
| 0:25 | edge-a: switch *Network* off. Header turns amber. | "Now the technician walks into the plant and loses signal." |
| 0:32 | edge-a: click *Load sample notes*. Point at the activity log. | "They keep recording. Each note is classified on the device: shared, private, or shared with personal details masked. *Waiting to sync* counts up." |
| 0:50 | edge-a: search `is a vibration of 7.2 mm/s acceptable`. Point at the timing line, then the answer. | "Search takes about a millisecond and makes zero network calls. The answer is written by a small model on the device, from a manual that came from the cloud earlier, and it cites it." |
| 1:15 | edge-a: type `Suresh and Priya replaced the coupling on pump P-102, call 9876543210 if it trips`. Point at *What the cloud receives*. | "Here is the decision being made. The fleet should know about the coupling. It should not get two names and a phone number. The original stays here, sealed on disk." |
| 1:28 | edge-a: click 📷 Photo, pick any picture, caption it `Coupling guard refitted on P-102`, save. Search `coupling guard`. | "Photos are memories too. The caption decides what is shared; the photo is found by what it shows, and location and camera details are stripped." |
| 1:35 | edge-a: switch *Network* on. Open the *Sync* tab as the queue drains. | "Signal returns. The queue drains by priority, so the gas leak goes first." |
| 1:45 | edge-a: open the *Cloud* tab. Scroll. | "This is what Qdrant Server actually holds. No health note, no password, and the names are masked." |
| 1:58 | edge-b: search `pump bearing vibration`. | "The second device now knows what the first one saw." |
| 2:05 | edge-b: open the Demo guide and click *Stage a conflict* (or do it by hand: both *Network* off, change the V-17 note differently on each, edge-a on, then edge-b). edge-b: *Sync* tab. | "Two technicians change the same note while offline. The second one to reconnect does not overwrite the first. It shows both versions and a person decides." |
| 2:35 | edge-b: click *Keep mine*. | "That decision syncs like any other change." |
| 2:42 | edge-a: *Sync* tab, click *Rebuild cloud replica*. Point at the activity log line. | "And if a device falls far behind, it restores its replica from a Qdrant Server snapshot in one download, without touching its private notes." |
| 2:55 | Show both dashboards. | "Remember, retrieve, work offline, and sync intelligently. That is FieldMind." |

## If something goes wrong on camera

- **The answer takes more than five seconds.** The model was not loaded. Cut, ask any question once, and retake.
- **No answer box appears.** The language model server is not running. Run `.\scripts\llm.ps1`. Answers still work without it, composed from the notes.
- **"Cloud unreachable" when you expect online.** Docker Desktop is not running. Start it and run `python -m fieldmind start` again. With the cloud down the two devices still exchange notes if they are configured as peers, so the fleet step still works.
- **The conflict does not appear.** Use *Stage a conflict* in the Demo guide. By hand: both devices must edit the text while *Network* is off on both, and edge-a must reconnect first.
- **Dictate says the language is not supported.** The browser retries with its online recogniser by itself; if it still fails, type the note.

## Optional closing shot: a real network cut

```bash
docker compose --profile linux-device up -d
python scripts/linux_device_check.py <PIN>        # set edge-c's PIN once at http://127.0.0.1:8003 first
```

Show the terminal. It disconnects a Linux device from the cloud's network, checks that it noticed and kept working, reconnects it and checks that its queue drained. Say: "The Network switch is a convenience. This is the same thing with the cable pulled."
