"""Sample content for demonstrations."""

from __future__ import annotations

import time
import uuid

from .cloud import Cloud
from .embedder import Embedder

# Notes a technician might record during a shift. They exercise every policy outcome.
DEVICE_NOTES: dict[str, list[str]] = {
    "edge-a": [
        "Pump P-102 bearing vibration high at 7.2 mm/s, recommend replacement",
        "Smell of gas near compressor K-4, isolated the line and cleared the bay",
        "Valve V-17 leaking hydraulic fluid near the flange, gasket looks worn",
        "Daily round done, boiler B-2 pressure 6.1 bar, normal",
        "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting",
        "Technician Ravi Kumar reported chest pain during night shift, sent to clinic",
        "SCADA panel login password is Plant@2026",
        "Remind me to call home after shift and pick up my keys",
    ],
    "edge-b": [
        "Conveyor C-3 belt tracking drifting left, adjusted the tail pulley by 4 mm",
        "Cooling tower CT-1 fan gearbox oil level low, topped up 2 litres",
        "Exposed live cable found behind panel MCC-2, area barricaded",
        "Heat exchanger HX-5 outlet temperature 8 degrees above design, fouling suspected",
        "Supervisor was rude to the new trainee again today",
        "Air compressor K-4 drain valve stuck open, replaced the solenoid",
    ],
}
DEFAULT_NOTES = DEVICE_NOTES["edge-a"]

# Reference knowledge published by headquarters. Devices receive it through the cloud.
CLOUD_KNOWLEDGE: list[dict] = [
    {"asset": "P-102", "text": "Procedure for P-series pump bearing replacement: isolate and lock out the pump, drain the casing, remove the coupling guard, extract the bearing with a puller, fit the new bearing heated to 110 C, realign the coupling within 0.05 mm."},
    {"asset": None, "text": "Vibration severity guide for pumps and motors: below 2.8 mm/s is acceptable, 2.8 to 7.1 mm/s needs monitoring, above 7.1 mm/s is unacceptable and the machine should be stopped for inspection."},
    {"asset": "V-17", "text": "Hydraulic valve flange gaskets must be replaced with spiral wound type SW-316. Torque flange bolts to 85 Nm in a star pattern and pressure test at 1.5 times working pressure."},
    {"asset": "K-4", "text": "Gas leak response: stop work, isolate the supply, ventilate the area, keep ignition sources away, evacuate if the detector reads above 10 percent LEL and report to the control room."},
    {"asset": "M-9", "text": "Motor winding temperature limits: class F insulation alarms at 140 C and trips at 155 C. Blocked cooling fans are the most common cause of overheating; clean the fan cowl monthly."},
    {"asset": "B-2", "text": "Boiler B-2 normal operating pressure is 5.8 to 6.4 bar. Safety valve lifts at 7.5 bar. Log the pressure once per shift."},
    {"asset": "C-3", "text": "Conveyor belt tracking: adjust the tail pulley in steps of 2 mm and run the belt for five minutes between adjustments. Persistent drift usually indicates a worn idler."},
    {"asset": None, "text": "Lockout and tagout is mandatory before opening any rotating equipment. Each technician applies a personal lock and keeps the key."},
]


def seed_cloud(cloud: Cloud, embedder: Embedder, site: str = "global") -> int:
    """Publish headquarters knowledge straight to the cloud, as a central system would."""
    cloud.ensure()
    now = time.time()
    for index, item in enumerate(CLOUD_KNOWLEDGE):
        memory_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fieldmind/hq/{index}"))
        dense, sparse = embedder.document(item["text"])
        payload = {
            "text": item["text"], "kind": "reference", "asset": item["asset"], "tags": ["manual"],
            "site": site, "scope": "shared", "category": "procedure_fix", "priority": 1,
            "device_id": "hq", "origin_device": "hq", "author": "headquarters",
            "created_at": now, "updated_at": now, "rev": 1, "status": "active",
        }
        cloud.write(memory_id, dense, sparse, payload, None)
    return len(CLOUD_KNOWLEDGE)
