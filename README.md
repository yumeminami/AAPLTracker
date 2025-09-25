# AAPLTracker

Command line helper for checking Apple Store pickup availability for the iPhone
17 Pro family in mainland China.

```bash
python main.py --location "Beijing"
```

Optional switches:

- `--location` – Search near a city or postcode (default: Beijing).
- `--store` – Query a specific retail store code such as `R320`.
- `--model` – Restrict to either "iPhone 17 Pro" or "iPhone 17 Pro Max".
- `--part` – Filter the results to specific Apple part numbers.
- `--show-raw` – Dump the JSON response for debugging.
- `--retry` / `--retry-delay` – Simple retry handling for transient failures.
