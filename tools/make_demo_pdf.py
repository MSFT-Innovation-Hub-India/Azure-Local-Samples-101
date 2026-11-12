"""Create a small synthetic PDF, never a copy of customer documents."""

import argparse
from pathlib import Path

from reportlab.pdfgen import canvas


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scenario", choices=["operations", "manufacturing", "retail"])
    args = parser.parse_args()
    output = args.output or (
        Path("scenarios") / args.scenario / "documents" / "demo-guide.pdf"
        if args.scenario else Path(".local") / "documents" / "demo-guide.pdf"
    )
    pages = {
        "operations": [
            ["Harbor Assistant - Synthetic Operations Guide", "The service recovery code is mango.",
             "Recovery requires approval from the operations team."],
            ["Backup policy", "Backups are taken every six hours and retained for fourteen days.",
             "Restore tests are performed every month."],
        ],
        "manufacturing": [
            ["Manufacturing maintenance - synthetic demo", "The approval code is cobalt.",
             "Press inspections are scheduled every ten days.",
             "Only the maintenance supervisor can approve maintenance requests."],
        ],
        "retail": [
            ["Retail returns - synthetic demo", "The approval code is amber.",
             "Items may be returned within twenty-one days of purchase.",
             "An original receipt is required. Only the store manager can approve exceptions."],
        ],
    }[args.scenario or "operations"]
    output.parent.mkdir(parents=True, exist_ok=True)
    document = canvas.Canvas(str(output))
    for lines in pages:
        for line, text in enumerate(lines):
            document.drawString(50, 770 - 30 * line, text)
        document.showPage()
    document.save()
    print(f"Created synthetic PDF: {output}")


if __name__ == "__main__":
    main()
