"""Regenerate only the named, synthetic envelope examples beside this file."""
import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def item(kind, payload, **headers):
    return json.dumps({"type": kind, "length": len(payload), **headers}, separators=(",", ":")).encode() + b"\n" + payload + b"\n"


def consistency_example():
    def transaction(trace, span, name="checkout", parent=None, release="shop@1"):
        context = {"trace_id": trace * 32, "span_id": span * 16}
        if parent is not None:
            context["parent_span_id"] = parent * 16
        return {"type": "transaction", "transaction": name, "release": release,
                "start_timestamp": 1, "timestamp": 2, "contexts": {"trace": context}}

    first = transaction("a", "1")
    first["spans"] = [
        {"trace_id": "a" * 32, "span_id": "2" * 16, "parent_span_id": "1" * 16,
         "op": "db.query", "start_timestamp": 1, "timestamp": 2},
        {"trace_id": "a" * 32, "span_id": "3" * 16,
         "op": "custom", "start_timestamp": 1, "timestamp": 2},
    ]
    events = [first, transaction("a", "1"),  # identical mapped root
              transaction("a", "2", "different definition"),  # conflicts with child
              transaction("a", "4", parent="2"),  # ambiguous parent definition
              transaction("a", "5", parent="9"),  # parent absent from input
              transaction("b", "6", parent="1"),  # ID occurs only in another trace
              transaction("b", "7", "independent", release="independent@2"),
              {"message": "uncorrelated", "release": "shop@1"}]
    return b"{}\n" + b"".join(item(event.get("type", "event"), json.dumps(event, indent=2).encode())
                               for event in events)


def main():
    event = {
        "event_id": "cccccccccccccccccccccccccccccccc",
        "timestamp": "2023-11-14T22:13:20.123456789Z",
        "level": "error", "message": "合成错误 — café\nsecond line",
        "release": "demo@1.0", "environment": "synthetic",
        "exception": {"values": [
            {"type": "ValueError", "value": "synthetic cause", "stacktrace": {"frames": [
                {"filename": "synthetic.py", "function": "parse", "lineno": 12,
                 "in_app": True, "vars": {"not_exported": "synthetic variable"}}
            ]}},
            {"type": "RuntimeError", "value": "synthetic wrapper", "mechanism": {"type": "generic"}}
        ]},
        "user": {"id": "synthetic-user-do-not-copy"},
        "tags": {"fixture": "synthetic"},
        "contexts": {"runtime": {"name": "synthetic-runtime"}},
    }
    payload = json.dumps(event, ensure_ascii=False, indent=2).encode("utf-8")
    transaction = (ROOT / "transaction.json").read_bytes()
    header = b'{"sdk":{"name":"synthetic-fixture"},"sent_at":"2023-11-14T22:13:22Z"}\n'
    mixed = header + item("event", payload) + item("transaction", transaction)
    (ROOT / "mixed.envelope").write_bytes(mixed)
    (ROOT / "mixed.envelope.gz").write_bytes(gzip.compress(mixed, mtime=0))
    (ROOT / "with-attachment.envelope").write_bytes(mixed + item("attachment", b"synthetic\x00binary\nnot-json\xff", filename="synthetic.bin"))
    (ROOT / "trace-consistency.envelope").write_bytes(consistency_example())


if __name__ == "__main__":
    main()
