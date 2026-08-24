"""
build_web.py — produce both front-end builds from one template.

  data/woodson_app.template.html  (has a __SNAPSHOT_SRC__ placeholder)
    + data/snapshot.json
    -> data/woodson_intelligence.html   (ARTIFACT build: snapshot embedded inline)
    -> web/index.html + web/snapshot.json  (VERCEL build: app fetches snapshot.json)

Run export_snapshot.py first so data/snapshot.json is current, then this.
Artifact: re-publish data/woodson_intelligence.html (same path -> same URL).
Vercel:   the web/ folder is the deploy root; the refresh workflow commits web/snapshot.json.
"""

import pathlib
import shutil

ROOT = pathlib.Path(__file__).parent
TPL = ROOT / "data" / "woodson_app.template.html"
SNAP = ROOT / "data" / "snapshot.json"
ARTIFACT = ROOT / "data" / "woodson_intelligence.html"
WEB = ROOT / "web"


def main():
    tpl = TPL.read_text()
    snap = SNAP.read_text()
    assert "__SNAPSHOT_SRC__" in tpl, "template missing __SNAPSHOT_SRC__"
    assert "</script>" not in snap, "snapshot would break the <script> tag"

    # 1) artifact build — data embedded inline
    ARTIFACT.write_text(tpl.replace("__SNAPSHOT_SRC__", snap))

    # 2) vercel build — app fetches snapshot.json at runtime
    WEB.mkdir(exist_ok=True)
    (WEB / "index.html").write_text(tpl.replace("__SNAPSHOT_SRC__", '"fetch"'))
    shutil.copy(SNAP, WEB / "snapshot.json")

    print(f"artifact -> {ARTIFACT.relative_to(ROOT)} ({ARTIFACT.stat().st_size/1e6:.2f} MB)")
    print(f"vercel   -> web/index.html ({(WEB/'index.html').stat().st_size/1e3:.0f} KB) + "
          f"web/snapshot.json ({(WEB/'snapshot.json').stat().st_size/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
