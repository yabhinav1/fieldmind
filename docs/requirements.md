# Problem statement, line by line

Each goal from Problem Statement 03, where it is implemented, how it is proven, and where to see it.

| # | Goal | Implemented in | Proven by | See it in the demo |
|---|---|---|---|---|
| 1 | Maintain searchable semantic memory directly on an edge device | [store.py](../fieldmind/store.py): two Qdrant Edge shards inside the app process. [embedder.py](../fieldmind/embedder.py): dense vectors from an ONNX model on the CPU. | `test_works_fully_offline`, `test_queued_changes_survive_restart` | Work tab, with the Network switch off |
| 2 | Perform low-latency vector and hybrid search without network access | [service.py](../fieldmind/service.py) `search`: dense plus Qdrant Edge BM25, fused by score across both shards | `test_strong_meaning_beats_a_weak_keyword`, `test_cloud_knowledge_is_searchable_on_the_device`. Scenario step 2 asserts 0 network calls. | The timing line under the search box: about 1 ms, 0 network calls |
| 3 | Dynamically decide what information should remain local and what should be synchronized | [policy.py](../fieldmind/policy.py): credential and contact patterns, a name recognition model ([ner.py](../fieldmind/ner.py)) and a semantic classifier. Three outcomes: private, shared, shared with details masked. | `tests/test_policy.py` (20 cases), `test_private_memory_never_reaches_the_cloud`, `test_masked_copy_goes_up_original_stays` | The preview under the note box, then the Cloud tab |
| 4 | Support intermittent connectivity and continue operating offline | [journal.py](../fieldmind/journal.py): a durable outbox in SQLite. [sync.py](../fieldmind/sync.py): link monitor and background loop. | `test_queued_changes_survive_restart`, `test_real_outage_is_detected_without_the_switch`, `scripts/linux_device_check.py` (a real network disconnect) | Network switch off, keep working, switch on |
| 5 | Synchronize data between edge devices and Qdrant Server when connectivity returns | [sync.py](../fieldmind/sync.py) push and pull. [cloud.py](../fieldmind/cloud.py): compare-and-swap upserts, manifest diff, shard snapshot download. | `test_shared_memory_reaches_other_devices`, `test_urgent_memories_are_sent_first`, `test_far_behind_device_restores_replica_from_a_snapshot` | Sync tab: the queue drains, urgent first. Rebuild cloud replica uses a snapshot. |
| 6 | Handle evolving local memory, updates, and conflicting information | [service.py](../fieldmind/service.py): follow-ups supersede earlier notes, duplicates are skipped. [sync.py](../fieldmind/sync.py): three-way merge and held conflicts. | `test_follow_up_replaces_earlier_belief`, `test_duplicates_are_not_stored_twice`, `test_edits_to_different_fields_merge_automatically`, `test_clashing_edits_wait_for_a_decision`, `test_conflict_keep_mine`, `test_conflict_accept_theirs`, `test_conflict_keep_both` | Sync tab, Needs a decision |
| 7 | Provide a user-facing interface to inspect device memory, search results, synchronization status, and system activity | [web/](../fieldmind/web/): Work, Memory, Sync and Cloud tabs plus a live activity log. [api.py](../fieldmind/api.py). | `tests/test_api.py`; the dashboard was also driven in a real browser | The dashboard itself. Demo guide button, top right. |
| 8 | Demonstrate a meaningful edge-to-cloud AI workflow, rather than simply running a local vector database | Headquarters manuals flow down to every device; field notes flow up, filtered by the policy; a device answers questions from both with a local language model ([llm.py](../fieldmind/llm.py)), and checks every number in the answer against the note it cites. | `scripts/scenario.py`: 30 checks across two live devices and a real Qdrant Server. `tests/test_llm.py` for the source check. | Ask "is a vibration of 7.2 mm/s acceptable" while offline: the answer cites a manual that came from the cloud |

## Expected outcome

> A complete edge-native AI product that can remember, retrieve, operate offline, and synchronize intelligently when connected.

| Word | What it means here |
|---|---|
| Remember | Notes persist in Qdrant Edge and survive restarts. A follow-up replaces an outdated belief instead of piling up beside it. |
| Retrieve | Hybrid search in about a millisecond, and cited answers from a 3B model, both on the device. |
| Operate offline | Every feature except sync itself works with no network. Verified with a real disconnect. |
| Synchronize intelligently | Priority order, privacy policy, compare-and-swap, automatic merge, snapshot restore for devices that are far behind. |

## Beyond the brief

- **Masked sharing.** A note that mixes a useful fault with a person's name is shared with the name removed, and the uploaded vector is computed from the masked text.
- **Withdrawal.** A shared note can be made private again; it is removed from the cloud and from other devices.
- **Self-healing.** If the cloud collection is lost, devices notice and upload again.
- **Checked answers.** A generated answer that reports a number its cited note does not contain is discarded, and the notes are quoted instead.
- **Device lock.** Every API route sits behind a PIN.
- **Runs on Linux.** The same code runs in a container, and on aarch64 boards such as a Raspberry Pi.
