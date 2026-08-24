"""
refresh.py — regenerate the published snapshot + web bundle (the auto-refresh entrypoint).

Phase 2a (now): re-exports from the committed data/woodson_enriched.xlsx and rebuilds the
web bundle. The GitHub Action commits web/snapshot.json + web/index.html and Vercel redeploys
to the same URL. With unchanged source data this is idempotent (no commit).

Phase 2b (TODO): before export, run the INCREMENTAL EDGAR pipeline to update
data/woodson_enriched.xlsx from filings since the last high-water mark (state.json), then
re-score only the affected companies. That is the heavy remaining build; wire it in below.
"""
import subprocess
import sys


def run(*args):
    subprocess.run([sys.executable, *args], check=True)


if __name__ == "__main__":
    run("pipeline_incremental.py")     # update data/woodson_enriched.xlsx from new filings (daily index)
    run("export_snapshot.py")          # enriched xlsx -> data/snapshot.json
    run("build_web.py")                # -> web/index.html + web/snapshot.json (+ artifact build)
    print("refresh complete")
