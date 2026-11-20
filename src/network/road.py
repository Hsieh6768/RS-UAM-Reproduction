from collections import Counter
import json
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from src.common.geo import chord_distance_m, distance_m, sphere_xyz


def read_road_graph(path, *, parse_float=float):
    """Read the P0 directed multigraph, preserving every serialized edge."""
    with Path(path).open(encoding="utf-8-sig") as stream:
        network = json.load(stream, parse_float=parse_float)
    graph = nx.MultiDiGraph()
    graph.add_nodes_from((node, {"position": (x, y)}) for node, x, y in network["nodes"])
    graph.add_edges_from(
        (a, b, {"way_id": way, "length_m": length, "time_s": time})
        for a, b, way, length, time in network["directed_edges"]
    )
    return graph


# P0 candidate profile follows the local OSMnx 2.0.7 drive filter.
EXCLUDED_HIGHWAYS = set(
    "abandoned bridleway bus_guideway construction corridor cycleway elevator escalator footway "
    "no path pedestrian planned platform proposed raceway razed rest_area service services steps track".split()
)


def _is_drive_way(tags):
    return (
        tags.get("highway") not in EXCLUDED_HIGHWAYS
        and tags.get("area") != "yes"
        and tags.get("access") != "private"
        and tags.get("motor_vehicle") != "no"
        and tags.get("motorcar") != "no"
        and tags.get("service") not in {"alley", "driveway", "emergency_access", "parking", "parking_aisle", "private"}
    )


def _direction(tags):
    value = tags.get("oneway")
    if value in {"yes", "true", "1"}:
        return "forward"
    if value in {"-1", "reverse"}:
        return "reverse"
    if value in {"no", "false", "0"}:
        return "both"
    if value is not None:
        return "unsupported"
    if tags.get("junction") in {"roundabout", "circular"} or tags.get("highway") == "motorway":
        return "forward"
    return "both"


def build_graph(nodes, ways, speed_mps):
    graph = nx.MultiDiGraph()
    directional_ways = Counter()
    tag_issues = Counter()
    segments = []
    for way_id, refs, tags in ways:
        if not _is_drive_way(tags):
            continue
        pairs = [(a, b) for a, b in zip(refs, refs[1:]) if a != b and a in nodes and b in nodes]
        if not pairs:
            continue
        direction = _direction(tags)
        directional_ways[direction] += 1
        for tag in ("oneway:conditional", "access:conditional", "motor_vehicle:conditional", "length"):
            if tag in tags:
                tag_issues[tag] += 1
        if direction == "unsupported":
            tag_issues["unsupported_oneway_" + tags["oneway"]] += 1
            continue
        for a, b in pairs:
            segments.append((a, b, way_id, direction))

    lengths = distance_m([nodes[a] for a, _, _, _ in segments], [nodes[b] for _, b, _, _ in segments]) if segments else []
    zero_length = 0
    for (a, b, way_id, direction), length in zip(segments, lengths):
        if length == 0:
            zero_length += 1
        for node in (a, b):
            graph.add_node(node, position=nodes[node])
        attributes = {"way_id": way_id, "length_m": float(length), "time_s": float(length / speed_mps)}
        for origin, destination, allowed in (
            (a, b, direction != "reverse"), (b, a, direction != "forward")
        ):
            if allowed:
                key = way_id if not graph.has_edge(origin, destination, way_id) else (way_id, graph.number_of_edges())
                graph.add_edge(origin, destination, key=key, **attributes)
    graph.graph.update(
        candidate_way_directions=dict(directional_ways),
        tag_audit=dict(tag_issues),
        zero_length_undirected_segments=zero_length,
    )
    return graph


def summarize_graph(graph):
    components = sorted(nx.strongly_connected_components(graph), key=lambda nodes: (-len(nodes), min(nodes)))
    if not components:
        raise ValueError("Candidate directed road graph is empty")
    largest = components[0]
    weak_sizes = sorted((len(c) for c in nx.weakly_connected_components(graph)), reverse=True)
    strong_sizes = [len(c) for c in components]
    lengths = np.array([edge["length_m"] for _, _, edge in graph.edges(data=True)])
    largest_edges = graph.subgraph(largest).number_of_edges()
    return {
        **graph.graph,
        "directed": graph.is_directed(),
        "nodes": len(graph),
        "edges": graph.number_of_edges(),
        "weak_components": len(weak_sizes),
        "strong_components": len(components),
        "weak_component_size_histogram": dict(sorted(Counter(weak_sizes).items())),
        "strong_component_size_histogram": dict(sorted(Counter(strong_sizes).items())),
        "largest_weak_nodes": weak_sizes[0],
        "largest_strong_nodes": len(largest),
        "largest_strong_edges": largest_edges,
        "largest_strong_node_fraction": len(largest) / len(graph),
        "largest_strong_edge_fraction": largest_edges / graph.number_of_edges(),
        "edge_length_m": distribution(lengths),
    }, components, largest


