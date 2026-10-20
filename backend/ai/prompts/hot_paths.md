<!-- perfpilot-prompt name=hot_paths version=1 -->
Step 3 of 5: likely hot paths.

Below are execution paths found by static analysis (entry point first, as unit ids), the role of
the code they reach, the findings on them, and summaries from the previous steps. Select the paths
that are most likely to be performance-sensitive in production (request handlers and code called
per request or per item usually matter most; seed scripts, migrations and tests rarely do) and
explain why in `rationale`. Use only the given unit ids, in call order, and cite only provided
locations. This is a hypothesis: nothing was measured.
