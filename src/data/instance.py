"""Resolve a network through the selected request manifest before opening any router."""
import json

from src.common.config import load_config
from src.data.scc100 import CONFIGS, DIRECTORY
from src.data.sources import fingerprint


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def check_file(path, ref, verify=True):
    if path.stat().st_size != ref["size_bytes"] or verify and fingerprint(path)["sha256"] != ref["sha256"]:
        raise ValueError(f"Incorrect P0 fingerprint for {path}")


def network_references(config, root, verify):
    inputs, settings = config["inputs"], config["network"]
    variant = inputs.get("dataset_variant", "full")
    if variant not in ("full", "scc100"):
        raise ValueError(f"Unknown dataset variant {variant}")
    selected = variant == "scc100"
    policy = "largest_scc" if selected else "full_directed_reachable_od"
    request_path = root / inputs["prepared_request_manifest_path"]
    roads = root / inputs["prepared_road_graph_path"]
    manifest = read_json(request_path)
    ref = manifest["official_road_network"]
    if (settings["directed_roads"] is not True or ref["directed"] is not True
            or ref["policy"] != policy or settings["road_graph_component_policy"] != policy
            or manifest.get("dataset_variant", "full") != variant or manifest["status"] != "OFFICIAL_P0_INPUTS"
            or roads.resolve() != (root / ref["path"]).resolve()):
        raise ValueError("Configured dataset and request/network manifest disagree")
    if selected and (request_path.resolve() != root.resolve() / DIRECTORY / "requests/manifest.json"
                     or roads.resolve() != root.resolve() / DIRECTORY / "network/roads.json"):
        raise ValueError("SCC100 requires its own network and request directories")
    check_file(roads, ref, verify)
    path = roads.with_name("manifest.json")
    check_file(path, manifest["network_manifest"], verify)
    net = read_json(path)
    if net.get("dataset_variant", "full") != variant:
        raise ValueError("Network manifest belongs to another dataset variant")
    artifacts = net["artifacts"]
    for key in ("sha256", "size_bytes"):
        if artifacts["roads.json"][key] != ref[key]:
            raise ValueError("Road identity differs between the paired manifests")
    for name in ("road_audit.json", "air_audit.json"):
        check_file(path.with_name(name), artifacts[name], verify)
    if selected:
        recipe = load_config(CONFIGS / "p0_scc100.json")
        sources = root / recipe["source_manifest"]
        check_file(sources, manifest["source_manifest"])
        entries = read_json(sources)["sources"]
        expected_sources = {k: v["sha256"] for k, v in entries.items()}
        raw_fields = {"tlc": "raw_tlc_path", "osm": "raw_osm_path",
                      "air_routes": "air_routes_path", "vertiports": "vertiports_path"}
        if any((root / inputs[field]).resolve() != (root / entries[k]["archive_path"]).resolve()
               for k, field in raw_fields.items()):
            raise ValueError("Configured raw paths disagree with the SCC100 source manifest")
        expected = dict(schema_version=2, dataset_variant=variant, road_policy=policy,
                        snap_max_distance_m=100, sample_rate=.05, sample_seed=20130505,
                        p0_config=recipe, base_config=fingerprint(CONFIGS / "p0.json"),
                        source_sha256=expected_sources, source_manifest=manifest["source_manifest"])
        for doc in (net, manifest):
            if any(doc.get(k) != v for k, v in expected.items()):
                raise ValueError("SCC100 source/configuration identity mismatch")
            check_file(CONFIGS / "p0_scc100.json", doc["config"])
        audit = read_json(path.with_name("road_audit.json"))
        if (net["status"] != "SCC100_NETWORK" or audit["component_policy"] != policy
                or audit["strong_components"] != 1 or audit["nodes"] != ref["nodes"]
                or audit["edges"] != ref["edges"] or
                (config["data"]["snap_max_distance_m"], config["data"]["sample_rate"], config["data"]["seed"])
                != (100, .05, 20130505)):
            raise ValueError("Invalid SCC100 graph, demand policy or audit counts")
    return manifest, net, roads

