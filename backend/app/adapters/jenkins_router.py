from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ControllerState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class JenkinsController:
    controller_id: str
    state: ControllerState = ControllerState.HEALTHY
    queue_depth: int = 0
    executors_total: int = 1
    executors_busy: int = 0
    capabilities: set[str] | None = None

    @property
    def available_capacity(self) -> int:
        return max(0, self.executors_total - self.executors_busy)

    def supports(self, capability: str) -> bool:
        return self.capabilities is None or capability in self.capabilities


class JenkinsRouter:
    def __init__(self, controllers: list[JenkinsController]):
        self.controllers = controllers

    def choose_for_new_build(self, required_capability: str | None = None) -> JenkinsController:
        candidates = [
            controller for controller in self.controllers
            if controller.state == ControllerState.HEALTHY
            and (required_capability is None or controller.supports(required_capability))
            and controller.available_capacity > 0
        ]
        if not candidates:
            raise RuntimeError("no healthy Jenkins controller with capacity")
        return min(candidates, key=lambda item: (item.queue_depth, -item.available_capacity, item.controller_id))

    def mark_unavailable(self, controller_id: str) -> None:
        for controller in self.controllers:
            if controller.controller_id == controller_id:
                controller.state = ControllerState.UNAVAILABLE
                return
        raise KeyError(controller_id)
