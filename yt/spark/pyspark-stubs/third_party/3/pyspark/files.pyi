# Stubs for pyspark.files (Python 3.5)
#

class SparkFiles:
    def __init__(self) -> None: ...
    @classmethod
    def get(cls, filename: str) -> str: ...
    @classmethod
    def getRootDirectory(cls) -> str: ...