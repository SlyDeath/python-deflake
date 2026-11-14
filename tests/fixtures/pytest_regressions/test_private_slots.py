class FeatureState:
    __slots__ = ("__enabled",)

    def __init__(self):
        self.__enabled = False

    def enable(self):
        self.__enabled = True


STATE = FeatureState()


def test_pollutes():
    STATE.enable()
