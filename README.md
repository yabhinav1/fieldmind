# FieldMind

[![tests](https://github.com/yabhinav1/fieldmind/actions/workflows/tests.yml/badge.svg)](https://github.com/yabhinav1/fieldmind/actions/workflows/tests.yml)

Offline-first memory for field technicians, built on **Qdrant Edge**. By Team Compilers.

A technician records what they see and do. The device remembers it, searches it in about a millisecond with no network, answers questions from it, decides by itself what is safe to share, and syncs with the rest of the fleet through Qdrant Server whenever a connection exists.

Problem statement 03: AI-Powered Edge Memory and Intelligence Platform.

![FieldMind walkthrough: go offline, record notes, search and ask, reconnect, resolve a conflict](docs/img/demo.gif)

## For judges

| If you have | Do this |
|---|---|
| 2 minutes | Read the [deck (PDF)](docs/FieldMind-Compilers.pdf) and the walkthrough above. |
| 5 minutes | Read [how each line of the problem statement is met](docs/requirements.md), with the code and the test that proves it. |
| 10 minutes | Run it: `docker compose --profile demo up --build`, open http://localhost:8001 and http://localhost:8002 (PIN `2468`), and click **Demo guide** in the top right. Or [host it online for free](docs/host-for-free.md). |
| A question | [Pitch notes](docs/pitch.md) answer the ones we expect: why two shards, how snapshots are used, what stops devices overwriting each other. |

## What it does

| Goal from the problem statement | How FieldMind does it |
|---|---|
| Searchable semantic memory on the device | Two Qdrant Edge shards run inside the app process, a third for photos. No database server on the device. Private text is sealed on disk with a device key. |
| Low-latency vector and hybrid search without network | Dense vectors (bge-small, ONNX, on CPU) plus Qdrant Edge's built-in BM25. About 1 ms per search. |
| Reason over local information | A 3B language model on the device phrases answers from the notes search found, with citations. Every number, asset tag and verdict in a generated answer is checked against the note it cites; if it does not match, the device quotes the notes instead. A 23 MB cross-encoder reranks the candidates first. No cloud call. |
| Decide what stays local and what syncs | A policy engine classifies every note on the device. Private notes stay, useful notes are shared, and notes that mix both are shared with names and contact details masked. The person can override, and the device learns from those overrides. |
| Work through intermittent connectivity | Every change goes to a durable outbox first. It survives restarts and replays when the link returns, urgent items first. |
| Sync with Qdrant Server | Uploads are compare-and-swap writes. Small downloads use a manifest diff. A device that is far behind restores its replica from a Qdrant Server shard snapshot. |
| Evolving memory, updates and conflicts | Follow-up notes replace earlier beliefs. Concurrent edits merge field by field. True clashes wait for a person and nothing is overwritten. |
| Interface to inspect everything | Dashboard with memory, search results, sync queue, conflicts and a live activity log, behind a device PIN. Installable on a phone; notes can be dictated. |
| A meaningful edge-to-cloud workflow | Headquarters publishes manuals to the cloud, devices carry them offline, field notes and photos flow back to the fleet. When the cloud is out of reach, devices on the same network learn from each other directly. |

[docs/requirements.md](docs/requirements.md) maps each goal to its code and tests.

## Architecture

```mermaid
flowchart LR
  subgraph device["Edge device, one process"]
    ui["Dashboard and API<br/>behind a PIN"]
    policy["Policy engine<br/>name model + classifier"]
    subgraph edge["Qdrant Edge"]
      local[("Local shard<br/>written here, private too")]
      replica[("Replica shard<br/>copy of cloud knowledge")]
    end
    journal["Journal (SQLite)<br/>outbox and conflicts"]
    llm["Answer model<br/>llama3.2 3B, optional"]
  end
  cloud[("Qdrant Server<br/>fleet_memory")]
  others["Other edge devices"]

  ui --> policy --> local
  policy --> journal
  local -- "push: compare-and-swap" --> cloud
  cloud -- "pull: diff or snapshot" --> replica
  local --> llm
  replica --> llm
  others <--> cloud
```

**Why two shards.** The local shard holds what this device wrote, including everything private. The replica shard is a disposable copy of cloud knowledge. The replica can be wiped and rebuilt from a snapshot at any time without touching a single private note.

**Why search is federated.** Each shard is asked for semantic and keyword matches separately. Raw cosine and BM25 scores are comparable across shards, so every memory gets one score per signal and these are combined into one relevance score. Rank-only fusion per shard would let a weak match in a small shard outrank a strong match in a large one.

**Why snapshot and diff.** A shard snapshot moves a whole collection in one file, which suits a new device or one that has been away for weeks. A manifest diff moves only what changed and lets a device skip other sites' data, which suits the everyday trickle. The device picks by how far behind it is (`FIELDMIND_SNAPSHOT_MIN_POINTS`, default 200). After restoring, it trims the replica to its own site and merges it into one segment, because every segment preallocates about 215 MB on the device.

## How sharing is decided

The policy runs on the device in about 20 ms and combines three kinds of evidence.

1. **Pattern detectors** for credentials, phone numbers, emails, identity numbers, vehicle registrations, badge numbers and street addresses.
2. **A name recognition model** (quantised BERT, ONNX, about 105 MB) for people, with or without a title in front.
3. **A semantic classifier** that compares the note with example sentences per category, using the same local embedding model as search. A note that reads as two kinds at once (a hazard that is also about someone's injury) is flagged for a look.

And it learns from the person: when a technician overrides a decision, the device keeps the note's vector and the chosen scope, never the text, and later notes at least 88% similar follow that choice. Credentials can never be learned into sharing.

| Example note | Decision |
|---|---|
| Pump P-102 bearing vibration high at 7.2 mm/s | Shared |
| Smell of gas near compressor K-4 | Shared, urgent, sent first |
| Suresh and Priya replaced the coupling on P-102, call 9876543210 | Shared with both names and the number masked. The original stays on the device. |
| Technician Ravi Kumar reported chest pain | Private |
| SCADA panel login password is ... | Private, and cannot be overridden |

Vectors uploaded for a masked note are computed from the masked text, so the embedding cannot leak what the text hides. A person can always override a decision, except to share a credential.

**Photos.** A technician photographs what they see and says what it is (`FIELDMIND_PHOTOS=1`). The caption is an ordinary memory and decides the scope; the photo follows it. A third Qdrant Edge shard holds one CLIP vector per photo, so "the corroded flange" finds the photo as well as the caption. EXIF data (position, time, camera) is stripped before anything is stored, and only thumbnails ever leave the device.

![Working offline: a note is classified and masked on the device while search and answers keep running](docs/img/offline-work.png)

## How sync handles change

Every memory carries a revision number. The device remembers which cloud revision each local change was based on.

| Situation | Outcome |
|---|---|
| Cloud is still at the base revision | Upload succeeds (compare-and-swap) |
| Another device changed different fields | Merged automatically, field by field |
| Another device changed the same field | Held as a conflict. The person chooses: keep mine, accept theirs, keep both, or write a combined note. This works offline. |
| Deleted here, edited elsewhere | The edit wins |
| Deleted elsewhere, edited here | The edit wins and the memory is restored |
| Shared note changed to private | Withdrawn from the cloud and from other devices, kept here |
| Cloud collection lost or rebuilt | Devices notice the gap and upload again |
| Headquarters manual and a field note about the same asset | The field note links to the manual and never replaces it |
| Note edited while its upload is in flight | The edit is rebased onto the uploaded revision and stays queued; nothing is lost |
| One queued change keeps failing while the cloud is up | Set aside after five attempts so the rest of the queue drains; retried from the Sync tab |
| Only tags or kind changed | The payload travels without the vectors |
| Cloud unreachable for days | Devices on the same network exchange shareable notes directly (`FIELDMIND_PEERS`, one fleet token); the cloud settles revisions when it returns |

![A conflict: two devices changed the same note while offline](docs/img/conflict.png)

## Run it

### Any OS, one command

Needs Docker. About 2 GB of free disk space.

```bash
docker compose --profile demo up --build
```

This starts Qdrant Server and two edge devices in Linux containers:

- edge-a at http://localhost:8001
- edge-b at http://localhost:8002

The PIN is `2468`. The first start downloads the embedding, name and reranking models (about 260 MB). Click **Demo guide** in the top right of either dashboard.

In this mode answers are composed from the notes; the language model runs in the from-source setup below. The two containers are also configured as peers, so cutting both uplinks still lets them exchange notes.

To cut a device's network for real, rather than with the Network switch:

```bash
docker network disconnect fieldmind_uplink fieldmind-edge-a
docker network connect    fieldmind_uplink fieldmind-edge-a
```

### From source, on macOS, Linux or Windows

Needs Python 3.10 or newer; Docker for the cloud (without it the devices still run and sync to peers). About 3 GB of free disk space.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # Windows: py -m venv .venv ; .venv\Scripts\pip install -r requirements.txt
.venv/bin/python -m fieldmind start --pin 2468                          # or ./scripts/start.sh, or .\scripts\start.ps1 -Pin 2468
```

This starts Qdrant Server in Docker when it is local and present, then both devices as their own processes, and opens the dashboards. Without `--pin` each device asks you to choose one the first time.

| Command | What it does |
|---|---|
| `python -m fieldmind doctor` | Preflight: checks models, cloud, language model and devices, and says whether the demo can run with no internet |
| `python -m fieldmind stop` | Stop the devices |
| `python -m fieldmind reset-all` | Clean slate between demo runs: stop, erase both devices and the cloud collection, start again |
| `python -m fieldmind start --only edge-b --cloud-url http://192.168.1.20:6333` | One device, syncing with a cloud elsewhere |
| `python -m fieldmind start --lan` | Let phones on the same network open the dashboards (and install them) |
| `scripts/llm.ps1` | Windows, optional, one time: downloads the 2 GB answer model (needs Ollama installed) |

`scripts/start.sh`, `stop.sh`, `reset.sh` and the `.ps1` equivalents are thin wrappers around these. After the first start, the devices run with no internet.

### Two laptops

On the first laptop, start as above. Allow inbound TCP 6333 in Windows Firewall and note its address.

On the second laptop, install the same way, then run one device pointed at the first:

```bash
python -m fieldmind start --only edge-b --cloud-url http://192.168.1.20:6333
```

Pull the network cable or switch off Wi-Fi on either laptop to take it offline for real. To let the two devices keep exchanging notes with the cloud down, give each the other's address in `FIELDMIND_PEERS` and the same `FIELDMIND_PEER_TOKEN` (see `.env.example`).

### Qdrant Cloud

```bash
FIELDMIND_CLOUD_API_KEY=YOUR-KEY python -m fieldmind start --cloud-url https://YOUR-CLUSTER.cloud.qdrant.io:6333
# Windows: .\scripts\start.ps1 -CloudUrl https://YOUR-CLUSTER.cloud.qdrant.io:6333 -CloudApiKey YOUR-KEY
```

Or put the same values in a `.env` file (see `.env.example`). Sync and snapshot restore both send the API key.

### A real network cut, checked automatically

```powershell
docker compose --profile linux-device up -d --build      # edge-c at http://127.0.0.1:8003, PIN 2468
python scripts\linux_device_check.py
```

The script disconnects the device from the cloud's network, checks that it noticed by itself and kept working, reconnects it, and checks that its queue drained. The same image builds for a Raspberry Pi or an industrial gateway; qdrant-edge-py ships x86_64 and aarch64 wheels.

### Phones and tablets

`python -m fieldmind start --lan` binds the dashboards to the local network. Open the address on a phone and choose *Add to Home Screen*: the dashboard installs as an app and its shell stays available offline (no note text is ever cached by the browser). A device with no PIN yet prints a one-time setup code in its console (the `start` command repeats it); the first person to open the dashboard from another machine enters that code with the PIN they choose, so only someone who can read the device's console can claim it. On the device itself no code is needed. The Dictate button uses the browser's speech recognition.

## Demo script (5 minutes)

Open both dashboards side by side and unlock them. The **Demo guide** button lists these steps and ticks them off as you go.

1. **Cloud knowledge reaches the device.** On edge-a, click *Publish headquarters manuals to the cloud*. Eight manuals appear under *Received from cloud*.
2. **Go offline.** Turn the *Network* switch off on edge-a. The header changes to *Working offline*.
3. **Keep working.** Click *Load sample notes*. Watch the activity log: some notes are saved as shared, some as private, one as masked. *Waiting to sync* counts up.
4. **Search and ask with no network.** Search `is a vibration of 7.2 mm/s acceptable`. Results arrive in about a millisecond with 0 network calls. The answer is written on the device from a manual that was downloaded earlier, and cites it.
5. **See a decision being made.** Type `Suresh and Priya replaced the coupling on pump P-102, call 9876543210 if it trips` in the note box. The preview shows what the cloud would receive, with both names and the number masked.
6. **Reconnect.** Turn *Network* on. The queue drains, the gas leak note goes first.
7. **Prove privacy.** Open the *Cloud* tab. The health note and the password are not there. Names and the phone number are masked.
8. **Fleet learning.** On edge-b, search `pump bearing vibration`. The note from edge-a is there.
9. **Conflict.** Turn *Network* off on both. Open the same note on each and change the text differently. Turn edge-a on, then edge-b. edge-b shows *Needs a decision* on the *Sync* tab with both versions side by side.
10. **Evolving memory.** On edge-b, record `Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal`. Ask edge-a `what is the condition of pump P-102`. It answers with the new state.
11. **Snapshot restore.** On the *Sync* tab, click *Rebuild cloud replica*. The activity log reports a restore from a Qdrant Server snapshot. Private notes are untouched.

`python scripts/scenario.py 2468` runs the same story against the two running devices and checks all 30 steps. [docs/demo-video.md](docs/demo-video.md) is a shot-by-shot script for recording it.

## Tests

96 tests, run on every push by GitHub Actions against a real Qdrant Server. Most run two devices against a shared cloud and cover offline work, restart safety, privacy, name masking, priority, merging, conflicts, deletion, withdrawal, recovery, the PIN lock, the source check on generated answers, the reranker, policy learning, peer exchange, photos, sealing on disk, a failing outbox entry, an edit made mid-upload, a snapshot restore that fails midway and captures racing sync cycles. Three need a real Qdrant Server on `localhost:6333` (snapshot restore, real outage detection) and are skipped when none is running.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## Measured on the development laptop

Ryzen 7 7435HS, 32 GB RAM, RTX 3050 4 GB.

| Operation | Time |
|---|---|
| Hybrid search across both shards | 1 to 2 ms |
| Embedding a question | 4 to 6 ms |
| Policy decision for a new note, including name recognition | about 20 ms |
| Generated answer, model already loaded | 1 to 3 s |
| Replica restore from a snapshot (15 memories) | under 2 s |
| Device start (models already on disk) | about 2 s |

Disk: each device reserves about 430 MB, because every Qdrant Edge shard preallocates its write-ahead log and storage pages (a third shard, 215 MB, when photos are on). Models take about 260 MB (embedding, names, reranker), plus 590 MB for the optional photo models and 2 GB for the optional language model.

## Known limits

- Private text, the activity log and photo files are sealed with a key file in the device's data directory. That stops the data files being read when copied off the device without the key; it does not stop someone who can read the key file too. Use full-disk encryption for that.
- The PIN session is a cookie over plain HTTP, which is fine on the device itself and weak across a network. Put TLS in front before using `--lan` outside a demo.
- The answer model is small. The source check catches numbers, asset tags and verdicts that do not come from the cited note, not every possible misreading, so answers always list the notes they were based on.
- The name model is English and cased. It handles common Indian and Western names in our tests but will miss some. Addresses are caught only when they start with a house, flat or plot number.
- Dictation uses the browser's speech recognition. Where the browser cannot run it on the device, audio goes to the browser vendor's service and needs internet; the device itself never sends audio anywhere.
- Pull sync compares a slim index (revision and state) of every memory in the device's site on each cycle. That is fine for tens of thousands of memories, not millions.
- Snapshot restore downloads the whole shard before trimming to the device's site, so other sites' data touches the disk briefly.
- Photos are not inspected for faces or name plates; the caption decides the scope. Peers exchange notes but not photos.

## Project layout

```
fieldmind/
  store.py      Qdrant Edge shard wrapper, snapshot restore
  embedder.py   dense (ONNX) and BM25 embeddings
  ner.py        name recognition for masking
  reranker.py   cross-encoder that reorders search candidates
  vision.py     CLIP image and text embeddings, EXIF stripping
  photos.py     photos as memories: media shard, thumbnails, cloud view
  policy.py     what may leave the device, and what it learned from the person
  service.py    capture, edit, search, answer
  llm.py        optional on-device language model, with a source check on its answers
  journal.py    SQLite outbox, conflicts, activity, retention
  vault.py      seals private text, activity and photo files with the device key
  cloud.py      Qdrant Server client: compare-and-swap writes, payload patches, snapshots, media
  sync.py       push, pull, merge, conflict resolution, photos
  peers.py      device-to-device exchange when the cloud is out of reach
  schema.py     payload layout version and migrations
  auth.py       device PIN, sessions and lockout (kept across restarts)
  metrics.py    /metrics in the Prometheus text format
  doctor.py     preflight check
  launcher.py   start, stop and reset the demo devices on any OS
  api.py        HTTP API
  web/          dashboard (no external assets, loads offline, installable)
tests/          two-device behaviour tests, API tests, live-server tests
scripts/        start, stop, reset (sh and ps1), language model, scripted checks
docs/           deck, requirement mapping, pitch notes, video script, screenshots
```

## License

MIT. See [LICENSE](LICENSE).
