#!/usr/bin/env python3
"""
Import & symbol-wiring auditor for Opportunity Radar.

Usage:
  python diagnostics/scripts/import_audit.py --repo PATH [--base-url http://localhost:8080]

Checks (best-effort, static heuristics):
- Frontend fetches include ?symbol= for API calls.
- FastAPI endpoints accept a symbol parameter where required.
- Agents registry includes expected agents (rvol_spike, trend_score).
- No NaN/Inf in signals serialization (looks for numeric hygiene helper usage).

This is a static scanner; it won't execute code.
"""
import argparse, re, sys
from pathlib import Path

API_PATHS = [
    r"/signals", r"/signals_tf", r"/findings", r"/debug/state", r"/debug/features", r"/agents", r"/agents/run-now", r"/db-health", r"/health"
]

def find_files(repo: Path, exts):
    for ext in exts:
        yield from repo.rglob(f"*{ext}")

def scan_frontend_symbol_wiring(repo: Path):
    issues = []
    for file in find_files(repo, [".js", ".jsx", ".ts", ".tsx", ".html"]):
        text = file.read_text(errors="ignore")
        # Look for fetch/axios/httpx-style calls to our endpoints
        for path in API_PATHS:
            if path in text:
                # If the path appears, require a symbol= param in the same line/block
                # Heuristic: within 120 chars of the endpoint string
                for m in re.finditer(re.escape(path), text):
                    start, end = m.start(), m.end()
                    window = text[max(0,start-80):min(len(text), end+120)]
                    if "symbol=" not in window and "symbol%3D" not in window:
                        issues.append(f"{file}: '{path}' appears without '?symbol=' nearby (heuristic).")
                        break
    return issues

def scan_backend_symbol_params(repo: Path):
    issues = []
    for file in find_files(repo, [".py"]):
        text = file.read_text(errors="ignore")
        if "FastAPI" in text or "APIRouter" in text or "@app.get" in text or "@router.get" in text:
            # Basic check: if an endpoint path is defined, symbol should appear in function signature for key endpoints
            for path in ["/signals", "/signals_tf", "/findings", "/debug/state", "/debug/features"]:
                if path in text and "def " in text:
                    # if path occurs, look for 'symbol:' or 'symbol: str' nearby
                    if re.search(r"def\s+\w+\s*\([^)]*symbol\s*:", text) is None:
                        issues.append(f"{file}: endpoint '{path}' defined but function signature may lack 'symbol' parameter.")
    return issues

def scan_agents_registry(repo: Path):
    expected = {"rvol_spike", "trend_score"}
    present = set()
    for file in find_files(repo, [".py"]):
        text = file.read_text(errors="ignore")
        # naive: agent registry lines like AGENTS = {...} or list of names
        if "AGENTS" in text or "agents" in text:
            for name in expected:
                if name in text:
                    present.add(name)
    missing = expected - present
    return list(sorted(missing))

def scan_numeric_hygiene(repo: Path):
    hints = []
    for file in find_files(repo, [".py"]):
        text = file.read_text(errors="ignore")
        if "_num" in text or "math.isfinite" in text:
            hints.append(str(file))
    return hints

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True, help="Path to repo to scan")
    ap.add_argument("--base-url", default="http://localhost:8080")
    args = ap.parse_args()

    if not args.repo.exists():
        print(f"[audit] repo path does not exist: {args.repo}", file=sys.stderr)
        sys.exit(2)

    print("[audit] scanning frontend symbol wiring...")
    fw = scan_frontend_symbol_wiring(args.repo)
    if fw:
        print(f"[audit] symbol wiring issues ({len(fw)}):")
        for i in fw[:50]:
            print("  -", i)
    else:
        print("[audit] symbol wiring looks OK (heuristic)")

    print("\n[audit] scanning backend endpoints for 'symbol' parameter...")
    be = scan_backend_symbol_params(args.repo)
    if be:
        print(f"[audit] backend symbol-param issues ({len(be)}):")
        for i in be[:50]:
            print("  -", i)
    else:
        print("[audit] backend symbol params look OK (heuristic)")

    print("\n[audit] checking agents registry coverage...")
    miss = scan_agents_registry(args.repo)
    if miss:
        print("[audit] missing expected agents:", ", ".join(miss))
    else:
        print("[audit] expected agents are referenced somewhere in the repo")

    print("\n[audit] numeric hygiene hints (_num / isfinite usage):")
    hints = scan_numeric_hygiene(args.repo)
    if hints:
        print("  present in:")
        for h in hints[:30]:
            print("   -", h)
    else:
        print("  no obvious helpers found (consider adding an _num guard)")

if __name__ == "__main__":
    main()
