# /// script
# dependencies = ["numpy", "pandas", "rasterio", "shapely>=2", "opencv-python-headless", "tqdm"]
# ///
"""HF Job (CPU): a full SpaceNet-4 (off-nadir, Atlanta) training/test set at the native 0.5 m GSD, for a dedicated
SN4 model comparable to the challenge entries.

Same location-held-out split as scripts/prepare_xds.py (the 40 test locations are identical, so 0 overlap with
training ground), but:
  * all 27 collects (look angles 7-54 deg) for both train and test, instead of 9 collects for train;
  * --n_train training locations (default 540 of the ~950 available) instead of 300;
  * native 0.5 m 900 px tiles (SN4's own resolution) instead of resampling to 0.3 m.
Output: <out>/sn4n/{images/*.png, labels.json, meta.json, splits.json}, read by train_seg/transfer_eval --xds_root.
"""
import argparse
import sys

sys.path.insert(0, "/sn2/code")
import prepare_xds as P  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/out/xds")
    ap.add_argument("--n_train", type=int, default=540)
    ap.add_argument("--workers", type=int, default=48)
    a = ap.parse_args()
    P.TARGET_GSD = 0.5  # keep SN4's native resolution
    P.MAX_PX = 1000  # one 900 px tile per location and collect
    orig = P.Writer.__init__

    def init(self, out, ds):
        orig(self, out, "sn4n")

    P.Writer.__init__ = init
    angles = {f"nadir{n}" for n in (7, 8, 10, 13, 14, 16, 19, 21, 23, 25, 27, 29, 30, 32, 34, 36, 39, 42, 44, 46, 47,
                                     49, 50, 52, 53)}
    P.do_sn4(a.out, 40, a.n_train, angles, a.workers)
    print("done")


if __name__ == "__main__":
    main()
