"""Read the OSM objects needed for P0 from an OsmSchema-V0.6 snapshot."""

import struct
import zlib
from collections import Counter
from itertools import accumulate


def _varints(data):
    value = shift = 0
    for byte in data:
        value |= (byte & 127) << shift
        if byte < 128:
            yield value
            value = shift = 0
        else:
            shift += 7
            if shift >= 70:
                raise ValueError("Oversized PBF varint")
    if shift:
        raise ValueError("Truncated PBF varint")


def _varint(data, cursor):
    value = shift = 0
    while cursor < len(data) and shift < 70:
        byte = data[cursor]
        cursor += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, cursor
        shift += 7
    raise ValueError("Invalid PBF varint")


def _fields(data):
    cursor = 0
    while cursor < len(data):
        tag, cursor = _varint(data, cursor)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, cursor = _varint(data, cursor)
        elif wire == 2:
            size, cursor = _varint(data, cursor)
            end = cursor + size
            if end > len(data):
                raise ValueError("Truncated PBF field")
            value = data[cursor:end]
            cursor = end
        else:
            raise ValueError(f"Unsupported PBF wire type: {wire}")
        yield number, value


def _signed(number):
    return (number >> 1) ^ -(number & 1)


def _deltas(data):
    return accumulate(map(_signed, _varints(data)))


def _read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("Truncated PBF file")
    return data


def _blobs(path):
    with path.open("rb") as source:
        while True:
            prefix = source.read(4)
            if not prefix:
                break
            if len(prefix) != 4:
                raise ValueError("Truncated PBF header size")
            size = struct.unpack(">I", prefix)[0]
            if not 0 < size <= 64 * 1024:
                raise ValueError("Invalid PBF header size")
            header = dict(_fields(_read_exact(source, size)))
            blob_size = header[3]
            if not 0 < blob_size <= 32 * 1024 * 1024:
                raise ValueError("Invalid PBF blob size")
            blob = dict(_fields(_read_exact(source, blob_size)))
            if 1 in blob:
                content = blob[1]
            elif 3 in blob:
                content = zlib.decompress(blob[3])
                if len(content) != blob[2]:
                    raise ValueError("Invalid PBF uncompressed blob size")
            else:
                raise ValueError("Expected raw or zlib-compressed PBF blob")
            yield header[1], content


def _header_audit(content):
    fields = list(_fields(content))
    required = [value.decode() for key, value in fields if key == 4]
    unknown = set(required) - {"OsmSchema-V0.6", "DenseNodes"}
    if "OsmSchema-V0.6" not in required or unknown:
        raise ValueError(f"Unsupported PBF required features: {required}")
    result = {"file_format": "OSM PBF", "required_features": required}
    for key, value in fields:
        if key == 1:
            bounds = dict(_fields(value))
            result["extract_bbox_wgs84"] = [_signed(bounds[k]) / 1e9 for k in (1, 4, 2, 3)]
        elif key in (16, 17, 34):
            result[{16: "writing_program", 17: "source", 34: "replication_base_url"}[key]] = value.decode()
        elif key in (32, 33):
            result[{32: "replication_epoch_s", 33: "replication_sequence"}[key]] = value
    return result


def inspect_header(path):
    blobs = _blobs(path)
    try:
        kind, content = next(blobs, (None, None))
    finally:
        blobs.close()
    if kind != b"OSMHeader":
        raise ValueError("Missing OSMHeader at start of PBF")
    return _header_audit(content)


def read_snapshot(path, bbox):
    """Keep node IDs in the candidate bbox and original node references on intersecting ways."""
    west, south, east, north = bbox
    blobs = _blobs(path)
    kind, content = next(blobs, (None, None))
    if kind != b"OSMHeader":
        raise ValueError("Missing OSMHeader at start of PBF")
    metadata = _header_audit(content)
    nodes, ways = {}, []
    counts = Counter()
    encountered_way = False
    for kind, content in blobs:
        if kind != b"OSMData":
            raise ValueError(f"Unexpected PBF record type: {kind}")
        block = list(_fields(content))
        scalars = {key: value for key, value in block if key >= 17}
        granularity = scalars.get(17, 100)
        lat_offset = scalars.get(19, 0)
        lon_offset = scalars.get(20, 0)
        if lat_offset >= 2 ** 63:
            lat_offset -= 2 ** 64
        if lon_offset >= 2 ** 63:
            lon_offset -= 2 ** 64
        min_y, max_y = (south * 1e9 - lat_offset) / granularity, (north * 1e9 - lat_offset) / granularity
        min_x, max_x = (west * 1e9 - lon_offset) / granularity, (east * 1e9 - lon_offset) / granularity
        strings = [value for _, value in _fields(next(value for key, value in block if key == 1))]
        highway_key = strings.index(b"highway") if b"highway" in strings else None
        for _, group in (field for field in block if field[0] == 2):
            for key, record in _fields(group):
                if key == 2:
                    if encountered_way:
                        raise ValueError("P0 requires nodes to precede ways in the PBF")
                    dense = dict(_fields(record))
                    ids = list(_deltas(dense[1]))
                    ys, xs = list(_deltas(dense[8])), list(_deltas(dense[9]))
                    if len(ids) != len(ys) or len(ids) != len(xs):
                        raise ValueError("Mismatched DenseNodes field lengths")
                    counts["nodes_raw"] += len(ids)
                    for node, x, y in zip(ids, xs, ys):
                        if min_x <= x <= max_x and min_y <= y <= max_y:
                            nodes[node] = ((x * granularity + lon_offset) / 1e9,
                                           (y * granularity + lat_offset) / 1e9)
                elif key == 1:
                    if encountered_way:
                        raise ValueError("P0 requires nodes to precede ways in the PBF")
                    node = dict(_fields(record))
                    counts["nodes_raw"] += 1
                    x, y = _signed(node[9]), _signed(node[8])
                    if min_x <= x <= max_x and min_y <= y <= max_y:
                        nodes[_signed(node[1])] = ((x * granularity + lon_offset) / 1e9,
                                                   (y * granularity + lat_offset) / 1e9)
                elif key == 3:
                    encountered_way = True
                    counts["ways_raw"] += 1
                    if highway_key is None:
                        continue
                    way = dict(_fields(record))
                    keys = list(_varints(way.get(2, b"")))
                    if highway_key not in keys:
                        continue
                    refs = list(_deltas(way[8]))
                    if any(ref in nodes for ref in refs):
                        vals = _varints(way[3])
                        tags = {strings[k].decode(): strings[v].decode() for k, v in zip(keys, vals)}
                        ways.append((way[1], refs, tags))
                elif key == 4:
                    counts["relations_raw"] += 1
    return nodes, ways, {**metadata, **counts, "candidate_bbox_nodes": len(nodes)}
