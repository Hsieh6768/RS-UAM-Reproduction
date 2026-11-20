import struct
import zlib

import pytest

from src.network.pbf import inspect_header, read_snapshot


def _varint(value):
    encoded = bytearray()
    while value > 127:
        encoded.append(128 | (value & 127))
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _uint(field, value):
    return _varint(field * 8) + _varint(value)


def _bytes(field, value):
    return _varint(field * 8 + 2) + _varint(len(value)) + value


def _zigzag(values):
    return b"".join(_varint(2 * value if value >= 0 else -2 * value - 1) for value in values)


def _blob(kind, body):
    blob = _uint(2, len(body)) + _bytes(3, zlib.compress(body))
    header = _bytes(1, kind) + _uint(3, len(blob))
    return struct.pack(">I", len(header)) + header + blob


def _small_snapshot():
    header = _bytes(4, b"OsmSchema-V0.6") + _bytes(4, b"DenseNodes")
    dense = (
        _bytes(1, _zigzag([100, 10, 15]))
        + _bytes(8, _zigzag([407000000, 0, -100000]))
        + _bytes(9, _zigzag([-740000000, 100000, 100000]))
    )
    way = (
        _uint(1, 22) + _bytes(2, bytes([1, 3])) + _bytes(3, bytes([2, 4]))
        + _bytes(8, _zigzag([125, -15, -10]))
    )
    strings = b"".join(_bytes(1, text) for text in (b"", b"highway", b"primary", b"oneway", b"-1"))
    block = _bytes(1, strings) + _bytes(2, _bytes(2, dense)) + _bytes(2, _bytes(3, way))
    return _blob(b"OSMHeader", header) + _blob(b"OSMData", block)


class _InMemoryPath:
    def __init__(self, content):
        self.content = content

    def open(self, mode):
        from io import BytesIO
        assert mode == "rb"
        return BytesIO(self.content)


def test_pbf_snapshot_decodes_real_node_ids_and_reversed_way_reference_deltas():
    path = _InMemoryPath(_small_snapshot())
    assert inspect_header(path)["required_features"] == ["OsmSchema-V0.6", "DenseNodes"]
    nodes, ways, evidence = read_snapshot(path, (-74.001, 40.695, -73.98, 40.705))
    assert nodes == {100: (-74, 40.7), 110: (-73.99, 40.7)}
    assert ways == [(22, [125, 110, 100], {"highway": "primary", "oneway": "-1"})]
    assert evidence["nodes_raw"] == 3 and evidence["candidate_bbox_nodes"] == 2


def test_truncated_pbf_raises_instead_of_returning_partial_topology():
    with pytest.raises(ValueError, match="Truncated PBF"):
        read_snapshot(_InMemoryPath(_small_snapshot()[:-1]), (-75, 40, -73, 41))
