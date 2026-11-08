#!/usr/bin/env python3
"""Require every reviewed lock regression to execute and pass, with no skips."""

import collections
import json
import pathlib
import sys


EXPECTED_TOP = {
    "TestNativeDeploymentOrdinaryLockRemainsFailFast",
    "TestNativeGuardBootLocksSerializeTwoWaiters",
    "TestNativeGuardBootLockCancellationAndDeadline",
    "TestNativeDeploymentLockCancellationAfterAcquisitionClosesFD",
    "TestNativeDeploymentLockRejectsUnsafeCustody",
    "TestNativeDeploymentLockRejectsPathAndModeDrift",
    "TestNativeDeploymentLockPreservesUnexpectedFlockError",
}
EXPECTED_EVENTS = 15
EXPECTED_PACKAGE = "example.invalid/ovn-lock-qualification/lock"


def validate(path):
    events = [json.loads(line) for line in pathlib.Path(path).read_text().splitlines() if line.strip()]
    if any(event.get("Action") in {"fail", "skip"} for event in events):
        raise ValueError("failure or skip recorded")
    if any(event.get("Package") != EXPECTED_PACKAGE for event in events):
        raise ValueError("unexpected package in test events")
    package_passes = [event for event in events
                      if event.get("Action") == "pass" and "Test" not in event]
    if len(package_passes) != 1:
        raise ValueError("exactly one package-level passing terminal event is required")
    started = collections.Counter(
        (event.get("Package"), event["Test"])
        for event in events if event.get("Action") == "run" and "Test" in event
    )
    passed = collections.Counter(
        (event.get("Package"), event["Test"])
        for event in events if event.get("Action") == "pass" and "Test" in event
    )
    top = {name for _, name in passed if "/" not in name}
    if not started or started != passed or any(count != 1 for count in started.values()):
        raise ValueError("each started test must have exactly one passing terminal event")
    if top != EXPECTED_TOP or sum(passed.values()) != EXPECTED_EVENTS:
        raise ValueError("the complete reviewed test set did not execute")
    print(json.dumps({"scope": "isolated boot-lock subset", "top_level_pass": len(top),
                      "pass_events": sum(passed.values()), "failures": 0, "skips": 0}))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: assert_test_events.py GO_TEST_JSONL")
    validate(sys.argv[1])
