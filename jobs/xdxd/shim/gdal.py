"""`import gdal` (GDAL-2 era) shim: only gdal.Open(...).RasterXSize/RasterYSize/GetGeoTransform are used."""
import rasterio


class _DS:
    def __init__(self, path):
        with rasterio.open(path) as r:
            self.RasterXSize, self.RasterYSize = r.width, r.height
            self._gt = r.transform.to_gdal()

    def GetGeoTransform(self):
        return self._gt


def Open(path):
    return _DS(path)
