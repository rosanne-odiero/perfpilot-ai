# ShopFast

A small storefront backend (catalog, pricing, orders, inventory sync, reporting) used as a
deterministic fixture for PerfPilot's static audit. Standard library only.

```bash
python -m pytest            # unit tests
python benchmarks/run_workload.py   # deterministic, seeded workload (no network)
python -m shopfast report --orders 50
```

`.env` holds a deliberately fake API key; it exists to verify that audits never read secrets.
