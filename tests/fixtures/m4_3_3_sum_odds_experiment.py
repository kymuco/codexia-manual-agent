from __future__ import annotations

import json
import sys
from pathlib import Path


run_id, output_path, input_json = sys.argv[1:4]
inputs = json.loads(input_json)
n = inputs["n"]
if type(n) is not int or not 1 <= n <= 10_000:
    raise ValueError("n must be an integer in [1, 10000]")

observed = sum(range(1, 2 * n, 2))
expected = n * n
absolute_error = abs(observed - expected)

result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {
        "name": "absolute_error",
        "value": absolute_error,
        "unit": "integer",
    },
}
encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
target = Path(output_path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(encoded, encoding="utf-8")
sys.stdout.write(encoded)
