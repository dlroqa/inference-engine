"""Virtual auto-models + route/cascade policies (Block 10, sub-slice 5a).

A *virtual model* is a client-facing model name that maps to a routing policy over
the backend pool, instead of a single physical model. Physical model ids (the
heterogeneous union the pool serves, Block 12.1) always work; virtual models add
named policies on top:

* **route** — serve from any backend in an allowed set (least-busy / prefix
  affinity within it, via the registry).
* **cascade** — try each ordered step's backends; the first step with an available
  backend serves the request (admission-time fallback across steps). Escalation is
  selection-time only — consistent with the engine's no-mid-stream-failover rule,
  there is never a switch after tokens have been sent.

The router resolves a requested name to one-or-more registry ``select``/``acquire``
scopes and delegates placement to the registry, so least-busy and prefix affinity
(sub-slices 3-4) keep working *within* a policy's allowed backends. ``plan``
answers the same question without reserving anything (the dry-run explanation).
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.inference.registry import (
    BackendEntry,
    BackendRegistry,
    NoBackendAvailable,
    RegistryLease,
)
from engine.inference.types import FeatureUnsupportedError


@dataclass(frozen=True)
class VirtualModel:
    """A resolved virtual model: a name, a policy, and ordered backend groups."""

    name: str
    policy: str  # "route" | "cascade"
    steps: tuple[tuple[str, ...], ...]


class Router:
    """Resolves client-facing model names to registry placement scopes."""

    def __init__(self, registry: BackendRegistry, virtual_models: list[VirtualModel]) -> None:
        self._registry = registry
        self._vmodels = {vm.name: vm for vm in virtual_models}

    # -- name resolution ---------------------------------------------------

    def known_model(self, name: str) -> bool:
        return name in self._vmodels or name in self._registry.known_models()

    def model_names(self) -> list[str]:
        """Every model a client may request, in a stable order (Block 12.1).

        Sorted physical model ids (the heterogeneous union across the pool) followed
        by virtual model names in configuration order. Physical/virtual name
        collisions are rejected at config load, so this list is unambiguous.
        """
        return [*sorted(self._registry.known_models()), *self._vmodels]

    def _is_virtual(self, name: str) -> bool:
        return name in self._vmodels

    def virtual_model_names(self) -> frozenset[str]:
        """The configured virtual model names (Block 12.1 collision checks)."""
        return frozenset(self._vmodels)

    def _scopes(self, name: str) -> list[frozenset[str] | None]:
        """Ordered placement scopes for a virtual model's route/cascade steps."""
        vm = self._vmodels[name]
        return [frozenset(step) for step in vm.steps]

    # -- placement ---------------------------------------------------------

    def acquire(
        self,
        name: str,
        prefix_key: str | None = None,
        required_features: frozenset[str] = frozenset(),
    ) -> RegistryLease:
        """Reserve a slot for a model, applying model + feature eligibility.

        A physical model scopes placement to its eligible engines (``model=name``).
        A virtual model cascades across its configured steps; every step still has
        the request's ``required_features`` enforced, so a route/cascade never lands
        on a feature-incapable backend. ``FeatureUnsupportedError`` propagates for
        the edge to map to a 400.
        """
        if not self._is_virtual(name):
            return self._registry.acquire(
                prefix_key, model=name, required_features=required_features
            )
        # Cascade across steps; keep the most actionable failure if none take it.
        saw_busy = saw_feature = False
        for scope in self._scopes(name):
            try:
                return self._registry.acquire(
                    prefix_key, allowed=scope, required_features=required_features
                )
            except NoBackendAvailable as exc:
                saw_busy = saw_busy or exc.reason == "busy"
            except FeatureUnsupportedError:
                saw_feature = True
        if saw_busy:  # a valid placement existed momentarily; retriable
            raise NoBackendAvailable("busy")
        if saw_feature:
            raise FeatureUnsupportedError(required_features, model=name)
        raise NoBackendAvailable("unloaded")

    def plan(
        self,
        name: str,
        prefix_key: str | None = None,
        required_features: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        """Explain how ``name`` would route right now, without reserving anything."""
        virtual = self._is_virtual(name)
        policy = self._vmodels[name].policy if virtual else "base"
        # Physical models resolve over the whole pool filtered by model eligibility;
        # virtual models resolve over their configured step scopes.
        model = None if virtual else name
        scopes: list[frozenset[str] | None] = self._scopes(name) if virtual else [None]
        steps_out: list[dict[str, object]] = []
        chosen: str | None = None
        chosen_step: int | None = None
        for i, scope in enumerate(scopes):
            entry = (
                self._registry.select(prefix_key, scope, model, required_features)
                if chosen is None
                else None
            )
            names = sorted(scope) if scope is not None else ["*all*"]
            candidates = [
                _candidate_row(e)
                for e in self._registry.entries
                if (scope is None or e.name in scope)
                and self._registry.eligible(e, model, required_features)
            ]
            step_choice = entry.name if entry is not None else None
            if chosen is None and step_choice is not None:
                chosen, chosen_step = step_choice, i
            steps_out.append({"targets": names, "chosen": step_choice, "candidates": candidates})
        return {
            "model": name,
            "policy": policy,
            "known": self.known_model(name),
            "chosen": chosen,
            "chosen_step": chosen_step,
            "steps": steps_out,
        }


def _candidate_row(entry: BackendEntry) -> dict[str, object]:
    backend = entry.backend()
    state = backend.state.value if backend is not None else "unloaded"
    return {
        "name": entry.name,
        "state": state,
        "available": entry.is_ready(),
        "in_flight": entry.in_flight,
        "has_capacity": entry.has_capacity(),
    }
