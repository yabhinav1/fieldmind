# Host FieldMind on the internet for free

So anyone can open it from a browser, at any time, without your laptop being on. Uses two free services and no credit card. About 30 minutes.

Result: a device at `https://<you>-fieldmind-a.hf.space`, a second at `...-b.hf.space`, both syncing through a Qdrant Cloud cluster. Answers are composed from the notes (no language model in the free tier). Everything else is the same as the laptop demo.

## 1. The cloud: Qdrant Cloud (free)

1. Go to https://cloud.qdrant.io and sign up (free).
2. Create a **free cluster** (1 GB). Pick any region.
3. Copy the cluster **URL** (looks like `https://xxxx.aws.cloud.qdrant.io:6333`) and create an **API key**. Keep both.

## 2. The devices: Hugging Face Spaces (free)

Do this twice, once for `edge-a` and once for `edge-b`.

1. Go to https://huggingface.co and sign up (free).
2. **New Space**. Name it `fieldmind-a` (then `fieldmind-b`). SDK: **Docker**, template **Blank**. Hardware: **CPU basic** (free). Visibility: Public.
3. Upload the project files. Either:
   - **Web upload:** download the repo ZIP (https://github.com/yabhinav1/fieldmind/archive/refs/heads/main.zip), extract it, and drag the folder contents into the Space's **Files** tab (everything except `docs/`, `tests/`, `scripts/`, `.github/` can be skipped, but uploading all is fine). Or:
   - **Git:** `git clone https://github.com/yabhinav1/fieldmind`, then `git remote add space https://huggingface.co/spaces/<you>/fieldmind-a` and `git push space main`.
4. In the Space, open **Settings → Variables and secrets** and add:

   | Name | Value |
   |---|---|
   | `FIELDMIND_DEVICE` | `edge-a` (or `edge-b` for the second Space) |
   | `FIELDMIND_PIN` | `2468` (or any PIN you like) |
   | `FIELDMIND_CLOUD_URL` | your Qdrant Cloud URL |
   | `FIELDMIND_CLOUD_API_KEY` | your Qdrant Cloud API key (add this one as a **secret**) |
   | `FIELDMIND_OLLAMA_MODEL` | leave empty |
   | `FIELDMIND_PEERS` | the other Space's URL, for example `https://<you>-fieldmind-b.hf.space` (optional: lets the two devices exchange notes directly if the cloud is down) |
   | `FIELDMIND_PEER_TOKEN` | any shared string, the same on both Spaces (add as a **secret**) |

5. The Space builds (about 5 minutes) and starts. The first start downloads the models (about 260 MB, another minute). Then open the Space's URL, enter the PIN, click **Demo guide**.

The Dockerfile already serves on port 7860, which is what Spaces expect, so no other configuration is needed.

## Notes

- Free Spaces go to sleep after about 48 hours without visitors and wake up when someone opens the link (a minute). Open both links a few minutes before a demo.
- Free Space storage is wiped on restart. Notes are kept in Qdrant Cloud, so a restarted device gets the shared ones back on its first sync; private notes on a restarted free Space are lost, and so is the device key that seals them. Fine for a demo, not for real use.
- Leave `FIELDMIND_PHOTOS` unset on a free Space: the CLIP models add 590 MB to the first start and a third 215 MB shard.
- The two Spaces are two devices on the same site, exactly like the laptop demo: the conflict step, masked sharing and snapshot restore all work between them.
- To stop paying nothing for nothing: delete the Spaces and the cluster when you are done.
