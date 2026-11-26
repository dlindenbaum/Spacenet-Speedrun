import json, sys, cv2, numpy as np
from shapely import wkt
ds, k, out = sys.argv[1], sys.argv[2], sys.argv[3]
im = cv2.imread(f"data/xds/{ds}/images/{k}.png")
for s in json.load(open(f"data/xds/{ds}/labels.json"))[k]:
    p = wkt.loads(s)
    cv2.polylines(im, [np.asarray(p.exterior.coords)[:, :2].round().astype(np.int32)], True, (0, 230, 255), 2)
cv2.imwrite(out, cv2.resize(im, (600, int(600 * im.shape[0] / im.shape[1]))))
