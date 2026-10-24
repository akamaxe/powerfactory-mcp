"""Generates the local references in <workspace>\\_ref from the PowerFactory installation.

    python -I -m powerfactory_mcp.setup_ref [--out DIR] [--force]

- pf_attributes.tsv: input attributes per class, read from datascheme.db (does not start the engine).
- PythonReference_en.txt, DplReference_en.txt and ErrorCodeReference_en.txt: text of the PDFs in Help\\,
  searchable with Grep. They are generated on each machine because they are DIgSILENT documentation and
  are not redistributed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from powerfactory_mcp.config import load_config

# Management attributes that appear in almost every class and add nothing to the model.
SKIP = {"loc_name", "chr_name", "for_name", "dat_src", "gnrl_modby", "gnrl_modif", "dataExtBlob", "root_id",
        "fold_id", "charact", "contents", "iSchemeStatus", "appr_modby", "appr_modif", "appr_status", "order",
        "addoptions", "cimRdfId"}
PDFS = ("PythonReference_en", "DplReference_en", "ErrorCodeReference_en")


def build_catalog(install_dir: Path, out: Path) -> int:
    db = install_dir / "datascheme.db"
    if not db.exists():
        raise FileNotFoundError(f"{db} does not exist")
    con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        build = con.execute("select max(Build) from Scheme").fetchone()[0]
        classes = con.execute("select Id, Name from Class where Build=? order by Name", (build,)).fetchall()
        n = 0
        with out.open("w", encoding="utf-8", newline="\n") as f:
            f.write(f"# Input attributes per class (datascheme.db from {install_dir.name}, build {build}).\n")
            f.write("# Names only: describe_class gives the description and unit live.\n")
            f.write("class\tattribute\tdescription\tunit\n")
            for cid, cls in classes:
                rows = con.execute("select Name from Attribute where Build=? and ClassId=? order by Id", (build, cid))
                for (name,) in rows:
                    if name not in SKIP:
                        f.write(f"{cls}\t{name}\t\t\n")
                        n += 1
    finally:
        con.close()
    return n


def extract_pdf(pdf: Path, out: Path) -> int:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        out.write_text("\n".join(page.get_text() for page in doc), encoding="utf-8")
        return doc.page_count


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output folder (default <workspace>\\_ref)")
    ap.add_argument("--force", action="store_true", help="regenerate even if they already exist")
    a = ap.parse_args(argv)

    cfg = load_config()
    out = Path(a.out) if a.out else cfg.workspace / "_ref"
    out.mkdir(parents=True, exist_ok=True)
    print(f"PowerFactory: {cfg.install_dir}\nOutput: {out}")
    failures = 0

    tsv = out / "pf_attributes.tsv"
    if tsv.exists() and not a.force:
        print(f"  = {tsv.name} already exists")
    else:
        try:
            print(f"  + {tsv.name}: {build_catalog(cfg.install_dir, tsv)} attributes")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ! {tsv.name}: {e}")

    for stem in PDFS:
        txt = out / f"{stem}.txt"
        pdf = cfg.install_dir / "Help" / f"{stem}.pdf"
        if txt.exists() and not a.force:
            print(f"  = {txt.name} already exists")
            continue
        if not pdf.exists():
            print(f"  - {pdf.name} is not in {pdf.parent}; skipped")
            continue
        try:
            print(f"  + {txt.name}: {extract_pdf(pdf, txt)} pages")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ! {txt.name}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
