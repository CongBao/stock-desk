from scripts.check_public_tree import forbidden_paths


def test_internal_paths_are_rejected() -> None:
    tracked = ["src/stock_desk/main.py", "openspec/config.yaml", "outputs/review.md"]
    assert forbidden_paths(tracked) == ["openspec/config.yaml", "outputs/review.md"]


def test_public_paths_are_allowed() -> None:
    tracked = ["README.md", "src/stock_desk/main.py", "docs/architecture.md"]
    assert forbidden_paths(tracked) == []
