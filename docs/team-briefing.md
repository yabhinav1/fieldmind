# FieldMind: team briefing

Everything you need to understand the project and present it. Read top to bottom once (about 15 minutes), then keep the "Demo script" and "Questions judges may ask" sections open during the call.

- Repository (public): https://github.com/yabhinav1/fieldmind
- Deck: `docs/FieldMind-Compilers.pdf` in the repo (also as `.pptx`)
- Team: Compilers
- Problem statement: 03, "AI-Powered Edge Memory & Intelligence Platform" (Qdrant sponsor track)

---

## 1. The idea in one paragraph

Field technicians (plants, basements, remote sites) do their most useful work where there is no network. FieldMind is a memory that lives **on their device**: they write short notes about what they saw and did, the device remembers them, searches them in about a millisecond with no network, and can answer questions from them. The device **decides by itself what is safe to share** with the rest of the fleet (private, shared, or shared with names and phone numbers masked). When the network returns, it syncs with the central Qdrant Server **without ever overwriting another device's change**. It is built on **Qdrant Edge**, which is Qdrant's embedded vector database that runs inside the app with no server on the device.

If you only remember one sentence: *"A technician's most useful knowledge is created where the network is worst. FieldMind keeps it on the device, searches and answers from it offline, decides what may leave, and syncs intelligently."*

---

## 2. What the problem statement asked for, and how we answer each line

| They asked | We do |
|---|---|
| Searchable semantic memory on an edge device | Two Qdrant Edge shards inside one Python process (a third for photos). No database server on the device. Private text is sealed on disk with a device key. |
| Low-latency vector and hybrid search without network | Dense vectors (bge-small model, ONNX, runs on CPU; a multilingual option exists) plus Qdrant Edge's built-in BM25 keyword search. About 1 ms per search. A 23 MB cross-encoder reranks for answers. |
| Dynamically decide what stays local and what syncs | A policy engine classifies every note: **private** (stays), **shared**, or **shared with masking** (names, phone numbers, emails, vehicles, badges, addresses removed from the cloud copy; original stays on device). Credentials can never be shared. The person can override, and the device learns from those overrides. |
| Intermittent connectivity, keep operating offline | Every change goes into a durable "outbox" (SQLite) first. It survives restarts and replays when the link returns, urgent notes first. A change that keeps failing is set aside so it never blocks the queue. With the cloud gone, devices on the same network exchange notes directly. |
| Sync between edge devices and Qdrant Server | Uploads are compare-and-swap writes (never overwrite blindly). Small downloads use a diff of ids and revisions. A device that is far behind restores its whole replica from a Qdrant Server **shard snapshot** in one download. |
| Evolving memory, updates, conflicting information | A follow-up note replaces the earlier belief ("bearing replaced" supersedes "bearing vibration high"). Concurrent edits to different fields merge automatically. A real clash is held for a person to decide, with both versions side by side. |
| User-facing interface for memory, search, sync status, activity | A dashboard with Work, Memory, Sync and Cloud tabs, a live activity log, and a "Demo guide" button. Locked behind a PIN (the first visitor chooses it with a setup code from the console). Installable on a phone; dictation; photo capture; shift report; encrypted backup. |
| A meaningful edge-to-cloud AI workflow | Headquarters publishes manuals to the cloud, devices carry them offline, field notes and photos flow back up filtered by the policy, and a small language model on the device answers questions using both, with citations checked against the sources. |

Full mapping with code files and test names: `docs/requirements.md`.

---

## 3. How it works (enough to explain the architecture slide)

```
Edge device (one Python process)                         Cloud
  Dashboard + API (PIN locked)                        Qdrant Server
  Policy engine: patterns + name model + classifier   collection "fleet_memory"
  Qdrant Edge:                                            shared notes
     local shard   = written on this device (private too)  masked notes
     replica shard = copy of cloud knowledge               headquarters manuals
  Journal (SQLite): outbox, conflicts, activity log
  Embeddings: bge-small (dense) + BM25 (keywords)    push: compare-and-swap  -->
  Answer model: llama3.2 3B via Ollama (optional)    <-- pull: diff, or snapshot if far behind
```

Key design points you may be asked about:

**Two shards, not one.** The *local shard* holds everything this device wrote, including private notes. The *replica shard* is a disposable copy of cloud knowledge. Because they are separate, the replica can be wiped and rebuilt from a snapshot at any time without touching a single private note.

**Search is "federated".** Each shard is searched separately for meaning (cosine) and keywords (BM25). Those raw scores are comparable across shards, so we combine them into one relevance score in the app. (If we had used rank-based fusion inside each shard, the best hit of an almost-empty shard would tie with the best hit of a full one.)

**Policy engine, three signals, all on the device, about 20 ms:**
1. Pattern detectors for passwords/API keys, phone numbers, emails, ID numbers.
2. A name recognition model (small BERT, ONNX, ~105 MB) that finds people's names even without a title in front.
3. A semantic classifier that compares the note with example sentences per category (safety hazard, equipment fault, procedure, routine reading, personal/health, people matter, personal note) using the same embedding model as search.

