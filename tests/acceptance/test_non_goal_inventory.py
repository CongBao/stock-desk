from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from pathlib import PurePosixPath

from stock_desk.main import create_app


ROOT = Path(__file__).resolve().parents[2]

# These are product-capability identifiers and positive feature claims, not words used
# in safety rules or documentation that explicitly describes an absent capability.
NON_GOALS = {
    "N-001": (
        "broker/live ordering",
        r"(?<![a-z0-9])(?:broker_(?:order|client|connection|integration)|live_(?:order|trading)|auto(?:matic)?_trading|submit_broker_order|order_submission|自动下单)(?![a-z0-9])",
    ),
    "N-002": (
        "shared-capital portfolio",
        r"(?<![a-z0-9])(?:shared_(?:cash|capital)(?:_portfolio)?|portfolio_(?:capital|rebalance)|position_competition|共享资金组合)(?![a-z0-9])",
    ),
    "N-003": (
        "realtime/tick/Level2",
        r"(?<![a-z0-9])(?:real_?time_(?:quote|feed|push)|tick_(?:stream|feed)|level_?2_(?:feed|market|quote|data)|order_book_depth|transaction_level_feed|逐笔行情)(?![a-z0-9])",
    ),
    "N-004": (
        "target price/specific allocation",
        r"(?<![a-z0-9])(?:target_price(?:_input)?|position_(?:percentage|sizing)|allocation_(?:advice|instruction)|specific_allocation|personalized_investment_recommendation|目标价|仓位建议)(?![a-z0-9])",
    ),
    "N-005": (
        "second native product UI",
        r"(?<![a-z0-9])(?:native_(?:desktop|product)_ui|desktop_client|electron_(?:app|window)|tauri_(?:app|window)|原生产品界面)(?![a-z0-9])",
    ),
    "N-006": (
        "accounts/RBAC/subscription/billing",
        r"(?<![a-z0-9])(?:login|sign_?up|account_registration|multi_user_account|organization|rbac(?:_role)?|subscription(?:_plan)?|billing(?:_portal)?|payment(?:_portal)?|invoice|invoicing|计费入口)(?![a-z0-9])",
    ),
    "N-007": (
        "dynamic screening",
        r"(?<![a-z0-9])(?:stock_screener|dynamic_(?:screen|screener|screening)|(?:screening_)?rules?_builder|动态选股器)(?![a-z0-9])",
    ),
    "N-008": (
        "condition-selection/color-K",
        r"(?<![a-z0-9])(?:condition_selection(?:_formula)?|color_?k(?:_formula)?|五彩k线(?:编辑器)?)(?![a-z0-9])",
    ),
    "N-009": (
        "drawing/multi-stock/multi-period linkage",
        r"(?<![a-z0-9])(?:chart_drawing|drawing_(?:tool|toolbar)|multi_stock(?:_view)?|multi_period_(?:link|linkage)|linked_periods|多周期联动入口)(?![a-z0-9])",
    ),
    "N-010": (
        "AI formula generation/explanation/repair",
        r"(?<![a-z0-9])(?:formula_(?:generation|explanation|repair)_ai|ai_formula_(?:generation|generate|explain|explanation|repair)|formula_generation_ai|automatic_formula_repair|formula_explanation|prompt_based_formula_authoring|ai公式(?:生成|解释|修复)入口)(?![a-z0-9])",
    ),
}

NEGATION = re.compile(
    r"(?<![a-z0-9])(?:no|not|neither|nor|without|absence|absent|unavailable|disabled|prohibited|unsupported|excluded?|does_not|do_not|never|cannot|can_not|doesn_t|isn_t|aren_t|wasn_t|weren_t|won_t|hasn_t|haven_t|hadn_t)(?![a-z0-9])"
    r"|不(?:提供|支持|包含|建设|连接|执行|展示|做)|没有|无(?:此|该)",
    re.IGNORECASE,
)
CLAUSE_BOUNDARY = re.compile(
    r"[.;\n]+|\b(?:but|however|whereas|while)\b|但(?:是)?|然而|而(?=\s)",
    re.IGNORECASE,
)
POSITIVE_CLAIM = re.compile(
    r"(?<![a-z0-9])(?:available|supports?|enabled|offers?|provides?|exposes?|includes?|has|contains?|allows?)(?![a-z0-9])",
    re.IGNORECASE,
)


def normalize_inventory_text(text: str) -> str:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", camel_split).strip("_").lower()


