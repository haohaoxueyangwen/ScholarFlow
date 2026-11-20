"""Create an isolated, explicitly synthetic browser-acceptance project and PDF."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fitz
from scholarflow import ProjectStage, ScholarFlowStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    allowed = (ROOT / ".tmp" / "eval").resolve()
    if not root.is_relative_to(allowed) or root == allowed or root.exists():
        raise ValueError("Fixture must be a new directory below .tmp/eval")
    root.mkdir(parents=True)
    data = root / "facts"
    store = ScholarFlowStore(data)
    for name in ("A", "B"):
        store.create_project_stage(ProjectStage(stage_id=f"stage-fixture-{name}", project_name=f"合成验收项目 {name}", title=f"阅读测试 {name}", goal="Only synthetic content; no real research data."))
    pdf = root / "synthetic.pdf"
    doc = fitz.open()
    for page_number in (1, 2):
        page = doc.new_page()
        page.insert_text((50, 70), f"Synthetic paper page {page_number}")
        page.insert_text((50, 110), "A coded aperture changes which synthetic measurements are recorded.")
        page.insert_text((50, 150), "This fixture has no real scientific claims or private content.")
    doc.save(pdf)
    doc.close()
    print(json.dumps({"data": str(data), "home": str(root / "workspaces"), "pdf": str(pdf)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
