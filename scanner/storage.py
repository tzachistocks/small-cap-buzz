"""Small JSON/JSONL storage inside the repo's data/ folder (committed by the workflow)."""
import json
import os
from datetime import datetime, timezone, timedelta


class Store:
    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "snapshots"), exist_ok=True)

    def path(self, name):
        return os.path.join(self.root, name)

    def append_snapshot(self, snap):
        day = snap["ts"][:10]
        with open(os.path.join(self.root, "snapshots", f"{day}.jsonl"), "a") as f:
            f.write(json.dumps(snap, separators=(",", ":")) + "\n")

    def load_history(self, days=8):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        out = []
        d = os.path.join(self.root, "snapshots")
        for fn in sorted(os.listdir(d)):
            if fn[:10] < cutoff or not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(d, fn)) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        out.sort(key=lambda s: s["ts"])
        return out[:-1] if out else out  # drop the snapshot written in this run

    def prune(self, keep_days):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).date().isoformat()
        d = os.path.join(self.root, "snapshots")
        for fn in os.listdir(d):
            if fn[:10] < cutoff:
                os.remove(os.path.join(d, fn))

    def load_json(self, name, default):
        try:
            with open(self.path(name)) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def save_json(self, name, obj):
        with open(self.path(name), "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)

    def load_alerts(self):
        return self.load_json("alerts.json", [])

    def save_alerts(self, alerts):
        self.save_json("alerts.json", alerts)
