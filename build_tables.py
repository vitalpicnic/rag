"""Build verified SQLite observations locally, without API keys."""
from pathlib import Path
import argparse
from rag.tables import build_tables, render_chart, CHOICES

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chart', type=Path, help='Save a sample comparison PNG to a new file')
    args = parser.parse_args()
    if args.chart and args.chart.exists():
        parser.error('Chart already exists; choose a new filename')
    root = Path(__file__).resolve().parent
    print({'verified_observations': build_tables(root), 'api_calls': 0})
    if args.chart:
        image, _ = render_chart(root, None, CHOICES[-1])
        with args.chart.open('xb') as handle:
            handle.write(image)
