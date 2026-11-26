"""Pack the per-tile PNGs into one uint8 .npy array per AOI (+ id list) so HF
Jobs can read the training set with a few large sequential reads from the
mounted dataset instead of ~10k small-file reads."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/sn2")
(root / "shards").mkdir(exist_ok=True)
for aoi_dir in sorted((root / "images").iterdir()):
    files = sorted(aoi_dir.glob("*.png"))
    ids = [f.stem for f in files]
    arr = np.lib.format.open_memmap(root / "shards" / f"{aoi_dir.name}.npy", mode="w+", dtype=np.uint8,
                                    shape=(len(files), 650, 650, 3))

    def load(k):
        arr[k] = np.asarray(Image.open(files[k]).convert("RGB"))

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(load, range(len(files))))
    arr.flush()
    (root / "shards" / f"{aoi_dir.name}.json").write_text(json.dumps(ids))
    print(aoi_dir.name, arr.shape)
