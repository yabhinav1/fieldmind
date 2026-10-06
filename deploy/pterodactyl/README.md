# Run FieldMind 24/7 on a Pterodactyl panel (HidenCloud and similar)

A panel server runs one process on one port and cannot run Docker, so the layout is:

- **One panel server per device.** Two servers (`edge-a`, `edge-b`) give the full two-device demo.
- **Qdrant Cloud as the cloud.** The free 1 GB cluster is plenty. No Docker anywhere.

What you need: a free [Qdrant Cloud](https://cloud.qdrant.io) account and a panel server with a **Python 3.11 or 3.12** image, about **2 GB of disk** and **1 GB of RAM** (1.5 GB is comfortable). The server must allow outbound internet for the first start (models and packages) and for syncing.

## 1. The cloud

1. Sign up at https://cloud.qdrant.io and create a **free cluster**.
2. Copy the cluster **URL** (`https://xxxx.cloud.qdrant.io:6333`) and create an **API key**.

## 2. The device

1. Create a server on the panel. Choose the **Python** egg (any generic Python 3.11/3.12 egg works). Note the port the panel allocated to it.
2. Open the server's **Files** tab and upload `fieldmind-pterodactyl.zip`, then use **Unarchive** on it (or extract locally and drag the contents in). The server's root must contain `start.sh`, `.env`, `requirements.txt` and the `fieldmind/` folder.
3. Edit `.env` in the file manager: set `FIELDMIND_DEVICE`, `FIELDMIND_PIN`, `FIELDMIND_CLOUD_URL` and `FIELDMIND_CLOUD_API_KEY`. Save.
4. In **Startup**, set the startup command to:

   ```
   sh start.sh
   ```

   If the egg insists on a "main file" variable instead, point it at `start.sh` and make sure the command runs it with `sh`. The egg's own `pip install` step, if any, can stay; `start.sh` installs what is missing into `.venv` anyway.
5. **Start** the server. The first start installs the packages and downloads the models (about 260 MB); watch the console. Later starts take seconds.
6. Open `http://<node address>:<port>` from the server's **Network** tab, enter the PIN, click **Demo guide**.

Repeat for the second server with `FIELDMIND_DEVICE=edge-b` and `FIELDMIND_AUTHOR=technician-b`, same cloud URL and key.

## Optional: devices that talk to each other without the cloud

Put each server's public address in the other's `.env` and give both the same token:

```
FIELDMIND_PEERS=http://node.hidencloud.com:25566
FIELDMIND_PEER_TOKEN=some-shared-secret
```

With the cloud unreachable, the two still exchange shareable notes directly.

## Notes

- Everything lives in the server's persistent storage: `data/` (notes, outbox, device key) and `models/`. Deleting the server deletes the device's private notes; shared ones are in Qdrant Cloud.
- The dashboard is plain HTTP on the panel's port, like the laptop demo. The PIN protects it; do not put a real plant's data on a public address without TLS in front.
- `FIELDMIND_PHOTOS` stays off: the CLIP models add 590 MB and a third shard. Turn it on only on a plan with 4 GB of disk and 2 GB of RAM.
- If the console shows the process killed right after "loading models", the plan has too little RAM: set `FIELDMIND_RERANK=0` in `.env`, or pick a bigger plan.
- To reset a device, stop it and delete the `data/` folder in the file manager.
