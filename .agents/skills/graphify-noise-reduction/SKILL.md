---
name: graphify-noise-reduction
description: Use with the vendored graphify skill in this repository when running /graphify, narrowing graph scope, reducing inferred-edge noise, or querying stricter extracted-only graphs.
---

# Power Tetris graphify addon

Use this repo-local addon after reading the vendored graphify skill and before
running graphify commands in this repository. It records local operating
guidance that should not be edited into graphify's vendored skill files.

## Low-INFERRED analysis

Graphify currently has no built-in low-INFERRED mode. Avoid `--mode deep` when
lower inferred-edge volume matters.

For stricter analysis, create an extracted-only graph and query that graph with
`--graph`:

```bash
python - <<'PY'
import json
from pathlib import Path

p = Path("graphify-out/graph.json")
g = json.loads(p.read_text())
g["links"] = [
    e for e in g.get("links", [])
    if e.get("confidence") == "EXTRACTED"
]
Path("graphify-out/graph.extracted-only.json").write_text(json.dumps(g, indent=2))
PY

graphify query "..." --graph graphify-out/graph.extracted-only.json
```

## Narrowed graph rebuilds

When rebuilding a narrowed graph after a broader graph, graphify may refuse to
overwrite `graphify-out/graph.json` because the new graph is much smaller. If
the narrower scope is intentional, force the overwrite only after confirming the
node-count reduction is expected.
