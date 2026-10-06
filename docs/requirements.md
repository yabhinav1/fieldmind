# Problem statement, line by line

Each goal from Problem Statement 03, where it is implemented, how it is proven, and where to see it.

| # | Goal | Implemented in | Proven by | See it in the demo |
|---|---|---|---|---|
| 1 | Maintain searchable semantic memory directly on an edge device | [store.py](../fieldmind/store.py): two Qdrant Edge shards inside the app process, a third for photos ([photos.py](../fieldmind/photos.py)). [embedder.py](../fieldmind/embedder.py): dense vectors from an ONNX model on the CPU. [vault.py](../fieldmind/vault.py): private text sealed on disk. | `test_works_fully_offline`, `test_queued_changes_survive_restart`, `test_private_text_and_the_activity_log_are_sealed_on_disk`, `test_a_photo_follows_its_caption_and_is_found_by_what_it_shows` | Work tab, with the Network switch off |
| 2 | Perform low-latency vector and hybrid search without network access | [service.py](../fieldmind/service.py) `search`: dense plus Qdrant Edge BM25, fused by score across both shards, plus a photo signal; [reranker.py](../fieldmind/reranker.py) reorders the best candidates on request and for answers | `test_strong_meaning_beats_a_weak_keyword`, `test_cloud_knowledge_is_searchable_on_the_device`, `test_search_can_rerank_and_reports_it`. Scenario step 2 asserts 0 network calls. | The timing line under the search box: about 1 ms, 0 network calls; the Rerank toggle |
| 3 | Dynamically decide what information should remain local and what should be synchronized | [policy.py](../fieldmind/policy.py): credential, contact, identity, vehicle, badge and address patterns, a name recognition model ([ner.py](../fieldmind/ner.py)) and a semantic classifier that also flags mixed notes. Three outcomes: private, shared, shared with details masked. The policy learns from the person's overrides. | `tests/test_policy.py`, `test_private_memory_never_reaches_the_cloud`, `test_masked_copy_goes_up_original_stays`, `test_the_policy_learns_from_overrides_and_remembers_across_restarts` | The preview under the note box, then the Cloud tab; "Learned choices" in the device panel |
| 4 | Support intermittent connectivity and continue operating offline | [journal.py](../fieldmind/journal.py): a durable outbox in SQLite that parks a change that keeps failing. [sync.py](../fieldmind/sync.py): link monitor and background loop. [peers.py](../fieldmind/peers.py): devices on the same network exchange shareable notes when the cloud is gone. | `test_queued_changes_survive_restart`, `test_real_outage_is_detected_without_the_switch`, `test_a_change_that_keeps_failing_is_set_aside_and_the_queue_drains`, `test_shareable_notes_cross_the_room_without_a_cloud`, `scripts/linux_device_check.py` (a real network disconnect) | Network switch off, keep working, switch on; "Nearby devices" on the Sync tab |
| 5 | Synchronize data between edge devices and Qdrant Server when connectivity returns | [sync.py](../fieldmind/sync.py) push and pull. [cloud.py](../fieldmind/cloud.py): compare-and-swap upserts confirmed by a per-write id, payload-only patches when the text did not change, manifest diff, shard snapshot download, a media collection for photo thumbnails. | `test_shared_memory_reaches_other_devices`, `test_urgent_memories_are_sent_first`, `test_an_edit_that_keeps_the_text_sends_no_vectors`, `test_far_behind_device_restores_replica_from_a_snapshot`, `test_photos_travel_with_their_memory_and_private_ones_stay` | Sync tab: the queue drains, urgent first. Rebuild cloud replica uses a snapshot. |
| 6 | Handle evolving local memory, updates, and conflicting information | [service.py](../fieldmind/service.py): follow-ups supersede earlier notes (the person can veto it in the preview), duplicates are skipped. [sync.py](../fieldmind/sync.py): three-way merge, held conflicts, an edit made mid-upload is rebased rather than lost. [schema.py](../fieldmind/schema.py): versioned payloads. | `test_follow_up_replaces_earlier_belief`, `test_the_person_can_keep_the_earlier_note_current`, `test_duplicates_are_not_stored_twice`, `test_edits_to_different_fields_merge_automatically`, `test_clashing_edits_wait_for_a_decision`, `test_conflict_keep_mine`, `test_conflict_accept_theirs`, `test_conflict_keep_both`, `test_an_edit_made_during_the_upload_is_not_lost`, `test_resolving_against_a_stale_conflict_raises_a_fresh_one_instead_of_overwriting`, `test_memories_from_a_newer_fieldmind_are_held_back` | Sync tab, Needs a decision |
| 7 | Provide a user-facing interface to inspect device memory, search results, synchronization status, and system activity | [web/](../fieldmind/web/): Work, Memory, Sync and Cloud tabs plus a live activity log; installable, with dictation and photo capture. [api.py](../fieldmind/api.py), with `/healthz` and `/metrics` for operators. | `tests/test_api.py`; the dashboard was also driven in a real browser | The dashboard itself. Demo guide button, top right. |
| 8 | Demonstrate a meaningful edge-to-cloud AI workflow, rather than simply running a local vector database | Headquarters manuals flow down to every device; field notes and photos flow up, filtered by the policy; a device answers questions from both with a local language model ([llm.py](../fieldmind/llm.py)), and checks every number, asset tag and verdict in the answer against the note it cites. | `scripts/scenario.py`: 30 checks across two live devices and a real Qdrant Server. `tests/test_llm.py` for the source check. | Ask "is a vibration of 7.2 mm/s acceptable" while offline: the answer cites a manual that came from the cloud |

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
- **Checked answers.** A generated answer that reports a number, an asset or a verdict its cited note does not support is discarded, and the notes are quoted instead.
- **Reranking.** A 23 MB cross-encoder reads the question with each candidate, on the device.
- **A policy that learns.** Overrides are remembered as vectors and scopes, never text, and similar notes follow them.
- **Device lock.** Every API route sits behind a PIN; sessions and the lockout counter survive restarts; the PIN can be changed.
- **Sealed on disk.** Private text, the activity log and photo files are encrypted with a key kept on the device.
- **Peers.** Devices on the same network learn from each other when the cloud is out of reach.
- **Photos.** Captured with a caption, searchable by what they show, EXIF stripped, thumbnails only.
- **Operable.** `/healthz`, Prometheus `/metrics`, a failing change never blocks the queue, bounded history, one `start` command on any OS.
- **Runs on Linux.** The same code runs in a container, and on aarch64 boards such as a Raspberry Pi.
