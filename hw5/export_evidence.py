"""Read-only journal copy from the VM using IAP; no SSH provisioning commands."""
from datetime import datetime, timezone
import json

from provision import OWNER, ROOT, owned


def export(gc, config):
    vm = gc.describe(["compute", "instances", "describe", OWNER + "-server", f"--zone={config['zone']}"])
    if vm is None:
        print("No server VM; existing local evidence is retained.")
        return None
    owned(vm)
    if vm["status"] == "TERMINATED":
        print("Server is stopped; use the evidence exported before stop, or start it before exporting again.")
        return None
    if vm["status"] != "RUNNING":
        raise RuntimeError("Wait for the server VM transition before exporting evidence.")
    destination = ROOT / "evidence" / ("export-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    destination.mkdir(parents=True, exist_ok=False)
    gc.run(["compute", "scp", f"{OWNER}-server:/var/lib/hw5/timings.jsonl", str(destination / "timings.jsonl"),
            f"--zone={config['zone']}", "--tunnel-through-iap"])
    gc.run(["compute", "scp", f"{OWNER}-server:/var/lib/hw5/database-failures.jsonl", str(destination / "database-failures.jsonl"),
            f"--zone={config['zone']}", "--tunnel-through-iap"])
    # This snapshot can overlap a write; the summarizer reports any incomplete last line.
    (destination / "export.json").write_text(json.dumps(dict(server=vm["name"],
        exportedAt=datetime.now(timezone.utc).isoformat(), note="Read-only IAP SCP snapshot; request IDs reconcile against SQL."), indent=2), encoding="utf-8")
    (ROOT / ".state" / "latest-export.json").write_text(json.dumps(dict(directory=str(destination))), encoding="utf-8")
    print(f"Timing evidence exported to {destination}", flush=True)
    return destination
