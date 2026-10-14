"""NV CLOUD LP の停止 (2026-10-10) に合わせて記事の CTA を外した (2026-10-15) ことの確認。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
LP_HOSTS = ("nvcloud-lp", "cloud.northvalue-assets.net")


def test_templates_have_no_lp_url():
    d = json.loads((ROOT / "config" / "templates.json").read_text(encoding="utf-8"))
    assert "lp_url" not in d
    for k, v in d["cta_map"].items():
        assert "{lp_url}" not in v, k
        assert not any(h in v for h in LP_HOSTS), k
        # コメントの呼びかけは残す
        assert "コメント" in v, k


def test_cta_formats_without_lp_url():
    d = json.loads((ROOT / "config" / "templates.json").read_text(encoding="utf-8"))
    for v in d["cta_map"].values():
        v.format(lp_url=d.get("lp_url", ""))


def test_src_has_no_lp_url():
    for p in (ROOT / "src").glob("*.py"):
        s = p.read_text(encoding="utf-8")
        assert not any(h in s for h in LP_HOSTS), p.name


def test_validator_does_not_require_cta_url():
    from src import validator
    assert not hasattr(validator, "cta_url_is_embeddable")
