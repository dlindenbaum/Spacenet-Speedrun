"""Restore 2017 (shapely 1.5) behaviour XD_XD's code relies on: Polygon('POLYGON EMPTY').exterior is None."""
try:
    from shapely.geometry import polygon as _p

    _orig = _p.Polygon.exterior.fget

    def _exterior(self):
        return None if self.is_empty else _orig(self)

    _p.Polygon.exterior = property(_exterior)
except Exception:  # pragma: no cover
    pass
