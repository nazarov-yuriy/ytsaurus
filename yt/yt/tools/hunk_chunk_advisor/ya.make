PY3_PROGRAM()

PEERDIR(
    yt/python/yt/wrapper
    yt/yt/python/yt_yson_bindings
    yt/yt/python/yt_driver_bindings
    contrib/python/matplotlib
)

PY_SRCS(
    __main__.py
)

END()
