"""Operational numbers in the Prometheus text format: counts, queue depths and
timings only. No note text ever appears here."""

from __future__ import annotations

from .runtime import Device


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def render(device: Device) -> str:
    service, journal, sync = device.service, device.journal, device.sync
    memory = service.stats()
    outbox = journal.counts()
    totals = journal.totals()
    link = sync.link()
    labels = f'device="{device.settings.device_id}",site="{device.settings.site}"'
    search = list(service.search_ms)
    answer = list(service.answer_ms)

    lines: list[str] = []

    def gauge(name: str, value: float, help_: str, extra: str = "") -> None:
        lines.append(f"# HELP fieldmind_{name} {help_}")
        lines.append(f"# TYPE fieldmind_{name} gauge")
        lines.append(f"fieldmind_{name}{{{labels}{',' + extra if extra else ''}}} {value}")

    def counter(name: str, value: float, help_: str) -> None:
        lines.append(f"# HELP fieldmind_{name} {help_}")
        lines.append(f"# TYPE fieldmind_{name} counter")
        lines.append(f"fieldmind_{name}{{{labels}}} {value}")

    gauge("up", 1, "The device process is serving.")
    gauge("link_online", int(bool(link["online"])), "1 when the cloud is reachable and the Network switch is on.")
    gauge("link_forced_offline", int(bool(link["forced_offline"])), "1 when the Network switch is off.")
    for key, help_ in (("local", "Memories written on this device."), ("replica", "Memories copied from the cloud."),
                       ("private", "Memories that never leave the device."), ("conflict", "Memories waiting for a decision."),
                       ("failed", "Memories whose last sync attempts failed.")):
        gauge(f"memories_{key}", memory[key], help_)
    gauge("outbox_pending", outbox["pending"], "Changes waiting to be sent.")
    gauge("outbox_failed", outbox["failed"], "Changes set aside after repeated failures.")
    gauge("open_conflicts", len(journal.conflicts("open")), "Conflicts that need a person.")
    counter("sync_runs_total", totals["runs"], "Completed sync cycles.")
    counter("sync_pushed_total", totals["pushed"], "Memories sent to the cloud.")
    counter("sync_pulled_total", totals["pulled"], "Memories received from the cloud.")
    counter("sync_merged_total", totals["merged"], "Concurrent edits merged automatically.")
    counter("sync_conflicts_total", totals["conflicts"], "Conflicts raised.")
    counter("sync_bytes_up_total", totals["bytes_up"], "Bytes sent to the cloud.")
    counter("sync_bytes_down_total", totals["bytes_down"], "Bytes received from the cloud.")
    counter("searches_total", service.searches, "Searches run on the device.")
    counter("answers_total", service.answers, "Questions answered on the device.")
    counter("answers_rejected_total", service.llm.rejected, "Generated answers discarded by the source check.")
    gauge("search_ms", _quantile(search, 0.5), "Recent search latency in milliseconds.", 'quantile="0.5"')
    lines.append(f'fieldmind_search_ms{{{labels},quantile="0.95"}} {_quantile(search, 0.95)}')
    gauge("answer_ms", _quantile(answer, 0.5), "Recent answer latency in milliseconds.", 'quantile="0.5"')
    lines.append(f'fieldmind_answer_ms{{{labels},quantile="0.95"}} {_quantile(answer, 0.95)}')
    last = journal.get("last_sync_at")
    gauge("last_sync_timestamp_seconds", last or 0, "Unix time of the last completed sync cycle.")
    return "\n".join(lines) + "\n"
