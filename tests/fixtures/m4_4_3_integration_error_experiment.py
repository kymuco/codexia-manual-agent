from pathlib import Path
import json
import sys

run_id, output_path, input_json = sys.argv[1:4]
config = json.loads(input_json)
method = config["method"]
n = config["n"]

if type(n) is not int or n <= 0:
    raise ValueError("n must be a positive integer")

if method == "left":
    sum_squares = sum(i * i for i in range(n))
    # Both methods report absolute error over the same denominator 6*n^3.
    error_numerator = abs(2 * n**3 - 6 * sum_squares)
elif method == "trapezoid":
    interior_sum = sum(i * i for i in range(1, n))
    error_numerator = abs(2 * n**3 - 3 * (2 * interior_sum + n * n))
else:
    raise ValueError("unsupported integration method")

result = {
    "schema": "codexia.python-json-result.v1",
    "run_id": run_id,
    "metric": {
        "name": "integration_error_numerator",
        "value": error_numerator,
        "unit": "over_6n_cubed",
    },
}
encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
target = Path(output_path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(encoded, encoding="utf-8")
sys.stdout.write(encoded)
