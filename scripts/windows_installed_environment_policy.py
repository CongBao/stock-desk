"""Verify the installed-acceptance environment and zero-runner boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Final, Mapping, Sequence


ENVIRONMENT: Final = "windows-installed-acceptance"


class EnvironmentPolicyError(ValueError):
    """Raised when the protected acceptance environment is not fail closed."""


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise EnvironmentPolicyError(f"{field} must be an object")
    return value


def verify_environment_policy(
    value: object, *, branch_policies: object, runners: object, repository: str
) -> None:
    document = _object(value, "environment")
    if document.get("name") != ENVIRONMENT:
        raise EnvironmentPolicyError("environment name mismatch")
    expected_url = (
        f"https://api.github.com/repos/{repository}/environments/{ENVIRONMENT}"
    )
    if document.get("url") != expected_url:
        raise EnvironmentPolicyError("environment repository binding mismatch")
    if document.get("can_admins_bypass") is not False:
        raise EnvironmentPolicyError(
            "environment administrators must not bypass policy"
        )
    deployment = _object(
        document.get("deployment_branch_policy"), "deployment_branch_policy"
    )
    if deployment != {
        "protected_branches": False,
        "custom_branch_policies": True,
    }:
        raise EnvironmentPolicyError(
            "environment must use an exact custom branch policy"
        )
    rules = document.get("protection_rules")
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        raise EnvironmentPolicyError("environment protection_rules must be an array")
    rule_types = {
        rule.get("type")
        for rule in rules
        if isinstance(rule, Mapping) and isinstance(rule.get("type"), str)
    }
    if "branch_policy" not in rule_types:
        raise EnvironmentPolicyError("environment branch policy rule is missing")
    policy_document = _object(branch_policies, "branch policies")
    policies = policy_document.get("branch_policies")
    if (
        policy_document.get("total_count") != 1
        or not isinstance(policies, Sequence)
        or isinstance(policies, (str, bytes))
        or len(policies) != 1
    ):
        raise EnvironmentPolicyError(
            "exactly one environment branch policy is required"
        )
    policy = _object(policies[0], "branch policy")
    if policy.get("name") != "main" or policy.get("type") != "branch":
        raise EnvironmentPolicyError("environment branch policy must be exact main")
    runner_document = _object(runners, "repository runners")
    runner_values = runner_document.get("runners")
    if (
        runner_document.get("total_count") != 0
        or not isinstance(runner_values, Sequence)
        or isinstance(runner_values, (str, bytes))
        or len(runner_values) != 0
    ):
        raise EnvironmentPolicyError(
            "persistent repository runners must not be registered"
        )


def bootstrap_payload(value: object | None = None) -> dict[str, object]:
    wait_timer = 0
    prevent_self_review = False
    reviewers: list[dict[str, object]] = []
    if value is not None:
        document = _object(value, "environment")
        rules = document.get("protection_rules", [])
        if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
            raise EnvironmentPolicyError(
                "environment protection_rules must be an array"
            )
        for raw_rule in rules:
            if not isinstance(raw_rule, Mapping):
                continue
            if raw_rule.get("type") == "wait_timer":
                raw_timer = raw_rule.get("wait_timer", 0)
                if not isinstance(raw_timer, int) or isinstance(raw_timer, bool):
                    raise EnvironmentPolicyError("existing wait timer is invalid")
                wait_timer = raw_timer
            if raw_rule.get("type") == "required_reviewers":
                prevent_self_review = raw_rule.get("prevent_self_review") is True
                raw_reviewers = raw_rule.get("reviewers", [])
                if (
                    not isinstance(raw_reviewers, Sequence)
                    or isinstance(raw_reviewers, (str, bytes))
                    or not raw_reviewers
                ):
                    raise EnvironmentPolicyError("existing reviewers are invalid")
                for raw_reviewer in raw_reviewers:
                    if not isinstance(raw_reviewer, Mapping):
                        raise EnvironmentPolicyError("existing reviewer is invalid")
                    reviewer = raw_reviewer.get("reviewer")
                    reviewer_type = raw_reviewer.get("type")
                    if not isinstance(reviewer, Mapping) or reviewer_type not in {
                        "User",
                        "Team",
                    }:
                        raise EnvironmentPolicyError("existing reviewer is invalid")
                    reviewer_id = reviewer.get("id")
                    if not isinstance(reviewer_id, int) or isinstance(
                        reviewer_id, bool
                    ):
                        raise EnvironmentPolicyError("existing reviewer is invalid")
                    reviewers.append({"type": reviewer_type, "id": reviewer_id})
    payload: dict[str, object] = {
        "wait_timer": wait_timer,
        "prevent_self_review": prevent_self_review,
        "can_admins_bypass": False,
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }
    if reviewers:
        payload["reviewers"] = reviewers
    return payload


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EnvironmentPolicyError("environment JSON is unreadable") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("environment", type=Path)
    verify.add_argument("--branch-policies", type=Path, required=True)
    verify.add_argument("--runners", type=Path, required=True)
    verify.add_argument("--repository", required=True)
    bootstrap = subparsers.add_parser("bootstrap-payload")
    bootstrap.add_argument("--existing", type=Path)
    subparsers.add_parser("bootstrap-branch-policy-payload")
    args = parser.parse_args(argv)
    if args.command == "verify":
        verify_environment_policy(
            _read_json(args.environment),
            branch_policies=_read_json(args.branch_policies),
            runners=_read_json(args.runners),
            repository=args.repository,
        )
        return 0
    if args.command == "bootstrap-branch-policy-payload":
        print(json.dumps({"name": "main", "type": "branch"}, sort_keys=True))
        return 0
    existing = _read_json(args.existing) if args.existing is not None else None
    print(json.dumps(bootstrap_payload(existing), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
