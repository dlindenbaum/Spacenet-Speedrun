"""Assemble the public Hugging Face Space for the story map (static SDK) from story/index.html and the assets made
by scripts/build_story_assets.py, then optionally upload it.

Differences from the Claude-artifact build: a full HTML document (the artifact host adds the skeleton), raw binary
vector chunks (the artifact host serves only web types, so it gets base64-gzip .txt), and a self-hosted copy of
deck.gl instead of the unpkg CDN, so the Space depends on nothing outside Hugging Face.

  python scripts/build_space.py data/story_assets out/space [--upload Rootkit29/spacenet-speedrun]
"""
import base64
import gzip
import json
import shutil
import sys
import urllib.request
from pathlib import Path

DECK = "https://unpkg.com/deck.gl@9.1.12/dist.min.js"
CARD = """---
title: SpaceNet 2 Speedrun
emoji: 🛰️
colorFrom: gray
colorTo: green
sdk: static
app_file: index.html
pinned: false
license: cc-by-sa-4.0
short_description: SpaceNet 2 revisited; 157k Vegas buildings mapped
---

# SpaceNet 2 Speedrun: story map

A scroll-driven story of revisiting the 2017 SpaceNet 2 building-footprint challenge with 2026 tools. It's set on a
WebGL map (deck.gl) of the full Las Vegas WorldView-3 strip (2015-10-22).

* **Imagery:** a pyramid built from the public SpaceNet strip. The overview is 4.8 m, the tiles are 1.2 m
  (`img/l4/`), and two insets are at the full 0.3 m (`img/full/`).
* **Vector layers:** the model's 157,339 footprints, SpaceNet's labels, Overture Maps buildings (2026-11-18) and the
  SpaceNet chip grid. Each layer is cut into 4 × 4 spatial chunks of quantised binary (`vec/`), described in
  `manifest.json`.
* **Streaming:** the page fetches only the tiles and chunks in view.

Data and code:
* Footprints as GeoParquet 1.1:
  [Rootkit29/vegas-wv3-building-footprints](https://huggingface.co/datasets/Rootkit29/vegas-wv3-building-footprints)
* Code, results and full write-up: [github.com/dlindenbaum/Spacenet-Speedrun](https://github.com/dlindenbaum/Spacenet-Speedrun)

Imagery and labels: SpaceNet (Maxar, CosmiQ Works), CC BY-SA 4.0.
"""


def main():
    assets, out = Path(sys.argv[1]), Path(sys.argv[2])
    shutil.rmtree(out, ignore_errors=True)
    (out / "vec").mkdir(parents=True)
    (out / "vendor").mkdir()
    shutil.copytree(assets / "img", out / "img")
    shutil.copy(assets / "thumb.jpg", out / "thumb.jpg")
    m = json.loads((assets / "manifest.json").read_text())
    for layer in m["layers"].values():
        for c in layer["chunks"]:
            src = assets / c["f"]
            raw = gzip.decompress(base64.b64decode(src.read_text().strip())) if src.suffix == ".txt" else src.read_bytes()
            c["f"] = c["f"].rsplit(".", 1)[0] + ".bin"
            (out / c["f"]).write_bytes(raw)
            c["bytes"] = len(raw)
    m["vector_encoding"] = "raw binary chunk"
    (out / "manifest.json").write_text(json.dumps(m))
    (out / "vendor/deck.gl-9.1.12.min.js").write_bytes(urllib.request.urlopen(DECK, timeout=120).read())

    body = Path("story/index.html").read_text().replace('<link rel="artifact-thumbnail" href="thumb.jpg">\n', "")
    body = body.replace(DECK, "vendor/deck.gl-9.1.12.min.js")
    i = body.index('<div class="app">')
    head_extra = (
        '<meta property="og:title" content="SpaceNet 2 Speedrun">\n'
        '<meta property="og:description" content="Nine years after SpaceNet 2: a one-hour model beats the 2017 winner '
        'and maps 157,339 buildings across the Las Vegas WorldView-3 strip.">\n'
        '<meta property="og:image" content="thumb.jpg">\n'
        "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
        " body{margin:0} img{max-width:100%} [hidden]{display:none!important}</style>\n")
    html = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            + body[:i] + head_extra + "</head>\n<body>\n" + body[i:] + "\n</body>\n</html>\n")
    (out / "index.html").write_text(html)
    (out / "README.md").write_text(CARD)
    print("built", sum(1 for f in out.rglob("*") if f.is_file()), "files")
    if "--upload" in sys.argv:
        from huggingface_hub import HfApi

        repo = sys.argv[sys.argv.index("--upload") + 1]
        api = HfApi()
        api.create_repo(repo, repo_type="space", space_sdk="static", private=False, exist_ok=True)
        print(api.upload_folder(repo_id=repo, repo_type="space", folder_path=str(out), commit_message="Rebuild story map"))


if __name__ == "__main__":
    main()
