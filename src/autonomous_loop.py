#!/usr/bin/env python3
"""
Autonomous Loop runtime for agent-reinforcement-system.

A minimal executable skeleton for Module 3:
Observe -> Orient -> Decide -> Act -> Verify -> Record -> Loop/Exit

This implementation is intentionally small and framework-agnostic.
It provides:
- GoalFrame / LoopState dataclasses
- bounded state transitions
- JSON load/save helpers
- a pluggable policy surface for custom observe/orient/decide/act/verify/record logic
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


VALID_STATUSES = {"initialized", "active", "waiting_human", "blocked", "done", "aborted"}
VALID_STEPS = ["observe", "orient", "decide", "act", "verify", "record"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class GoalFrame:
    goal_id: str
    name: str
    goal: str
    success_criteria: list[str]
    constraints: list[str]
    non_goals: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    external_side_effects: bool = False
    escalation_points: list[str] = field(default_factory=list)
    abort_conditions: list[str] = field(default_factory=list)
    max_iterations: int = 12
    max_consecutive_failures: int = 3
    owner: str = "user"
    created_at: str = field(default_factory=now_iso)


@dataclass
class LoopState:
    loop_id: str
    goal_id: str
    status: str = "initialized"
    iteration: int = 0
    current_step: str = "observe"
    last_observation: str = ""
    working_hypotheses: list[str] = field(default_factory=list)
    selected_action: str = ""
    verification_plan: list[str] = field(default_factory=list)
    verification_result: str = "unknown"
    blockers: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    needs_human_input: bool = False
    consecutive_failures: int = 0
    last_error: str | None = None
    memory_write_required: bool = False
    next_iteration_hint: str | None = None
    updated_at: str = field(default_factory=now_iso)


class AutonomousLoopError(RuntimeError):
    pass


class DefaultPolicy:
    """Reference policy. Replace methods for real integration."""

    def observe(self, goal: GoalFrame, state: LoopState) -> dict[str, Any]:
        return {
            "observation": f"Goal: {goal.goal}",
            "evidence": ["no external observation adapter configured"],
        }

    def orient(self, goal: GoalFrame, state: LoopState, observation: dict[str, Any]) -> dict[str, Any]:
        return {
            "hypotheses": [
                "The smallest next step should reduce uncertainty before heavy action.",
            ],
            "explanation": "No domain-specific orient adapter configured; using minimal first-principles fallback.",
        }

    def decide(self, goal: GoalFrame, state: LoopState, orientation: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "request_or_prepare_next_minimal_step",
            "verification_plan": ["confirm the selected action reduces uncertainty or advances completion"],
            "needs_human_input": goal.external_side_effects and goal.risk_level == "high",
        }

    def act(self, goal: GoalFrame, state: LoopState, decision: dict[str, Any]) -> dict[str, Any]:
        if decision.get("needs_human_input"):
            return {"performed": False, "result": "waiting for human authorization"}
        return {"performed": True, "result": f"executed: {decision['action']}"}

    def verify(self, goal: GoalFrame, state: LoopState, action_result: dict[str, Any]) -> dict[str, Any]:
        if not action_result.get("performed"):
            return {"result": "unknown", "reason": action_result.get("result", "not performed")}
        return {"result": "pass", "reason": action_result.get("result", "ok")}

    def record(self, goal: GoalFrame, state: LoopState, verification: dict[str, Any]) -> dict[str, Any]:
        return {
            "memory_items": [
                {
                    "type": "episodic",
                    "text": f"Iteration {state.iteration}: {verification.get('reason', '')}",
                }
            ]
        }


class AutonomousLoop:
    def __init__(self, goal: GoalFrame, state: LoopState | None = None, policy: Any | None = None):
        self.goal = goal
        self.state = state or LoopState(loop_id=str(uuid.uuid4()), goal_id=goal.goal_id)
        self.policy = policy or DefaultPolicy()
        self._validate_goal()
        self._validate_state()

    def _validate_goal(self) -> None:
        if not self.goal.goal_id or not self.goal.goal:
            raise AutonomousLoopError("GoalFrame missing required content")
        if self.goal.risk_level not in {"low", "medium", "high"}:
            raise AutonomousLoopError("Invalid risk_level")
        if self.goal.owner not in {"user", "agent"}:
            raise AutonomousLoopError("Invalid owner")
        if self.goal.max_iterations < 1 or self.goal.max_consecutive_failures < 1:
            raise AutonomousLoopError("Invalid loop limits")

    def _validate_state(self) -> None:
        if self.state.status not in VALID_STATUSES:
            raise AutonomousLoopError(f"Invalid status: {self.state.status}")
        if self.state.current_step not in VALID_STEPS:
            raise AutonomousLoopError(f"Invalid step: {self.state.current_step}")

    def _touch(self) -> None:
        self.state.updated_at = now_iso()

    def step(self) -> LoopState:
        if self.state.status in {"done", "blocked", "waiting_human", "aborted"}:
            return self.state

        if self.state.iteration >= self.goal.max_iterations:
            self.state.status = "aborted"
            self.state.last_error = "max_iterations_exceeded"
            self._touch()
            return self.state

        self.state.status = "active"
        self.state.iteration += 1

        observation = self._run_observe()
        orientation = self._run_orient(observation)
        decision = self._run_decide(orientation)

        if decision.get("needs_human_input"):
            self.state.status = "waiting_human"
            self.state.needs_human_input = True
            self.state.next_iteration_hint = "await human approval or decision"
            self._touch()
            return self.state

        action_result = self._run_act(decision)
        verification = self._run_verify(action_result)
        self._run_record(verification)
        self._resolve_terminal_state(verification)
        self._touch()
        return self.state

    def run(self) -> LoopState:
        while self.state.status == "initialized" or self.state.status == "active":
            self.step()
        return self.state

    def _run_observe(self) -> dict[str, Any]:
        self.state.current_step = "observe"
        obs = self.policy.observe(self.goal, self.state)
        self.state.last_observation = obs.get("observation", "")
        return obs

    def _run_orient(self, observation: dict[str, Any]) -> dict[str, Any]:
        self.state.current_step = "orient"
        orient = self.policy.orient(self.goal, self.state, observation)
        self.state.working_hypotheses = orient.get("hypotheses", [])
        return orient

    def _run_decide(self, orientation: dict[str, Any]) -> dict[str, Any]:
        self.state.current_step = "decide"
        decision = self.policy.decide(self.goal, self.state, orientation)
        self.state.selected_action = decision.get("action", "")
        self.state.verification_plan = decision.get("verification_plan", [])
        self.state.needs_human_input = bool(decision.get("needs_human_input", False))
        return decision

    def _run_act(self, decision: dict[str, Any]) -> dict[str, Any]:
        self.state.current_step = "act"
        return self.policy.act(self.goal, self.state, decision)

    def _run_verify(self, action_result: dict[str, Any]) -> dict[str, Any]:
        self.state.current_step = "verify"
        verification = self.policy.verify(self.goal, self.state, action_result)
        result = verification.get("result", "unknown")
        if result not in {"pass", "fail", "unknown"}:
            raise AutonomousLoopError(f"Invalid verification result: {result}")
        self.state.verification_result = result
        if result == "fail":
            self.state.consecutive_failures += 1
        elif result == "pass":
            self.state.consecutive_failures = 0
        return verification

    def _run_record(self, verification: dict[str, Any]) -> dict[str, Any]:
        self.state.current_step = "record"
        self.state.memory_write_required = True
        record = self.policy.record(self.goal, self.state, verification)
        self.state.memory_write_required = False
        return record

    def _resolve_terminal_state(self, verification: dict[str, Any]) -> None:
        if self.state.consecutive_failures >= self.goal.max_consecutive_failures:
            self.state.status = "blocked"
            self.state.last_error = "max_consecutive_failures_exceeded"
            self.state.next_iteration_hint = "revise hypothesis or escalate"
            return

        if verification.get("result") == "pass":
            self.state.status = "done"
            self.state.next_iteration_hint = "goal verified complete"
            return

        if verification.get("result") == "unknown":
            self.state.status = "waiting_human" if self.state.needs_human_input else "active"
            self.state.next_iteration_hint = "need verification or authorization"
            return

        self.state.status = "active"
        self.state.next_iteration_hint = "retry with revised hypothesis"

    @staticmethod
    def from_files(goal_path: str, state_path: str | None = None) -> "AutonomousLoop":
        goal = GoalFrame(**json.load(open(goal_path)))
        state = LoopState(**json.load(open(state_path))) if state_path and os.path.exists(state_path) else None
        return AutonomousLoop(goal, state)

    def save_state(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(asdict(self.state), f, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Loop runtime")
    parser.add_argument("goal", help="Path to goal frame JSON")
    parser.add_argument("--state", help="Path to existing loop state JSON")
    parser.add_argument("--out", help="Write resulting loop state to file")
    parser.add_argument("--mode", choices=["step", "run"], default="run")
    args = parser.parse_args()

    loop = AutonomousLoop.from_files(args.goal, args.state)
    result = loop.step() if args.mode == "step" else loop.run()

    payload = asdict(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.out:
        loop.save_state(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
