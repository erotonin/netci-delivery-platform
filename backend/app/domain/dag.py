# backend/app/domain/dag.py
"""DAG release plan computation, cycle detection, and wave grouping."""

from __future__ import annotations

from typing import Any


class DagValidationError(Exception):
    def __init__(self, message: str, code: str = "DAG_VALIDATION_ERROR", status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def compute_dag_waves(modules: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute release waves from module definitions and explicit dependencies.

    Uses Kahn's topological sort algorithm to group independent stages into
    parallel waves and detect cycles. If dependencies are empty, groups waves
    by deploymentOrder.
    """
    if not modules:
        raise DagValidationError("At least one module must be included in the release plan", "EMPTY_MODULES", 422)

    module_ids = [str(m["moduleId"]) for m in modules]
    unique_ids = set()
    for mid in module_ids:
        if mid in unique_ids:
            raise DagValidationError(f"Duplicate module {mid} in production request", "DUPLICATE_MODULE", 422)
        unique_ids.add(mid)

    # Build adjacency list and in-degrees
    adj: dict[str, list[str]] = {mid: [] for mid in module_ids}
    in_degree: dict[str, int] = {mid: 0 for mid in module_ids}
    dependencies_map: dict[str, list[str]] = {}

    has_explicit_deps = False
    for m in modules:
        mid = str(m["moduleId"])
        raw_deps = m.get("dependencies") or []
        deps = [str(d) for d in raw_deps]
        dependencies_map[mid] = deps
        if deps:
            has_explicit_deps = True

        for dep in deps:
            if dep == mid:
                raise DagValidationError(
                    f"Module {mid} cannot depend on itself",
                    "CYCLIC_DEPENDENCY",
                    422,
                )
            if dep not in unique_ids:
                raise DagValidationError(
                    f"Module {mid} depends on unknown module {dep} not in this request",
                    "INVALID_DEPENDENCY",
                    422,
                )
            # dep -> mid (dep must complete before mid can start)
            adj[dep].append(mid)
            in_degree[mid] += 1

    # Fallback to deploymentOrder grouping if no explicit dependencies given
    if not has_explicit_deps:
        # Group by deploymentOrder
        order_groups: dict[int, list[str]] = {}
        for m in modules:
            mid = str(m["moduleId"])
            order = int(m.get("deploymentOrder", 1))
            order_groups.setdefault(order, []).append(mid)

        waves = []
        for wave_idx, order in enumerate(sorted(order_groups.keys()), start=1):
            waves.append({
                "wave": wave_idx,
                "deploymentOrder": order,
                "moduleIds": order_groups[order],
                "status": "pending",
            })

        return {
            "totalModules": len(module_ids),
            "totalWaves": len(waves),
            "waves": waves,
            "dependencies": dependencies_map,
        }

    # Kahn's Algorithm with Wave Layering
    current_wave_nodes = [mid for mid in module_ids if in_degree[mid] == 0]
    processed_count = 0
    waves = []
    wave_num = 1

    while current_wave_nodes:
        # Sort current wave nodes for deterministic execution order
        current_wave_nodes.sort()
        waves.append({
            "wave": wave_num,
            "moduleIds": current_wave_nodes,
            "status": "pending",
        })
        processed_count += len(current_wave_nodes)

        next_wave_nodes = []
        for node in current_wave_nodes:
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_wave_nodes.append(neighbor)

        current_wave_nodes = next_wave_nodes
        wave_num += 1

    if processed_count < len(module_ids):
        raise DagValidationError(
            "Cyclic dependency detected in module release graph",
            "CYCLIC_DEPENDENCY",
            422,
        )

    return {
        "totalModules": len(module_ids),
        "totalWaves": len(waves),
        "waves": waves,
        "dependencies": dependencies_map,
    }
