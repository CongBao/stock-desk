from stock_desk.backtest.export import _canonical_json, _csv_text
from stock_desk.security.redaction import scoped_log_redaction


def test_csv_and_json_exports_compose_exact_redaction_with_injection_defense() -> None:
    secret = "configured-export-secret-value"
    similar_ordinary_text = "configured-export-secret"

    with scoped_log_redaction(secret):
        csv_value = _csv_text(f"={secret}")
        json_value = _canonical_json(
            {"secret": secret, "ordinary": similar_ordinary_text}
        )

    assert csv_value.startswith("'=")
    assert secret not in csv_value
    assert secret.encode() not in json_value
    assert similar_ordinary_text.encode() in json_value
