"""Realistic synthetic audit data (findings, recommendations, inventory, code index) for tests."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import Any

from backend.models.audit_schemas import (
    ArchitectureInsight,
    CodeIndex,
    CodeLocation,
    DependencyGraph,
    DependencySpec,
    DetectedTechnology,
    Finding,
    GitMetadata,
    GraphEdge,
    GraphNode,
    ImportRef,
    ModuleInfo,
    Recommendation,
    RepositoryInventory,
    SourceFileInfo,
)
from backend.models.database import session_scope
from backend.models.enums import (
    AuditMode,
    AuditStage,
    AuditStatus,
    Confidence,
    EffortEstimate,
    FindingCategory,
    Provenance,
    RepositorySourceType,
    Severity,
    SnapshotStatus,
)
from backend.models.orm.audit import (
    AuditRun,
    FindingRecord,
    RecommendationRecord,
    Repository,
    RepositorySnapshot,
)

# A number followed by a time/memory unit, a speedup factor or a percentage.
MEASUREMENT_LIKE = re.compile(
    r"\d[\d,]*(?:\.\d+)?[ \u00a0]?(?:(?:ms|µs|us|ns|s|MB|MiB|KB|KiB|GB|GiB|x)\b|\u00d7|%)"
)


def visible_text(document: str) -> str:
    """Text a reader sees in an HTML document (styles and tags removed)."""
    document = re.sub(r"(?is)<style.*?</style>", " ", document)
    document = re.sub(r"(?s)<[^>]+>", " ", document)
    return html.unescape(document)


COMMIT_SHA = "3f9c2a7b1d4e5f60718293a4b5c6d7e8f9012345"
REPO_URL = "https://github.com/acme/shopfront"

# Built at runtime so no secret-looking literal lives in the source tree.
FAKE_GITHUB_TOKEN = "ghp_" + "Zq8" * 12
FAKE_DB_PASSWORD = "Tr0ub4dor" + "9x7Kq2"
FAKE_CREDENTIALS_SECRET = "cred" + "Secret" + "4242ZZ"

MODULES: list[tuple[str, str, bool]] = [
    ("shopfront", "shopfront/__init__.py", False),
    ("shopfront.main", "shopfront/main.py", True),
    ("shopfront.api.orders", "shopfront/api/orders.py", False),
    ("shopfront.api.products", "shopfront/api/products.py", False),
    ("shopfront.services.pricing", "shopfront/services/pricing.py", False),
    ("shopfront.services.inventory", "shopfront/services/inventory.py", False),
    ("shopfront.services.recommendations", "shopfront/services/recommendations.py", False),
    ("shopfront.db.session", "shopfront/db/session.py", False),
    ("shopfront.db.models", "shopfront/db/models.py", False),
    ("shopfront.utils.text", "shopfront/utils/text.py", False),
    ("shopfront.utils.cache", "shopfront/utils/cache.py", False),
    ("scripts.backfill", "scripts/backfill.py", True),
]

EDGES: list[tuple[str, str]] = [
    ("shopfront.main", "shopfront.api.orders"),
    ("shopfront.main", "shopfront.api.products"),
    ("shopfront.api.orders", "shopfront.services.pricing"),
    ("shopfront.api.orders", "shopfront.db.session"),
    ("shopfront.api.orders", "shopfront.db.models"),
    ("shopfront.api.products", "shopfront.services.inventory"),
    ("shopfront.api.products", "shopfront.services.recommendations"),
    ("shopfront.api.products", "shopfront.db.models"),
    ("shopfront.services.pricing", "shopfront.services.inventory"),
    ("shopfront.services.inventory", "shopfront.services.pricing"),
    ("shopfront.services.recommendations", "shopfront.utils.text"),
    ("shopfront.services.recommendations", "shopfront.db.models"),
    ("shopfront.services.inventory", "shopfront.utils.cache"),
    ("shopfront.db.models", "shopfront.db.session"),
    ("scripts.backfill", "shopfront.db.session"),
    ("scripts.backfill", "shopfront.services.pricing"),
]
CYCLES = [["shopfront.services.pricing", "shopfront.services.inventory"]]


def _inventory(limits_hit: list[str]) -> RepositoryInventory:
    files = [
        SourceFileInfo(path=path, size_bytes=1200 + 37 * i, language="Python", is_python=True)
        for i, (_, path, _) in enumerate(MODULES)
    ]
    files += [
        SourceFileInfo(
            path="shopfront/legacy/py2_compat.py", size_bytes=800, language="Python", is_python=True
        ),
        SourceFileInfo(path="web/src/App.tsx", size_bytes=5400, language="TypeScript"),
        SourceFileInfo(path="web/src/api.ts", size_bytes=2100, language="TypeScript"),
        SourceFileInfo(path="deploy/compose.yaml", size_bytes=900, language="YAML"),
        SourceFileInfo(
            path="web/node_modules/react/index.js",
            size_bytes=300,
            skipped=True,
            skip_reason="excluded_directory",
        ),
        SourceFileInfo(
            path="assets/logo.png", size_bytes=48_000, skipped=True, skip_reason="binary"
        ),
        SourceFileInfo(
            path="data/dump.sql", size_bytes=9_000_000, skipped=True, skip_reason="file_too_large"
        ),
        SourceFileInfo(path=".env", size_bytes=210, skipped=True, skip_reason="sensitive_file"),
        SourceFileInfo(
            path="deploy/credentials.json",
            size_bytes=410,
            skipped=True,
            skip_reason="sensitive_file",
        ),
    ]
    return RepositoryInventory(
        root_name="shopfront",
        files=files,
        total_files_seen=len(files) + 412,
        total_bytes_seen=sum(f.size_bytes for f in files),
        languages={"Python": len(MODULES) + 1, "TypeScript": 2, "YAML": 1},
        technologies=[
            DetectedTechnology(name="FastAPI", kind="framework", evidence=["pyproject.toml"]),
            DetectedTechnology(name="SQLAlchemy", kind="library", evidence=["pyproject.toml"]),
            DetectedTechnology(name="PostgreSQL", kind="database", evidence=["compose.yaml"]),
            DetectedTechnology(name="React", kind="framework", evidence=["web/package.json"]),
        ],
        dependencies=[
            DependencySpec(name="fastapi", version_spec=">=0.110", source_file="pyproject.toml"),
            DependencySpec(name="sqlalchemy", version_spec=">=2.0", source_file="pyproject.toml"),
        ],
        limits_hit=limits_hit,
    )


def _code_index() -> CodeIndex:
    modules = [
        ModuleInfo(
            module_name=name,
            file_path=path,
            line_count=120,
            imports=[
                ImportRef(module=t, line=i + 1, resolved_module=t, is_internal=True)
                for i, (s, t) in enumerate(EDGES)
                if s == name
            ],
            is_entrypoint=entry,
            entrypoint_reasons=(
                ["FastAPI app instance"] if name == "shopfront.main" else ["__main__ guard"]
            )
            if entry
            else [],
        )
        for name, path, entry in MODULES
    ]
    nodes = [
        GraphNode(module_name=name, file_path=path, is_entrypoint=entry)
        for name, path, entry in MODULES
    ]
    graph = DependencyGraph(
        nodes=nodes,
        edges=[GraphEdge(source=s, target=t, line=3) for s, t in EDGES],
        external_imports={"fastapi": 4, "sqlalchemy": 5, "requests": 1, "re": 2},
        cycles=CYCLES,
    )
    return CodeIndex(
        modules=modules,
        graph=graph,
        parse_errors={"shopfront/legacy/py2_compat.py": "SyntaxError: invalid syntax (line 14)"},
    )


def _finding(
    fid: str,
    rule: str,
    title: str,
    category: FindingCategory,
    severity: Severity,
    path: str,
    start: int,
    end: int,
    snippet: str,
    *,
    score: float,
    complexity: str | None = None,
    provenance: Provenance = Provenance.STATIC,
    symbol: str | None = None,
) -> Finding:
    return Finding(
        id=fid,
        rule_id=rule,
        title=title,
        category=category,
        description=f"{title} detected by rule {rule}.",
        severity=severity,
        confidence=Confidence.HIGH
        if severity in {Severity.CRITICAL, Severity.HIGH}
        else Confidence.MEDIUM,
        provenance=provenance,
        location=CodeLocation(file_path=path, line_start=start, line_end=end, symbol=symbol),
        snippet=snippet,
        suggestion="Restructure the hot path as described in the linked recommendation.",
        tradeoffs="Slightly more memory for an auxiliary structure.",
        complexity_note=complexity,
        priority_score=score,
    )


def build_findings() -> list[Finding]:
    return [
        _finding(
            "F-001",
            "PP-LOOP-001",
            "List membership test inside a loop",
            FindingCategory.COLLECTIONS,
            Severity.HIGH,
            "shopfront/services/recommendations.py",
            42,
            45,
            "for item in candidates:\n    if item.sku in purchased_skus:\n"
            "        continue\n    results.append(item)\n",
            score=0.91,
            complexity="O(n*m) membership checks; O(n+m) with a set",
            symbol="recommend_for_user",
        ),
        _finding(
            "F-002",
            "PP-DB-002",
            "Query executed inside a loop (N+1)",
            FindingCategory.DATABASE,
            Severity.CRITICAL,
            "shopfront/api/orders.py",
            88,
            91,
            "for order in orders:\n    order.items = session.execute(\n"
            "        select(Item).where(Item.order_id == order.id)\n    ).scalars().all()\n",
            score=0.95,
            complexity="O(n) round-trips to the database",
            symbol="list_orders",
        ),
        _finding(
            "F-003",
            "PP-ASYNC-001",
            "Blocking HTTP call inside async function",
            FindingCategory.ASYNC_BLOCKING,
            Severity.HIGH,
            "shopfront/api/products.py",
            31,
            31,
            '    resp = requests.get(f"{PRICING_URL}/quote/{sku}", timeout=5)\n',
            score=0.84,
            symbol="get_product",
        ),
        _finding(
            "F-004",
            "PP-STR-001",
            "String concatenation in a loop",
            FindingCategory.STRINGS,
            Severity.MEDIUM,
            "shopfront/utils/text.py",
            12,
            15,
            "out = ''\nfor line in lines:\n    out += line.strip() + '\\n'\nreturn out\n",
            score=0.55,
            complexity="O(n^2) copying in the worst case",
            symbol="normalize",
        ),
        _finding(
            "F-005",
            "PP-REGEX-001",
            "Regex compiled on every call",
            FindingCategory.REGEX,
            Severity.LOW,
            "shopfront/services/pricing.py",
            57,
            57,
            "    pattern = re.compile(r'^SKU-(\\d+)$')\n",
            score=0.31,
            symbol="parse_sku",
        ),
        _finding(
            "F-006",
            "PP-CACHE-001",
            "Repeated pure computation could be memoized",
            FindingCategory.CACHING,
            Severity.MEDIUM,
            "shopfront/services/inventory.py",
            20,
            22,
            "def stock_level(sku):\n    return sum(b.qty for b in load_batches(sku))\n",
            score=0.48,
            provenance=Provenance.AI_HYPOTHESIS,
            symbol="stock_level",
        ),
        _finding(
            "F-007",
            "PP-STR-002",
            "HTML assembled by string formatting in a loop",
            FindingCategory.STRINGS,
            Severity.LOW,
            "shopfront/utils/text.py",
            30,
            31,
            "banner = \"<script>alert('owned')</script>\"\nhtml += banner\n",
            score=0.22,
            symbol="render_banner",
        ),
        _finding(
            "F-008",
            "PP-IO-001",
            "Connection created per request",
            FindingCategory.NETWORK,
            Severity.MEDIUM,
            "shopfront/db/session.py",
            5,
            6,
            f'GITHUB_TOKEN = "{FAKE_GITHUB_TOKEN}"\n'
            f'engine = create_engine("postgresql://shop:{FAKE_DB_PASSWORD}@db/shop")\n',
            score=0.5,
            symbol="get_engine",
        ),
        _finding(
            "F-009",
            "PP-IO-002",
            "Credentials file parsed on every request",
            FindingCategory.NETWORK,
            Severity.LOW,
            "deploy/credentials.json",
            1,
            2,
            f'{{"client_secret": "{FAKE_CREDENTIALS_SECRET}"}}\n',
            score=0.1,
        ),
    ]


SET_DIFF = """--- a/shopfront/services/recommendations.py
+++ b/shopfront/services/recommendations.py
@@ -40,6 +40,7 @@ def recommend_for_user(user, candidates):
     purchased = load_purchases(user)
