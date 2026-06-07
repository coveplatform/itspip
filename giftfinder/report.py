"""Turn a list of Findings into the shock number — the thing you screenshot.

Dedupes reminder emails about the same card, totals by currency, and prints
a clean summary. Falls back to plain text if `rich` isn't installed.
"""
from collections import defaultdict
from typing import Dict, List

from .models import Finding, KIND_LABELS

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    _RICH = True
except ImportError:  # graceful degradation
    _RICH = False


def dedupe(findings: List[Finding]) -> List[Finding]:
    """Collapse duplicate emails about the same card; keep the most confident."""
    best: Dict[str, Finding] = {}
    for f in findings:
        cur = best.get(f.dedupe_key)
        if cur is None or f.confidence > cur.confidence:
            best[f.dedupe_key] = f
    return sorted(
        best.values(),
        key=lambda f: (f.amount or 0, f.confidence),
        reverse=True,
    )


def totals_by_currency(findings: List[Finding]) -> Dict[str, float]:
    totals: Dict[str, float] = defaultdict(float)
    for f in findings:
        if f.amount is not None:
            totals[f.currency] += f.amount
    return dict(totals)


def _fmt_money(amount, currency) -> str:
    if amount is None:
        return "?"
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency, "")
    return f"{sym}{amount:,.2f}"


def render(findings: List[Finding], scanned: int) -> None:
    findings = dedupe(findings)
    totals = totals_by_currency(findings)
    unknown = sum(1 for f in findings if f.amount is None)

    if not _RICH:
        _render_plain(findings, totals, unknown, scanned)
        return

    console = Console()
    table = Table(box=box.SIMPLE_HEAVY, show_lines=False, expand=True)
    table.add_column("Amount", justify="right", style="bold green", no_wrap=True)
    table.add_column("Type")
    table.add_column("Brand", style="cyan")
    table.add_column("From subject", overflow="ellipsis", max_width=46)
    table.add_column("Conf", justify="right")
    table.add_column("Flags", no_wrap=True)

    for f in findings:
        flags = []
        if f.code_present:
            flags.append("code")
        if f.expires_text:
            flags.append(f"exp:{f.expires_text}")
        table.add_row(
            _fmt_money(f.amount, f.currency),
            KIND_LABELS.get(f.kind, f.kind),
            f.brand,
            f.subject or "(no subject)",
            f"{int(f.confidence * 100)}%",
            " ".join(flags),
        )

    console.print()
    console.print(table)

    total_line = "  ".join(
        f"[bold green]{_fmt_money(v, k)}[/bold green]" for k, v in totals.items()
    ) or "[dim]$0.00[/dim]"
    extra = f"  [dim](+{unknown} found with no clear amount)[/dim]" if unknown else ""
    headline = (
        f"You're sitting on {total_line} in forgotten money.{extra}\n"
        f"[dim]{len(findings)} items across {scanned} scanned emails — "
        f"all detected locally, nothing left your machine.[/dim]"
    )
    console.print(Panel(headline, border_style="green", box=box.ROUNDED, padding=(1, 2)))


def _render_plain(findings, totals, unknown, scanned) -> None:
    print()
    for f in findings:
        flags = []
        if f.code_present:
            flags.append("code")
        if f.expires_text:
            flags.append(f"exp:{f.expires_text}")
        print(
            f"  {_fmt_money(f.amount, f.currency):>10}  "
            f"{KIND_LABELS.get(f.kind, f.kind):<16} {f.brand:<14} "
            f"{int(f.confidence*100):>3}%  {f.subject[:46]}  {' '.join(flags)}"
        )
    total = "  ".join(_fmt_money(v, k) for k, v in totals.items()) or "$0.00"
    extra = f" (+{unknown} with no clear amount)" if unknown else ""
    print("\n" + "=" * 70)
    print(f"  You're sitting on {total} in forgotten money.{extra}")
    print(f"  {len(findings)} items across {scanned} scanned emails — all local.")
    print("=" * 70 + "\n")