def find_non_goal_exposures(text: str, *, claims: bool) -> set[str]:
    segments = CLAUSE_BOUNDARY.split(text) if claims else [text]
    exposed: set[str] = set()
    for segment in segments:
        normalized = normalize_inventory_text(segment)
        if not normalized:
            continue
        clauses = normalized.split("_and_") if claims else [normalized]
        negated_clauses = [bool(NEGATION.search(clause)) for clause in clauses]
        positive_clauses = [bool(POSITIVE_CLAIM.search(clause)) for clause in clauses]
        inherited_negation = False
        for index, clause in enumerate(clauses):
            clause_is_negated = negated_clauses[index]
            positive_claim = positive_clauses[index]
            if clause_is_negated:
                inherited_negation = True
            elif positive_claim:
                inherited_negation = False
            next_negation = next(
                (
                    offset
                    for offset, negated in enumerate(negated_clauses[index + 1 :], 1)
                    if negated
                ),
                None,
            )
            next_positive = next(
                (
                    offset
                    for offset, positive in enumerate(positive_clauses[index + 1 :], 1)
                    if positive
                ),
                None,
            )
            negated_by_trailing_qualifier = (
                not positive_claim
                and next_negation is not None
                and (next_positive is None or next_negation < next_positive)
            )
            if claims and (
                clause_is_negated or inherited_negation or negated_by_trailing_qualifier
            ):
                continue
            for non_goal_id, (_, pattern) in NON_GOALS.items():
                if non_goal_id == "N-006" and re.search(
                    r"bao_?stock_performs_login_logout", clause
                ):
                    continue
                if re.search(pattern, clause, re.IGNORECASE):
                    exposed.add(non_goal_id)
    return exposed


def _openapi_inventories() -> tuple[str, str]:
    schema = create_app().openapi()
    structural: list[str] = []
    claims: list[str] = []
    free_text_fields = {"description", "summary", "title"}

    def free_text(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                claims.append(str(key))
                free_text(child)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                free_text(child)
            return
        if value is not None:
            claims.append(str(value))

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                structural.append(str(key))
                if str(key) in free_text_fields:
                    free_text(child)
                else:
                    visit(child)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                visit(child)
            return
        if value is None:
            return
        structural.append(str(value))

    visit(schema)
    return "\n".join(structural).lower(), "\n".join(claims).lower()


def _openapi_inventory() -> str:
    structural, _claims = _openapi_inventories()
    return structural


def _public_ui_source_paths(source_root: Path) -> tuple[Path, ...]:
    excluded_parts = {
        "__fixtures__",
        "__tests__",
        "fixtures",
        "stories",
        "test",
        "tests",
    }

    def shipped(path: Path) -> bool:
        lowered_parts = {
            part.lower() for part in path.relative_to(source_root).parts[:-1]
        }
        name = path.name.lower()
        return (
            not lowered_parts.intersection(excluded_parts)
            and not any(
                marker in name
                for marker in (".test.", ".spec.", ".stories.", ".story.", ".fixture.")
            )
            and not name.startswith("testfixtures.")
        )

    return tuple(
        path
        for path in sorted((*source_root.rglob("*.ts"), *source_root.rglob("*.tsx")))
        if shipped(path)
    )


def _tracked_repo_paths(root: Path) -> frozenset[str]:
    output = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"],
        stderr=subprocess.STDOUT,
    )
    return frozenset(os.fsdecode(item) for item in output.split(b"\0") if item)


def _public_doc_paths(
    root: Path, tracked_paths: set[str] | frozenset[str]
) -> tuple[Path, ...]:
    excluded_prefixes = (
        "openspec/",
        "outputs/",
        ".agents/",
        ".codex/",
        ".superpowers/",
        "work/",
        "docs/superpowers/",
    )
    public: list[Path] = []
    for relative in sorted(tracked_paths):
        pure = PurePosixPath(relative)
        if pure.suffix.lower() != ".md" or relative.startswith(excluded_prefixes):
            continue
        candidate = root.joinpath(*pure.parts)
        if candidate.is_file():
            public.append(candidate)
    return tuple(public)


SURFACE_FILES = {
    "api": tuple((ROOT / "src" / "stock_desk" / "api").rglob("*.py")),
    "worker": tuple((ROOT / "src" / "stock_desk").rglob("*worker*.py"))
    + tuple((ROOT / "src" / "stock_desk" / "tasks").rglob("*.py")),
    "web_ui": _public_ui_source_paths(ROOT / "web" / "src"),
    "docs": _public_doc_paths(ROOT, _tracked_repo_paths(ROOT)),
}


def _source_inventory(paths: tuple[Path, ...]) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def test_every_non_goal_is_checked_on_every_declared_public_surface() -> None:
    openapi_structural, openapi_claims = _openapi_inventories()
    inventories = {
        "openapi-structure": (openapi_structural, False),
        "openapi-claims": (openapi_claims, True),
        "api": (_source_inventory(SURFACE_FILES["api"]), False),
        "worker": (_source_inventory(SURFACE_FILES["worker"]), False),
        "web_ui": (_source_inventory(SURFACE_FILES["web_ui"]), False),
        "docs": (_source_inventory(SURFACE_FILES["docs"]), True),
    }

    assert set(NON_GOALS) == {f"N-{number:03d}" for number in range(1, 11)}
    failures: list[str] = []
    for surface, (inventory, claims) in inventories.items():
        exposed = find_non_goal_exposures(inventory, claims=claims)
        for non_goal_id in sorted(exposed):
            failures.append(
                f"{non_goal_id} exposes {NON_GOALS[non_goal_id][0]} on {surface}"
            )
    assert failures == []