Outcomes: safety and equipment notes are shared (safety is marked urgent and sent first), personal/health/HR notes stay private, credentials always stay private even if the user asks to share. A useful note that also contains a name becomes "shared with masking". The vector uploaded for a masked note is computed from the masked text, so the embedding cannot leak the name.

**Sync and conflicts.** Every memory has a revision number; the device remembers which cloud revision each of its changes was based on. An upload only succeeds if the cloud is still at that revision (a conditional upsert in Qdrant). If another device changed it meanwhile: fields that differ are merged automatically; the same field changed by both is a conflict and is shown to a person: keep mine, accept theirs, keep both, or combine. Deletes use tombstones. A shared note made private again is withdrawn from the cloud and from other devices. If the cloud collection is wiped, devices notice and upload again.

**Snapshot vs diff.** Everyday changes travel as a small diff (ids and revisions, then only the changed points). A device that is far behind (200+ memories, or when "Rebuild cloud replica" is clicked) downloads one Qdrant Server shard snapshot, unpacks it straight into its replica shard, trims it to its own site, and merges it to one segment (each segment preallocates about 215 MB on the device, so this matters).

**Answers.** Search finds the relevant notes; a 3B language model on the device (llama3.2 via Ollama) phrases a short answer with citations like [1]. Every number in the generated answer is checked against the note it cites; if a number is not there, the answer is thrown away and the device quotes the notes instead. Without the model, answers are always composed from the notes. Either way, zero network calls.

---

## 4. Numbers you can quote (all measured on our laptop, Ryzen 7, RTX 3050)

| What | Value |
|---|---|
| Hybrid search across both shards | about 1 ms |
| Network calls to search or answer | 0 |
| Policy decision for a note (with name recognition) | about 20 ms |
| Generated answer, model loaded | 1 to 3 seconds |
| Replica restore from a snapshot (15 memories) | under 2 seconds |
| Automated tests | 57, run on every push by GitHub Actions on Linux against a real Qdrant Server |
| Live end-to-end checks across two devices | 30, all passing |
| Disk per device | about 430 MB (Qdrant Edge preallocates storage), plus 235 MB of models, plus 2 GB if the language model is installed |

Also true and worth saying: the same code was run on Linux in a container with its network **physically disconnected** (Docker network disconnect, not a switch in the UI). The device noticed on its own, kept working, and drained its queue after reconnecting.

---

## 5. Running it yourself before the call

**Easiest, any OS (Docker only):**

```
git clone https://github.com/yabhinav1/fieldmind
cd fieldmind
docker compose --profile demo up --build
```

Then open http://localhost:8001 (edge-a) and http://localhost:8002 (edge-b). Each device asks you to choose a PIN the first time (setup code in the compose output). First start downloads about 260 MB of models. In this mode answers are composed from the notes (no language model in the containers); everything else is identical. Click **Demo guide** in the top right of the dashboard: it lists 8 steps and ticks them off as you do them.

