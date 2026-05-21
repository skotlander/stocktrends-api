from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_TERMS = [
    "Stock Trends Intermediate Momentum",
    "Stock Trends Indicator Model",
    "ST-IM (Stock Trends Intermediate Momentum)",
    "ST-IM (Stock Trends Indicator Model)",
]

TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".json",
    ".txt",
    ".yaml",
    ".yml",
}



def iter_repo_text_files():
    skip_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        ".claude",          # ✅ CRITICAL FIX
        "docs",             # ✅ EXCLUDE CONTRACT FILE
        "tests",            # ✅ EXCLUDE TEST FILE ITSELF
    }

    for path in REPO_ROOT.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.is_file() and path.suffix in TEXT_EXTENSIONS:
            yield path


def test_forbidden_stocktrends_semantic_terms_absent():
    violations = []

    for path in iter_repo_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for term in FORBIDDEN_TERMS:
            if term in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} contains forbidden term: {term}")

    assert not violations, "\n".join(violations)


def test_semantic_contract_defines_stim_correctly():
    contract = REPO_ROOT / "docs" / "STOCK_TRENDS_SEMANTIC_CONTRACT.md"
    text = contract.read_text(encoding="utf-8")

    assert "STIM = Stock Trends Inference Model" in text
    assert "forward return expectations" in text
    assert "statistical distributions" in text
    assert "4-week" in text
    assert "13-week" in text
    assert "40-week" in text


def test_stim_select_definition_in_semantic_contract():
    """STIM Select methodology must be defined in the semantic contract."""
    contract = REPO_ROOT / "docs" / "STOCK_TRENDS_SEMANTIC_CONTRACT.md"
    text = contract.read_text(encoding="utf-8")

    assert "STIM Select" in text, "STIM Select term must appear in the contract"
    assert "x4wk1" in text, "x4wk1 field must appear in the contract"
    assert "x13wk1" in text, "x13wk1 field must appear in the contract"
    assert "x40wk1" in text, "x40wk1 field must appear in the contract"
    assert "prob13wk" in text, "prob13wk field must appear in the contract"
    assert "2.19" in text, "13-week base-period mean (2.19) must appear in the contract"


def test_cognition_architecture_document_exists_and_defines_provider_doctrine():
    doc = REPO_ROOT / "docs" / "STOCK_TRENDS_COGNITION_ARCHITECTURE.md"
    text = doc.read_text(encoding="utf-8")

    assert "ST-IM Is The Baseline, Not The Ceiling" in text
    assert "Future Causal AI Is A First-Class Future Provider" in text
    assert "Provider-Agnostic Inference Concepts Come First" in text
    assert "uncertainty" in text
    assert "evidence" in text
    assert "auditability" in text


def test_signals_spec_exists_for_provider_agnostic_signal_evidence():
    spec = REPO_ROOT / "docs" / "SIGNALS_SPEC.md"
    text = spec.read_text(encoding="utf-8")

    assert "provider-agnostic" in text
    assert "Stock Trends classifications" in text
    assert "future Causal AI providers" in text
    assert "not the traditional Wilder RSI oscillator" in text


def test_no_stc_per_call_in_route_files():
    """No route description= strings may contain hardcoded 'STC per call'."""
    violations = []
    for path in iter_repo_text_files():
        if path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "STC per call" in text:
            violations.append(str(path.relative_to(REPO_ROOT)))
    assert not violations, (
        "These files still contain hardcoded 'STC per call' — "
        "replace with 'Fetch /v1/pricing/catalog for current STC cost.':\n"
        + "\n".join(violations)
    )
