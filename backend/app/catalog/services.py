"""Service Catalog & Dependency Graph domain logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..store.records import CatalogServiceRecord, ServiceDependencyRecord
from ..store.session import PlatformSession


VALID_TIERS = {"tier-0", "tier-1", "tier-2", "tier-3"}
VALID_LIFECYCLES = {"experimental", "active", "deprecated", "end_of_life"}
VALID_DEPENDENCY_TYPES = {"sync", "async", "data", "optional"}


class CatalogValidationError(ValueError):
    """Raised when catalog validation fails."""


@dataclass(frozen=True)
class DependencyGraph:
    service_id: str
    nodes: list[CatalogServiceRecord]
    edges: list[dict[str, Any]]
    upstream: list[str]
    downstream: list[str]
    has_cycle: bool
    cycles: list[list[str]]


class CatalogServiceManager:
    """Manages service catalog registrations, team ownership, and dependency topology."""

    def __init__(self, session: PlatformSession) -> None:
        self._session = session

    def register_service(
        self,
        *,
        service_id: str,
        name: str,
        owning_team: str,
        description: str = "",
        tier: str = "tier-2",
        lifecycle: str = "active",
        repo_url: str = "",
        docs_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CatalogServiceRecord:
        service_id = service_id.strip()
        if not service_id:
            raise CatalogValidationError("service_id cannot be empty")
        if not name.strip():
            raise CatalogValidationError("service name cannot be empty")
        if not owning_team.strip():
            raise CatalogValidationError("owning_team cannot be empty")
        if tier not in VALID_TIERS:
            raise CatalogValidationError(f"invalid tier '{tier}', must be one of {sorted(VALID_TIERS)}")
        if lifecycle not in VALID_LIFECYCLES:
            raise CatalogValidationError(
                f"invalid lifecycle '{lifecycle}', must be one of {sorted(VALID_LIFECYCLES)}"
            )

        now = datetime.now(timezone.utc)
        record = CatalogServiceRecord(
            id=service_id,
            name=name.strip(),
            description=description.strip(),
            owning_team=owning_team.strip(),
            tier=tier,
            lifecycle=lifecycle,
            repo_url=repo_url.strip(),
            docs_url=docs_url.strip(),
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        self._session.insert_catalog_service(record)
        return record

    def update_service(
        self,
        *,
        service_id: str,
        name: str | None = None,
        description: str | None = None,
        owning_team: str | None = None,
        tier: str | None = None,
        lifecycle: str | None = None,
        repo_url: str | None = None,
        docs_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CatalogServiceRecord:
        current = self._session.catalog_service(service_id)
        if not current:
            raise CatalogValidationError(f"service '{service_id}' not found")

        updated_tier = tier if tier is not None else current.tier
        if updated_tier not in VALID_TIERS:
            raise CatalogValidationError(f"invalid tier '{updated_tier}'")

        updated_lifecycle = lifecycle if lifecycle is not None else current.lifecycle
        if updated_lifecycle not in VALID_LIFECYCLES:
            raise CatalogValidationError(f"invalid lifecycle '{updated_lifecycle}'")

        updated = CatalogServiceRecord(
            id=service_id,
            name=name.strip() if name is not None else current.name,
            description=description.strip() if description is not None else current.description,
            owning_team=owning_team.strip() if owning_team is not None else current.owning_team,
            tier=updated_tier,
            lifecycle=updated_lifecycle,
            repo_url=repo_url.strip() if repo_url is not None else current.repo_url,
            docs_url=docs_url.strip() if docs_url is not None else current.docs_url,
            metadata=dict(metadata) if metadata is not None else current.metadata,
            created_at=current.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        self._session.update_catalog_service(updated)
        return updated

    def add_dependency(
        self,
        *,
        source_service_id: str,
        target_service_id: str,
        dependency_type: str = "sync",
        description: str = "",
    ) -> ServiceDependencyRecord:
        if source_service_id == target_service_id:
            raise CatalogValidationError("A service cannot depend on itself")

        if not self._session.catalog_service(source_service_id):
            raise CatalogValidationError(f"source service '{source_service_id}' does not exist")
        if not self._session.catalog_service(target_service_id):
            raise CatalogValidationError(f"target service '{target_service_id}' does not exist")

        if dependency_type not in VALID_DEPENDENCY_TYPES:
            raise CatalogValidationError(
                f"invalid dependency_type '{dependency_type}', must be one of {sorted(VALID_DEPENDENCY_TYPES)}"
            )

        dep = ServiceDependencyRecord(
            id=uuid4(),
            source_service_id=source_service_id,
            target_service_id=target_service_id,
            dependency_type=dependency_type,
            description=description.strip(),
            created_at=datetime.now(timezone.utc),
        )
        self._session.insert_service_dependency(dep)
        return dep

    def remove_dependency(self, source_service_id: str, target_service_id: str) -> bool:
        return self._session.delete_service_dependency(source_service_id, target_service_id)

    def get_dependency_graph(self, service_id: str, max_depth: int = 5) -> DependencyGraph:
        root = self._session.catalog_service(service_id)
        if not root:
            raise CatalogValidationError(f"service '{service_id}' not found")

        visited_nodes: dict[str, CatalogServiceRecord] = {service_id: root}
        edges: list[dict[str, Any]] = []
        upstream: set[str] = set()
        downstream: set[str] = set()

        # BFS / DFS traversal
        queue: list[tuple[str, int]] = [(service_id, 0)]
        seen_pairs: set[tuple[str, str]] = set()

        while queue:
            curr_id, depth = queue.pop(0)
            if depth >= max_depth:
                continue

            deps = self._session.service_dependencies(curr_id)
            for d in deps:
                pair = (d.source_service_id, d.target_service_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                edge_dict = {
                    "source": d.source_service_id,
                    "target": d.target_service_id,
                    "type": d.dependency_type,
                    "description": d.description,
                }
                edges.append(edge_dict)

                # Upstream: services that curr_id calls (source -> target)
                if d.source_service_id == service_id:
                    upstream.add(d.target_service_id)
                # Downstream: services that call curr_id (target <- source)
                if d.target_service_id == service_id:
                    downstream.add(d.source_service_id)

                for next_id in (d.source_service_id, d.target_service_id):
                    if next_id not in visited_nodes:
                        svc_rec = self._session.catalog_service(next_id)
                        if svc_rec:
                            visited_nodes[next_id] = svc_rec
                            queue.append((next_id, depth + 1))

        # Cycle detection on traversed edges
        cycles = self._detect_cycles(edges)

        return DependencyGraph(
            service_id=service_id,
            nodes=list(visited_nodes.values()),
            edges=edges,
            upstream=sorted(upstream),
            downstream=sorted(downstream),
            has_cycle=len(cycles) > 0,
            cycles=cycles,
        )

    def _detect_cycles(self, edges: list[dict[str, Any]]) -> list[list[str]]:
        adj: dict[str, list[str]] = {}
        for e in edges:
            adj.setdefault(e["source"], []).append(e["target"])

        visited: set[str] = set()
        recursion_stack: list[str] = []
        cycles: list[list[str]] = []

        def dfs(node: str):
            visited.add(node)
            recursion_stack.append(node)

            for neighbor in adj.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor)
                elif neighbor in recursion_stack:
                    idx = recursion_stack.index(neighbor)
                    cycles.append(recursion_stack[idx:] + [neighbor])

            recursion_stack.pop()

        for node in list(adj.keys()):
            if node not in visited:
                dfs(node)

        return cycles