-    purchased_skus = [p.sku for p in purchased]
+    purchased_skus = {p.sku for p in purchased}
     for item in candidates:
         if item.sku in purchased_skus:
             continue
"""

EAGER_DIFF = """--- a/shopfront/api/orders.py
+++ b/shopfront/api/orders.py
@@ -85,9 +85,8 @@ def list_orders(session, customer_id):
-    orders = session.execute(select(Order).where(Order.customer_id == customer_id)).scalars()
-    for order in orders:
-        order.items = session.execute(
-            select(Item).where(Item.order_id == order.id)
-        ).scalars().all()
+    stmt = select(Order).where(Order.customer_id == customer_id).options(
+        selectinload(Order.items)
+    )
+    orders = session.execute(stmt).scalars().all()
     return orders
"""

JOIN_DIFF = """--- a/shopfront/utils/text.py
+++ b/shopfront/utils/text.py
@@ -11,5 +11,2 @@ def normalize(lines):
-    out = ''
-    for line in lines:
-        out += line.strip() + '\\n'
-    return out
+    return ''.join(line.strip() + '\\n' for line in lines)
"""

SECRET_DIFF = """--- a/deploy/credentials.json
+++ b/deploy/credentials.json
@@ -1 +1 @@
-{"client_secret": "old"}
+{"client_secret": "new"}
"""


def _rec(
    rid: str,
    title: str,
    finding_ids: list[str],
    effort: EffortEstimate,
    rank: int,
    *,
    provenance: Provenance = Provenance.STATIC,
    diff: str | None = None,
    citations: list[CodeLocation] | None = None,
) -> Recommendation:
    return Recommendation(
        id=rid,
        title=title,
        summary=f"{title}.",
        rationale="The flagged pattern repeats work proportional to the input size.",
        proposed_strategy=f"{title} and keep behaviour identical.",
        finding_ids=finding_ids,
        citations=citations or [],
        provenance=provenance,
        effort=effort,
        uncertainty="Impact depends on production data sizes; not measured.",
        expected_effect="Likely fewer redundant operations on large inputs (hypothesis).",
        correctness_risks="Ordering and duplicate semantics must be preserved.",
        suggested_diff=diff,
        priority_rank=rank,
        generated_by="deterministic" if provenance == Provenance.STATIC else "gpt-test-model",
    )


def build_recommendations() -> list[Recommendation]:
    return [
        _rec(
            "R-001",
            "Use a set for SKU membership checks",
            ["F-001"],
            EffortEstimate.SMALL,
            1,
            diff=SET_DIFF,
            citations=[
                CodeLocation(
                    file_path="shopfront/services/recommendations.py", line_start=42, line_end=45
                )
            ],
        ),
        _rec(
            "R-002", "Eager-load order items", ["F-002"], EffortEstimate.MEDIUM, 2, diff=EAGER_DIFF
        ),
        _rec(
            "R-003",
            "Use an async HTTP client",
            ["F-003"],
            EffortEstimate.SMALL,
            3,
            provenance=Provenance.AI_HYPOTHESIS,
        ),
        _rec(
            "R-004",
            "Build strings with str.join",
            ["F-004"],
            EffortEstimate.SMALL,
            4,
            diff=JOIN_DIFF,
        ),
        _rec(
            "R-005",
            "Break the pricing/inventory import cycle",
            [],
            EffortEstimate.LARGE,
            5,
            provenance=Provenance.AI_HYPOTHESIS,
        ),
        _rec("R-006", "Hoist the SKU regex to module level", ["F-005"], EffortEstimate.SMALL, 0),
        _rec(
            "R-007",
            "Load credentials once at startup",
            ["F-009"],
            EffortEstimate.SMALL,
            6,
            diff=SECRET_DIFF,
        ),
    ]


AuditFactory = Callable[..., str]


def create_audit(
    *,
    status: AuditStatus = AuditStatus.SUCCEEDED,
    source_type: RepositorySourceType = RepositorySourceType.GITHUB_PUBLIC,
    locator: str = REPO_URL,
    commit_sha: str | None = COMMIT_SHA,
    with_index: bool = True,
    findings: list[Finding] | None = None,
    recommendations: list[Recommendation] | None = None,
    limits_hit: list[str] | None = None,
    extra_audit_fields: dict[str, Any] | None = None,
) -> str:
    """Create Repository -> Snapshot -> AuditRun (+ findings, recommendations); return audit id."""
    findings = build_findings() if findings is None else findings
    recommendations = build_recommendations() if recommendations is None else recommendations
    with session_scope() as session:
        repo = Repository(
            name="acme/shopfront",
            source_type=source_type,
            source_locator=locator,
            requested_ref="main",
        )
        session.add(repo)
        session.flush()
        snapshot = RepositorySnapshot(
            repository_id=repo.id,
            status=SnapshotStatus.READY,
            commit_sha=commit_sha,
            branch="main",
            git=GitMetadata(commit_sha=commit_sha, branch="main", remote_url=locator).model_dump(
                mode="json"
            ),
            inventory=_inventory(limits_hit or []).model_dump(mode="json"),
            content_hash="b" * 64,
        )
        session.add(snapshot)
        session.flush()
        audit = AuditRun(
            snapshot_id=snapshot.id,
            mode=AuditMode.STATIC,
            status=status,
            stage=AuditStage.COMPLETED,
            code_index=_code_index().model_dump(mode="json") if with_index else None,
            insights=[
                ArchitectureInsight(
                    id="I-001",
                    title="Pricing and inventory are mutually dependent",
                    description="An import cycle couples pricing and inventory services.",
                    modules=["shopfront.services.pricing", "shopfront.services.inventory"],
                ).model_dump(mode="json")
            ],
            **(extra_audit_fields or {}),
        )
        session.add(audit)
        session.flush()
        for f in findings:
            session.add(
                FindingRecord(
                    audit_id=audit.id,
                    rule_id=f.rule_id,
                    category=f.category,
                    severity=f.severity,
                    confidence=f.confidence,
                    provenance=f.provenance,
                    file_path=f.location.file_path,
                    line_start=f.location.line_start,
                    priority_score=f.priority_score,
                    data=f.model_dump(mode="json"),
                )
            )
        for r in recommendations:
            session.add(
                RecommendationRecord(
                    audit_id=audit.id,
                    priority_rank=r.priority_rank,
                    provenance=r.provenance,
                    generated_by=r.generated_by,
                    data=r.model_dump(mode="json"),
                )
            )
        return audit.id