**Windows with the language model** (how it runs on Abhinav's laptop): see "Run it" in the README. Before a demo there, run `python -m fieldmind doctor`; every line should say `ok`.

**Scripted proof:** `python scripts/scenario.py <PIN>` walks both devices through the whole story and checks 30 steps. Good to run on screen if a judge asks "does it really work?".

---

## 6. Demo script (5 minutes, two dashboards side by side)

Say the words in italics, or your own version of them.

1. **Headquarters publishes.** On edge-a click *Publish headquarters manuals to the cloud*. "Received from cloud" becomes 8.
   *"Headquarters publishes manuals to Qdrant Server. The device pulls them into a local replica shard."*
2. **Go offline.** Turn the **Network** switch off on edge-a. Header turns amber: Working offline.
   *"Now the technician walks into the plant and loses signal."*
3. **Keep working.** Click *Load sample notes*. Watch the activity log.
   *"They keep recording. Each note is classified on the device: shared, private, or shared with personal details masked. Waiting to sync counts up."*
4. **Search and ask offline.** Search `is a vibration of 7.2 mm/s acceptable`. Point at the timing line, then the answer.
   *"Search takes about a millisecond and makes zero network calls. The answer is written by a small model on the device from a manual that came from the cloud earlier, and it cites it. Every number in it was checked against the source."*
5. **Watch a decision.** Type `Suresh and Priya replaced the coupling on pump P-102, call 9876543210 if it trips`. Point at *What the cloud receives*.
   *"The fleet should know about the coupling. It should not get two names and a phone number. The original stays here."*
6. **Reconnect.** Turn **Network** on. Open the Sync tab as the queue drains.
   *"Signal returns. The queue drains by priority, so the gas leak note goes first."*
7. **Prove privacy.** Open the Cloud tab.
   *"This is what Qdrant Server actually holds. No health note, no password, names masked."*
8. **Fleet learning.** On edge-b search `pump bearing vibration`.
   *"The second device now knows what the first one saw."*
9. **Conflict.** Easiest: on edge-b open the Demo guide and click **Stage a conflict**. By hand: both devices Network off, open the same shared note on each (for example the V-17 valve note) and change the text differently, turn edge-a on, then edge-b. On edge-b open the Sync tab.
   *"Two technicians changed the same note while offline. The second to reconnect does not overwrite the first. It shows both versions and a person decides."* Click **Keep mine**.
10. **Evolving memory.** On edge-b record `Pump P-102 bearing replaced, vibration now 1.8 mm/s, normal`. On edge-a ask `what is the condition of pump P-102`.
    *"The follow-up replaced the earlier belief. The answer reflects the new state."*
11. **Snapshot restore.** Sync tab, click **Rebuild cloud replica**. Point at the activity log line.
    *"A device that falls far behind restores its replica from a Qdrant Server snapshot in one download, without touching its private notes."*

If something breaks: the Network switch only simulates loss on that device, so flipping it back on fixes most things. If a dashboard says "Device not responding", the process is down; restart it. If the answer box never appears, the language model is not running; answers still work, composed from notes.

---

## 7. Questions judges may ask (with answers)

**Why two shards instead of one?**
The local shard holds everything this device wrote, including private notes. The replica is a disposable copy of the cloud. Keeping them apart means the replica can be restored from a snapshot or wiped with zero risk to private data.

**Why not use Qdrant's built-in fusion for hybrid search?**
We do use Edge's dense and BM25 search in each shard, but fuse in the app, because rank fusion inside one shard cannot see the other shard. Raw cosine and BM25 scores are comparable across shards, so we combine those.

**How exactly do you use snapshots?**
A device that is 200 or more memories behind, or is asked to rebuild, downloads one shard snapshot from Qdrant Server and unpacks it into its replica shard with `EdgeShard.unpack_snapshot`. Then it deletes other sites' data and tombstones and merges to one segment. Everyday changes use a diff instead, because a diff lets a device skip data it does not subscribe to.

**Why not partial snapshots?**
They keep an edge shard identical to the server shard. Our replica is deliberately not identical: it is trimmed to the device's site. A diff fits that better.

**What stops two devices overwriting each other?**
Every memory has a revision. An upload is a conditional upsert that only applies if the cloud is still at the revision the device last saw. If not, we do a three-way merge per field. Only a true clash on the same field goes to a person.

**How is "what may leave the device" decided?**
Three signals, all local: pattern detectors for credentials and contact details, a BERT model for people's names, and a classifier that compares the note with example sentences using the search embedding model. Credentials can never be shared, even if the user asks.

**Could the embedding leak a masked name?**
No. For a masked note, the vectors we upload are computed from the masked text.

**Can the language model make things up?**
A 3B model can, and in testing it did once: it took an alarm limit from a manual and reported it as a reading on a motor. So every generated answer goes through a source check. Each number in a sentence must appear in the note that sentence cites; otherwise the answer is discarded and the notes are quoted instead. Sources are always listed under the answer.

**Is the language model required?**
No. Without it, answers are composed from the retrieved notes. With it, a 3B model phrases the answer and cites sources. Either way there is no cloud call.

**What happens if the cloud is wiped?**
Devices notice that memories they already delivered are missing and upload them again.

**What does it cost on the device?**
About 430 MB of disk for the two shards, 235 MB for the models, and 2 GB more if the language model is installed. Search and policy run on the CPU.

**Does it run on real edge hardware?**
It runs in a Linux container today, and qdrant-edge-py ships aarch64 wheels, so the same code runs on a Raspberry Pi or an industrial gateway. We have not tested on a Pi.

---

## 8. What is NOT done (say it if asked; never claim otherwise)

- Notes are not encrypted on disk. The PIN protects the dashboard and API, not the files.
- The PIN session is a cookie over plain HTTP. Fine on the device itself, weak across a network.
- The name model is English and cased. It will miss some names and does not detect addresses.
- Pull sync lists every id in the device's site each cycle: fine for tens of thousands of memories, not millions.
- Snapshot restore downloads the whole shard before trimming, so other sites' data touches the disk briefly.
- Not tested on a real Raspberry Pi or against a real Qdrant Cloud cluster (the API-key path was tested against a local server that requires a key).

---

## 9. Words to use and avoid

Use: *on the device*, *offline*, *zero network calls*, *decides what may leave*, *nothing is overwritten*, *a person decides*, *snapshot*, *compare-and-swap*.

Avoid: *AI magic*, *100% accurate*, *fully secure*, *production ready*. Say *prototype* if asked about maturity, and point to the 57 tests and CI.

---

## 10. Files worth opening during the call

| File | Why |
|---|---|
| `README.md` | Overview, GIF walkthrough, run instructions |
| `docs/FieldMind-Compilers.pdf` | The deck |
| `docs/requirements.md` | Problem statement line by line, with code and tests |
| `docs/pitch.md` | Pitch notes and Q&A |
| `fieldmind/policy.py` | The "what may leave" logic, if a judge wants to see code |
| `fieldmind/sync.py` | Compare-and-swap, merge, conflicts, snapshot restore |
| `tests/test_sync.py` | Two devices and a cloud, every sync behaviour as a test |
