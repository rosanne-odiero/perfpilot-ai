# Advisor fixture

Small, never-executed-by-the-audit repository used by the deterministic advisor tests: an N+1
router, sync database access in async handlers, per-call HTTP clients in two modules, a seed
script, and helpers whose findings have safe mechanical rewrites (plus look-alikes that must not
get a diff).
