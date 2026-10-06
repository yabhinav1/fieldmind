# Pitch notes

Deck: kept outside this repository. Replace `[Team name]` on slides 1 and 9 and `[Repository link]` on slide 9 before presenting.

## The pitch in one breath

A technician's most useful knowledge is created where the network is worst. FieldMind keeps that knowledge on the device in Qdrant Edge, searches and answers from it with no network, decides on the device what is safe to share, and syncs with the fleet through Qdrant Server without ever overwriting someone else's change.

## Slide order

1. Cover
2. The problem: no signal, mixed notes, drift
3. The product, shown offline
4. Architecture: one process on the device, one collection in the cloud
5. The device decides what leaves
6. When two devices disagree
7. What Qdrant does for us
8. Measured numbers
9. Demo moments and what is next

## Questions judges are likely to ask

**Why two shards instead of one?**
The local shard holds everything this device wrote, including private notes. The replica is a disposable copy of the cloud. Keeping them apart means the replica can be restored from a snapshot or wiped without any risk to private data.

**Why not use Qdrant's built-in fusion for hybrid search?**
We use Edge's dense and BM25 search in each shard, but fuse in the app. Rank fusion inside one shard cannot see the other shard, so the best hit of a nearly empty shard would tie with the best hit of a full one. Raw cosine and BM25 scores are comparable across shards, so we combine those.

**How do you use snapshots?**
A device that is 200 or more memories behind, or is asked to rebuild, downloads one shard snapshot from Qdrant Server and unpacks it into the replica shard with `EdgeShard.unpack_snapshot`. It then deletes other sites' data and tombstones, and merges to one segment. Everyday changes use a manifest diff of ids and revisions instead, because that lets a device skip data it does not subscribe to.

**Why not partial snapshots?**
They keep an edge shard identical to the server shard. Our replica is deliberately not identical: it is trimmed to the device's site. A diff is the better fit for that.

**What stops two devices overwriting each other?**
Every memory has a revision. An upload is a conditional upsert that only applies if the cloud is still at the revision the device last saw. If not, we do a three-way merge per field. Only a true clash on the same field goes to a person.

**How is "what may leave the device" decided?**
Three signals, all local: pattern detectors for credentials and contact details, a quantised BERT model for people's names, and a classifier that compares the note with example sentences using the search embedding model. Credentials can never be shared, even if the user asks.

**Could the embedding leak a masked name?**
No. For a masked note the vectors we upload are computed from the masked text.

**Can the language model make things up?**
A 3B model can, and in testing it did: it took an alarm limit from a manual and reported it as a reading on a motor. So every generated answer goes through a source check before it is shown. Each number and each asset tag in a sentence must appear in the note that sentence cites, and a sentence may not call something fine when its sources only call it faulty, or the reverse. If any check fails, the answer is thrown away and the device quotes the notes instead. The sources are always listed under the answer.

**How good is retrieval beyond the fused score?**
Fusion of cosine and BM25 is fast and good at recall. For answers, and for search when asked, a 23 MB cross-encoder (MiniLM, ONNX) reads the question together with each of the twenty best candidates and its verdict is blended in. It costs a few tens of milliseconds on a CPU and stays on the device.

**Does the policy ever learn?**
Yes, from the person. When a technician overrides a decision, the device keeps the note's vector and the chosen scope, never the text, and a later note at least 88% similar follows that choice. The policy also reports when a note reads as two kinds at once, say a hazard that is also about someone's injury, and flags it for a look. Credentials can never be learned into sharing.

**What if the cloud is gone for days?**
Devices on the same network still learn from each other. Each device offers the notes it may share, masked exactly as the cloud would get them, plus the cloud knowledge it already holds, behind one fleet token. A device without a cloud link pulls from its peers into its replica shard. Nothing from a peer touches the local shard, and once the cloud is back its copy takes over.

**What about photos?**
A photo is captured with a caption. The caption is an ordinary memory and decides the scope; the photo follows it. A third Qdrant Edge shard holds one CLIP vector per photo, so "the corroded flange" finds the photo as well as the caption. EXIF data (position, time, camera) is stripped, only thumbnails leave the device, and they travel in a second collection with their vector. It is optional because the model pair is 590 MB.

**What happens to a change that keeps failing?**
If the cloud is up but one queued change fails for its own reasons, it is set aside after five attempts so everything behind it, including urgent safety notes, still goes. The Sync tab shows it with a Retry button. A change that fails because the link dropped is simply retried next cycle.

**Is the language model required?**
No. Without it, answers are composed from the retrieved notes. With it, a 3B model on the device phrases the answer and cites its sources. Either way there is no cloud call.

**What happens if the cloud is wiped?**
Devices notice that memories they already delivered are missing and upload them again.

**What does it cost on the device?**
About 430 MB of disk per device for the two shards, 260 MB for the embedding, name and reranking models, and 2 GB more if the language model is installed. Photos add a third shard (215 MB) and 590 MB of models. Search and policy run on the CPU.

**Is anything protected on disk?**
The text of private notes, the unmasked original of masked notes, every activity-log line and every photo file are sealed with a key created on first start in the device's data directory (mode 0600, or wherever FIELDMIND_KEY_FILE points). Copying the shard and journal files off the device without the key yields no private text. Vectors are not sealed; they are needed for search and cannot be turned back into text.

## What is honestly not done

- Sealing protects against the data files being copied without the key file; someone who can read the key file too can read everything. Use full-disk encryption for that.
- The PIN session runs over plain HTTP. Put TLS in front before exposing a device beyond the demo network.
- Name masking is English only and will miss some names. Addresses are caught only when they start with a house, flat or plot number.
- Dictation uses the browser's speech recognition; where the browser cannot run it on the device, audio goes to the browser vendor's service and needs internet.
- Pull sync compares a slim index of every memory in the device's site each cycle, which suits tens of thousands of memories, not millions.
- Photos are not inspected for faces or name plates; the caption decides the scope. Peers exchange notes but not photos.
