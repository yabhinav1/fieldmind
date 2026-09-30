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

**Is the language model required?**
No. Without it, answers are composed from the retrieved notes. With it, a 3B model on the device phrases the answer and cites its sources. Either way there is no cloud call.

**What happens if the cloud is wiped?**
Devices notice that memories they already delivered are missing and upload them again.

**What does it cost on the device?**
About 430 MB of disk per device for the two shards, 235 MB for the embedding and name models, and 2 GB more if the language model is installed. Search and policy run on the CPU.

## What is honestly not done

- Notes are not encrypted at rest.
- The PIN session runs over plain HTTP.
- Name masking is English only and will miss some names.
- Pull sync lists every id in the device's site each cycle, which suits tens of thousands of memories, not millions.