def distribution(values):
    if len(values) == 0:
        return {}
    return {
        "mean": float(np.mean(values)), "min": float(np.min(values)),
        "median": float(np.median(values)), "p95": float(np.quantile(values, 0.95)),
        "p99": float(np.quantile(values, 0.99)), "max": float(np.max(values)),
    }


class RoadNodeIndex:
    def __init__(self, graph, nodes):
        self.ids = np.array(sorted(nodes), dtype=np.int64)
        self.positions = np.array([graph.nodes[node]["position"] for node in self.ids])
        self.tree = cKDTree(sphere_xyz(self.positions))

    def nearest(self, positions):
        coords = sphere_xyz(positions)
        if len(self.ids) == 1:
            chord, index = self.tree.query(coords)
        else:
            candidates, indices = self.tree.query(coords, k=2)
            chord, index = candidates[:, 0], indices[:, 0]
            for row in np.flatnonzero(candidates[:, 0] == candidates[:, 1]):
                tied = self.tree.query_ball_point(coords[row], np.nextafter(chord[row], np.inf))
                index[row] = min(tied)
        return self.ids[index], chord_distance_m(chord), self.positions[index]


def map_requests(graph, components, largest, origins, destinations):
    full = RoadNodeIndex(graph, graph)
    crop = RoadNodeIndex(graph, largest)
    condensation = nx.condensation(graph, components)
    membership = condensation.graph["mapping"]
    origin_node, origin_error, mapped_origin = full.nearest(origins)
    destination_node, destination_error, mapped_destination = full.nearest(destinations)
    crop_o, crop_o_error, crop_origin = crop.nearest(origins)
    crop_d, crop_d_error, crop_destination = crop.nearest(destinations)
    source_components = np.array([membership[node] for node in origin_node])
    target_components = np.array([membership[node] for node in destination_node])
    reachable = source_components == target_components
    cache = {}
    for index in np.flatnonzero(~reachable):
        component = source_components[index]
        if component not in cache:
            cache[component] = nx.descendants(condensation, component)
        reachable[index] = target_components[index] in cache[component]
    moved = (origin_node != crop_o) | (destination_node != crop_d)
    mapped = pd.DataFrame({
        "origin_node": origin_node, "destination_node": destination_node,
        "origin_snap_m": origin_error, "destination_snap_m": destination_error,
        "mapped_direct_od_m": distance_m(mapped_origin, mapped_destination),
        "road_reachable": reachable,
        "largest_origin_node": crop_o, "largest_destination_node": crop_d,
        "largest_origin_snap_m": crop_o_error, "largest_destination_snap_m": crop_d_error,
        "largest_mapped_direct_od_m": distance_m(crop_origin, crop_destination),
    })
    full_error = np.maximum(origin_error, destination_error)
    crop_error = np.maximum(crop_o_error, crop_d_error)
    return mapped, {
        "compared_requests": len(mapped),
        "both_full_snaps_in_largest": int((~moved).sum()),
        "either_endpoint_changed": int(moved.sum()),
        "either_endpoint_changed_by_more_than_10m_error": int((crop_error - full_error > 10).sum()),
        "ordered_od_unreachable_on_full": int((~reachable).sum()),
        "full_mapped_same_node": int((origin_node == destination_node).sum()),
        "largest_mapped_same_node": int((crop_o == crop_d).sum()),
        "full_snap_error_m": distribution(np.concatenate([origin_error, destination_error])),
        "largest_snap_error_m": distribution(np.concatenate([crop_o_error, crop_d_error])),
        "full_mapped_od_m": distribution(mapped["mapped_direct_od_m"]),
        "largest_mapped_od_m": distribution(mapped["largest_mapped_direct_od_m"]),
        "full_snap_over_1km_requests": int((full_error > 1000).sum()),
        "largest_snap_over_1km_requests": int((crop_error > 1000).sum()),
        "max_largest_snap_m": float(crop_error.max()) if len(crop_error) else 0,
    }
