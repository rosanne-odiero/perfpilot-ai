<!-- perfpilot-prompt name=correlation version=1 -->
Step 4 of 5: cross-module correlation.

Below are groups of static findings across the repository with their modules and roles, plus the
module context and likely hot paths from earlier steps. Identify architectural patterns that span
several modules (for example the same N+1 access pattern in many routers, clients created per
call in several services, synchronous database access in async handlers, a hot shared module).
Return at most five insights; each must name the modules involved (exact names) and cite provided
locations. Do not repeat a single finding as an insight.
