# FieldMind

Offline-first memory for field technicians, built on **Qdrant Edge**.

A technician records what they see and do. The device remembers it, searches it in about a millisecond with no network, decides by itself what is safe to share, and syncs with the rest of the fleet through Qdrant Server whenever a connection exists.

Problem statement 03: AI-Powered Edge Memory and Intelligence Platform.

## What it does

| Goal from the problem statement | How FieldMind does it |
|---|---|
| Searchable semantic memory on the device | Two Qdrant Edge shards run inside the app process. No database server on the device. |
| Low-latency vector and hybrid search without network | Dense vectors (bge-small, ONNX, on CPU) plus Qdrant Edge's built-in BM25. About 1 ms per search. |
| Decide what stays local and what syncs | A policy engine classifies every note on the device. Private notes stay, useful notes are shared, and notes that mix both are shared with personal details masked. |
| Work through intermittent connectivity | Every change goes to a durable outbox first. It survives restarts and replays when the link returns, urgent items first. |
| Sync with Qdrant Server | Uploads are compare-and-swap writes. Downloads compare a manifest of ids and revisions and fetch only what changed. |
| Evolving memory, updates and conflicts | Follow-up notes replace earlier beliefs. Concurrent edits merge field by field. True clashes wait for a person and nothing is overwritten. |
| Interface to inspect everything | Dashboard with memory, search results, sync queue, conflicts and a live activity log. |
| A meaningful edge-to-cloud workflow | Headquarters publishes manuals to the cloud, devices carry them offline, field notes flow back to the fleet. |

## Architecture

```
  Edge device (one process)                          Cloud
 ┌───────────────────────────────────────┐        ┌──────────────────────┐
 │ Dashboard + API (FastAPI)             │        │ Qdrant Server        │
 │                                       │        │ collection:          │
 │ Policy engine ── private / shared /   │        │   fleet_memory       │
 │                  shared with masking  │        │                      │
 │                                       │  push  │  shared notes        │
 │ Qdrant Edge                           │ ─────► │  masked notes        │
 │  ├─ local shard    written here       │  CAS   │  headquarters        │
 │  └─ replica shard  copy of the cloud  │ ◄───── │  manuals             │
 │                                       │  pull  │                      │
 │ Embeddings  bge-small (ONNX) + BM25   │  diff  └──────────────────────┘
 │ Journal     SQLite: outbox, conflicts │                  ▲
 │ Sync engine background loop           │                  │
 └───────────────────────────────────────┘          other edge devices
```

**Why two shards.** The local shard holds what this device wrote, including everything private. The replica shard is a disposable copy of cloud knowledge. The replica can be wiped and rebuilt at any time without touching a single private note.

**Why search is federated.** Each shard is asked for semantic and keyword matches separately. Raw cosine and BM25 scores are comparable across shards, so every memory gets one score per signal and these are combined into one relevance score. Rank-only fusion per shard would let a weak match in a small shard outrank a strong match in a large one.

## How sharing is decided

The policy runs on the device in about 7 ms and combines two kinds of evidence.

1. **Pattern detectors** for credentials, phone numbers, emails, identity numbers and named people.
2. **A semantic classifier** that compares the note with example sentences per category, using the same local embedding model as search.

| Example note | Decision |
|---|---|
| Pump P-102 bearing vibration high at 7.2 mm/s | Shared |
| Smell of gas near compressor K-4 | Shared, urgent, sent first |
| Motor M-9 overheating, call Operator Anil Sharma on 9876543210 | Shared with the name and number masked. The original stays on the device. |
| Technician Ravi Kumar reported chest pain | Private |
| SCADA panel login password is ... | Private, and cannot be overridden |

Vectors uploaded for a masked note are computed from the masked text, so the embedding cannot leak what the text hides. A person can always override a decision, except to share a credential.

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

## Run it

Requirements: Windows, Python 3.10 or newer, Docker Desktop. About 1 GB of free disk space.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\start.ps1
```

This starts Qdrant Server in Docker and two devices, then opens both dashboards:

- edge-a at http://127.0.0.1:8001
- edge-b at http://127.0.0.1:8002

The embedding model (about 130 MB) downloads once on first start. After that the devices start and run with no internet.

Stop with `.\scripts\stop.ps1`. Add `-Cloud` to stop Qdrant Server too.

To start clean:

```powershell
.\scripts\stop.ps1
.\.venv\Scripts\python.exe -m fieldmind reset --device edge-a --cloud
.\.venv\Scripts\python.exe -m fieldmind reset --device edge-b
```

To use Qdrant Cloud instead of Docker, set `FIELDMIND_CLOUD_URL` and `FIELDMIND_CLOUD_API_KEY` before starting.

## Demo script (5 minutes)

Open both dashboards side by side.

1. **Cloud knowledge reaches the device.** On edge-a, click *Publish headquarters manuals to the cloud*. Eight manuals appear under *Received from cloud*.
2. **Go offline.** Turn the *Network* switch off on edge-a. The header changes to *Working offline*.
3. **Keep working.** Click *Load sample notes*. Watch the activity log: some notes are saved as shared, some as private, one as masked. *Waiting to sync* counts up.
4. **Search with no network.** Search `which machine is running too hot`. Results arrive in about a millisecond with 0 network calls, and include a headquarters manual that was downloaded earlier.
5. **See a decision being made.** Type `Motor M-9 overheating, call Operator Anil Sharma on 9876543210` in the note box. The preview shows what the cloud would receive.
6. **Reconnect.** Turn *Network* on. The queue drains, the gas leak note goes first.
7. **Prove privacy.** Open the *Cloud* tab. The health note and the password are not there. The phone number is masked.
8. **Fleet learning.** On edge-b, search `pump bearing vibration`. The note from edge-a is there.
9. **Conflict.** Turn *Network* off on both. Open the same note on each and change the text differently. Turn edge-a on, then edge-b. edge-b shows *Needs a decision* on the *Sync* tab with both versions side by side.
10. **Evolving memory.** On edge-b, record `Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal`. Ask edge-a `what is the condition of pump P-102`. It answers with the new state and what it replaced.

`python scripts/scenario.py` runs the same story against the two running devices and checks every step.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

36 tests run two devices against a shared cloud and cover offline work, restart safety, privacy, masking, priority, merging, conflicts, deletion, withdrawal and recovery.

## Measured on the development laptop

Ryzen 7 7435HS, 32 GB RAM, no GPU used.

| Operation | Time |
|---|---|
| Hybrid search across both shards | about 1 ms |
| Embedding a question | 4 to 6 ms |
| Policy decision for a new note | about 7 ms |
| Device start (model already on disk) | about 2 s |

Each device reserves about 430 MB of disk, because every Qdrant Edge shard preallocates its write-ahead log and storage pages.

## Optional: answers from a local language model

By default, answers are composed from the memories themselves. To use a local model through Ollama instead, set `FIELDMIND_OLLAMA_MODEL` (for example `llama3.2:3b`) before starting. If Ollama is not reachable the device falls back to composed answers.

## Project layout

```
fieldmind/
  store.py      Qdrant Edge shard wrapper
  embedder.py   dense (ONNX) and BM25 embeddings
  policy.py     what may leave the device
  service.py    capture, edit, search, answer
  journal.py    SQLite outbox, conflicts, activity
  cloud.py      Qdrant Server client with compare-and-swap writes
  sync.py       push, pull, merge, conflict resolution
  api.py        HTTP API
  web/          dashboard (no external assets, loads offline)
tests/          two-device behaviour tests
scripts/        start, stop and the scripted scenario
```
