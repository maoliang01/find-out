from app.services.article_signals import content_fingerprint, normalize_for_fingerprint
from app.services.risk_detection import detect_risk


def test_content_fingerprint_ignores_presentation_noise():
    first = "摘要：简短摘要\nXX项目出现交付延期。\nhttps://example.com/a"
    second = "XX 项目 出现交付延期！"

    assert normalize_for_fingerprint(first) == normalize_for_fingerprint(second)
    assert content_fingerprint("标题", first) == content_fingerprint("标题", second)


def test_risk_detector_returns_category_and_evidence():
    result = detect_risk("监管部门已对该公司立案调查，项目交付延期。")

    assert result["categories"] == ["delivery", "regulatory"]
    assert result["severity"] == 70
    assert {item["term"] for item in result["evidence"]} == {"立案", "延期"}
