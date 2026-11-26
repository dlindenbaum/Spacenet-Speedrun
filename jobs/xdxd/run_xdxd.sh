#!/bin/bash
# Run XD_XD's winning SpaceNet-2 solution (github.com/SpaceNetChallenge/BuildingDetectors_Round2, 1-XD_XD),
# code UNMODIFIED, for one city: train on our `train` tiles, predict our `val` tiles.
# Only the environment differs from 2017: Keras-1.1/Theano -> PyTorch shim (shim/keras), shapely-1.5
# empty-polygon behaviour (shim/sitecustomize.py), `import gdal` -> rasterio (shim/gdal.py),
# Mapzen OSM extracts -> 2017-05-15 OSM rebuilt from Overpass (osm_2017.py).
# Usage (HF Job, image pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime):
#   bash /sn2/xdxd/run_xdxd.sh AOI_3_Paris
set -euo pipefail
AOI=$1
OUT=/out/xdxd/$AOI
mkdir -p $OUT
exec > >(tee -a $OUT/run.log) 2>&1
echo "== $(date) start $AOI"

apt-get update -qq && apt-get install -y -qq --no-install-recommends openjdk-17-jre-headless unzip git >/dev/null
pip install -q -r /sn2/xdxd/requirements.txt && pip install -q --no-deps click==6.7  # 2017 click: underscore command names

git clone -q https://github.com/SpaceNetChallenge/BuildingDetectors_Round2 /tmp/bd2
echo "XD_XD code commit: $(git -C /tmp/bd2 rev-parse HEAD)"
cp -r /tmp/bd2/1-XD_XD/code/. /root/
mkdir -p /root/visualizer-2.0 && unzip -q -o /root/visualizer-2.0.zip -d /root/visualizer-2.0
printf '#!/bin/bash\n' > /usr/local/bin/activate && chmod +x /usr/local/bin/activate  # `source activate py35`
export PYTHONPATH=/sn2/xdxd/shim

python /sn2/xdxd/stage_data.py --sn2 /sn2 --root /data --aois $AOI
save() {  # results + logs (not the big HDF5 caches) to the bucket
  mkdir -p $OUT/working
  (cd /data/working && find . -name "*.csv" | while read f; do mkdir -p $OUT/working/$(dirname $f); cp $f $OUT/working/$f; done) || true
  cp /root/.v*.log $OUT/working/ 2>/dev/null || true
}
run() { echo "== $(date) $*"; python "$@"; save; }

# == train.sh (same commands, same order; v16's preproc is moved after v13 so the GPU work
#    can start while the 2017 OSM layers are still being built)
cd /root
T=/data/train/${AOI}_Train
rm -rf /data/working && mkdir -p /data/working
run v5_im.py preproc_train $T
run v12_im.py preproc_train $T
run v9s.py validate $T
run v9s.py evalfscore $T
run v13.py validate $T
run v13.py evalfscore $T
until [ -f /out/xdxd/osmdata/DONE ]; do echo "waiting for OSM 2017 layers"; sleep 120; done
mkdir -p /root/osmdata && cp -r /out/xdxd/osmdata/. /root/osmdata/
run v16.py preproc_train $T
run v16.py validate $T
run v16.py evalfscore $T
run v17.py evalfscore $T
# == test.sh (per city; merge.py only concatenates the 4 cities' CSVs, done offline)
E=/data/test/${AOI}_Test
run v5_im.py preproc_test $E
run v12_im.py preproc_test $E
run v16.py preproc_test $E
run v17.py testproc $E
cp /data/working/models/v17/${AOI}_poly.csv $OUT/test_poly.csv
mkdir -p $OUT/models && cp -r /data/working/models/v17 $OUT/models/ && save
echo "== $(date) done $AOI"
