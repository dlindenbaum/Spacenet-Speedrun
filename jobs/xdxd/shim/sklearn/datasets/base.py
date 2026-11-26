class Bunch(dict):
    def __init__(self, **kw):
        super().__init__(kw)
        self.__dict__ = self
