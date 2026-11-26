#!/bin/bash
# HF Job (cpu-performance, image python:3.11-bookworm): OSM planet 2017-05-01 -> XD_XD's imposm-style shapefiles.
set -euo pipefail
OUT=/out/xdxd/osmdata
mkdir -p $OUT /work && cd /work
apt-get update -qq && apt-get install -y -qq --no-install-recommends osmium-tool lbzip2 curl >/dev/null
pip install -q fiona "shapely>=2"
URL=https://osm-planet-us-west-2.s3.dualstack.us-west-2.amazonaws.com/planet/osm/2017/planet-170501.osm.bz2
echo "$(date) download"; curl -s -o planet.osm.bz2 "$URL"; ls -la planet.osm.bz2
curl -s "$URL.md5"; md5sum planet.osm.bz2 &
python /sn2/xdxd/osm_2017.py config > extracts.json
echo "$(date) extract"
lbzip2 -d -c -n 28 planet.osm.bz2 | osmium extract --config extracts.json --strategy simple -F osm -O -
wait
for f in *.osm.pbf; do
  nm=${f%.osm.pbf}
  osmium export "$f" -f geojsonseq -a type,id -O -o "$nm.geojsonseq"
  python /sn2/xdxd/osm_2017.py "$nm" "$nm.geojsonseq" $OUT
  cp "$f" $OUT/
done
touch $OUT/DONE; echo "$(date) done"
