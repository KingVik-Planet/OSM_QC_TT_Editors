"""
Rule engine: turns a changeset's metadata + osmChange diff + nearby
existing geometry into a list of Issue objects, one per problem found.

Honesty note: this is a first, pragmatic version of each geometry check
-- not a full replacement for JOSM's validator or Osmose, which are
mature tools built over years. It's tuned to catch the categories you
asked for with a low false-positive rate, using thresholds in
config.py that can be adjusted as you see real-world results.

Structure note: checks that need Overpass context (comparing a new
object against pre-existing map data) are split into
run_overpass_dependent_checks() so they can be retried on their own
later if Overpass was unavailable, without re-running -- and
duplicating -- the checks that don't need it and already succeeded.
"""
from dataclasses import dataclass
from shapely.geometry import Point, LineString, Polygon
from shapely.strtree import STRtree

import config
import geo_utils
import geocode
from fetch import OverpassUnavailable


@dataclass
class Issue:
    issue_type: str
    osm_type: str
    osm_id: int
    lat: float
    lon: float
    detail: str = ""


def check_mass_edit_and_revert(cs_meta, diff):
    issues = []
    n_create, n_modify, n_delete = len(diff["create"]), len(diff["modify"]), len(diff["delete"])
    tags = cs_meta.get("tags", {})
    created_by = tags.get("created_by", "")
    comment = tags.get("comment", "")
    looks_like_revert = any(
        sig.lower() in created_by.lower() or sig.lower() in comment.lower()
        for sig in config.REVERT_SIGNATURES
    )
    lat, lon = _changeset_centroid(cs_meta)
    cs_id = cs_meta["id"]

    if n_delete >= config.MASS_DELETE_THRESHOLD and not looks_like_revert:
        issues.append(Issue("mass delete without revert tag", "changeset", cs_id, lat, lon,
                             detail=f"changeset {cs_id}: {n_delete} objects deleted, no revert signature found"))
    if n_create >= config.MASS_CREATE_THRESHOLD:
        issues.append(Issue("mass upload (create)", "changeset", cs_id, lat, lon,
                             detail=f"changeset {cs_id}: {n_create} objects created"))
    if n_modify >= config.MASS_MODIFY_THRESHOLD:
        issues.append(Issue("mass upload (modify)", "changeset", cs_id, lat, lon,
                             detail=f"changeset {cs_id}: {n_modify} objects modified"))
    return issues


def check_comment_quality(cs_meta):
    tags = cs_meta.get("tags", {})
    comment = tags.get("comment", "").strip()
    lat, lon = _changeset_centroid(cs_meta)
    cs_id = cs_meta["id"]
    generic = {"mapping", "edit", "test", "update", "changes", "fix", "map", "editing"}

    if not comment:
        detail = f"changeset {cs_id}: comment is empty"
    elif len(comment) < 10:
        detail = f"changeset {cs_id}: comment too short to be informative: '{comment}'"
    elif comment.strip().lower() in generic:
        detail = f"changeset {cs_id}: generic, uninformative comment: '{comment}'"
    else:
        return []

    return [Issue("unclear changeset comment", "changeset", cs_id, lat, lon, detail=detail)]


def _changeset_centroid(cs_meta):
    try:
        return (cs_meta["min_lat"] + cs_meta["max_lat"]) / 2, (cs_meta["min_lon"] + cs_meta["max_lon"]) / 2
    except (KeyError, TypeError):
        return None, None


def _has_primary_tag(tags):
    for key in tags:
        if key in config.PRIMARY_TAG_KEYS:
            return True
        if ":" in key:
            prefix, _, rest = key.partition(":")
            if prefix in config.LIFECYCLE_PREFIXES and rest in config.PRIMARY_TAG_KEYS:
                return True
    return False


def check_untagged_and_missing_primary(elements):
    issues = []
    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _element_point(el)
        if lat is None:
            continue
        if not tags:
            if el["type"] == "way":
                issues.append(Issue("untagged way", "way", el["id"], lat, lon))
            continue
        if not _has_primary_tag(tags):
            issues.append(Issue("feature mapped without primary tag", el["type"], el["id"], lat, lon,
                                 detail=f"{el['type']} {el['id']}: tags present but none are a primary key: {list(tags.keys())}"))
    return issues


def check_wrong_tagging(elements):
    issues = []
    all_known_values = set()
    for values in config.ENUMERATED_KEY_VALUES.values():
        all_known_values |= values

    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _element_point(el)
        if lat is None:
            continue
        for key, allowed in config.ENUMERATED_KEY_VALUES.items():
            if key in tags and tags[key] not in allowed:
                value = tags[key]
                belongs_elsewhere = any(value in vals for k, vals in config.ENUMERATED_KEY_VALUES.items() if k != key)
                if belongs_elsewhere:
                    issues.append(Issue("wrong tagging", el["type"], el["id"], lat, lon,
                                         detail=f"'{key}={value}' looks like a value meant for a different key"))
        name = tags.get("name")
        if name and name.strip().lower() in all_known_values:
            issues.append(Issue("wrong tagging", el["type"], el["id"], lat, lon,
                                 detail=f"name='{name}' looks like a misplaced tag value, not a real name"))
    return issues


def _element_point(el):
    if el["type"] == "node" and "lat" in el:
        return el["lat"], el["lon"]
    geom = el.get("_geom")
    if geom is not None:
        return geo_utils.centroid_of(geom)
    return None, None


def check_duplicate_nodes(created_nodes):
    issues = []
    tol = config.DUPLICATE_NODE_TOLERANCE_M
    cell = max(tol, 0.01) / 111000
    buckets = {}
    for n in created_nodes:
        key = (round(n["lat"] / cell), round(n["lon"] / cell))
        buckets.setdefault(key, []).append(n)

    seen_pairs = set()
    for (kx, ky), nodes_here in buckets.items():
        neighbours = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbours.extend(buckets.get((kx + dx, ky + dy), []))
        for a in nodes_here:
            for b in neighbours:
                if a["id"] >= b["id"]:
                    continue
                pair = (a["id"], b["id"])
                if pair in seen_pairs:
                    continue
                d = geo_utils.haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                if d <= tol:
                    seen_pairs.add(pair)
                    issues.append(Issue("duplicated node", "node", a["id"], a["lat"], a["lon"],
                                         detail=f"within {d:.2f}m of node {b['id']}"))
    return issues


def check_duplicate_against_existing(created_nodes, context_nodes, fetch_module=None):
    issues = []
    tol = config.DUPLICATE_NODE_TOLERANCE_M
    cell = max(tol, 0.01) / 111000
    buckets = {}
    for nid, (lon, lat) in context_nodes.items():
        key = (round(lat / cell), round(lon / cell))
        buckets.setdefault(key, []).append((nid, lat, lon))

    for n in created_nodes:
        kx, ky = round(n["lat"] / cell), round(n["lon"] / cell)
        found = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other_id, other_lat, other_lon in buckets.get((kx + dx, ky + dy), []):
                    d = geo_utils.haversine_m(n["lat"], n["lon"], other_lat, other_lon)
                    if d <= tol:
                        found = (other_id, other_lat, other_lon, d)
                        break
                if found:
                    break
            if found:
                break
        if not found:
            continue

        other_id, other_lat, other_lon, d = found
        if fetch_module is not None:
            live = fetch_module.fetch_live_node(other_id)
            if live is None:
                continue
            live_lon, live_lat = live
            d = geo_utils.haversine_m(n["lat"], n["lon"], live_lat, live_lon)
            if d > tol:
                continue

        issues.append(Issue("duplicated node", "node", n["id"], n["lat"], n["lon"],
                             detail=f"within {d:.2f}m of pre-existing node {other_id}"))
    return issues


def check_dense_node_cluster(created_nodes):
    issues = []
    radius = config.DENSE_CLUSTER_RADIUS_M
    min_size = config.DENSE_CLUSTER_MIN_NODES
    cell = radius / 111000
    buckets = {}
    for n in created_nodes:
        key = (round(n["lat"] / cell), round(n["lon"] / cell))
        buckets.setdefault(key, []).append(n)

    flagged_cells = set()
    for (kx, ky) in list(buckets.keys()):
        if (kx, ky) in flagged_cells:
            continue
        neighbours = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbours.extend(buckets.get((kx + dx, ky + dy), []))
        if len(neighbours) >= min_size:
            flagged_cells.add((kx, ky))
            lat = sum(n["lat"] for n in neighbours) / len(neighbours)
            lon = sum(n["lon"] for n in neighbours) / len(neighbours)
            issues.append(Issue(
                "dense node cluster", "node", neighbours[0]["id"], lat, lon,
                detail=f"{len(neighbours)} new nodes packed within ~{radius}m of each other "
                       f"-- possible accidental or junk mass node creation",
            ))
    return issues


def check_duplicate_ways(new_ways):
    issues = []
    seen = {}
    for w in new_ways:
        if len(w["nodes"]) < 2:
            continue
        key = tuple(sorted(w["nodes"]))
        if key in seen:
            lat, lon = _way_point(w)
            if lat is not None:
                issues.append(Issue("duplicated way", "way", w["id"], lat, lon,
                                     detail=f"shares all the same nodes as way {seen[key]}"))
        else:
            seen[key] = w["id"]
    return issues


def _way_point(w):
    geom = w.get("_geom")
    return geo_utils.centroid_of(geom) if geom is not None else (None, None)


def check_building_geometry(new_buildings, context_buildings, fetch_module=None):
    issues = []
    context_ids = {b["id"] for b in context_buildings}
    all_polys = new_buildings + context_buildings
    geoms = [b["_geom"] for b in all_polys]
    valid_idx = [i for i, g in enumerate(geoms) if g is not None and g.is_valid and g.area > 0]
    if not valid_idx:
        return issues
    tree = STRtree([geoms[i] for i in valid_idx])

    checked_pairs = set()
    for b in new_buildings:
        geom = b.get("_geom")
        if geom is None or not geom.is_valid or geom.area == 0:
            continue
        for j in tree.query(geom):
            other = all_polys[valid_idx[j]]
            if other["id"] == b["id"]:
                continue
            pair = tuple(sorted((b["id"], other["id"])))
            if pair in checked_pairs:
                continue
            other_geom = other["_geom"]
            if not geom.intersects(other_geom):
                continue

            if fetch_module is not None and other["id"] in context_ids:
                fresh_geom = fetch_module.fetch_live_way_geometry(other["id"])
                if fresh_geom is not None:
                    other_geom = fresh_geom
                    if not geom.is_valid or not other_geom.is_valid or not geom.intersects(other_geom):
                        continue

            checked_pairs.add(pair)
            lat, lon = geo_utils.centroid_of(geom)

            if geom.within(other_geom) or other_geom.within(geom):
                issues.append(Issue("building inside building", "way", b["id"], lat, lon,
                                     detail=f"fully contained with/containing way {other['id']}"))
                continue

            inter_area = geom.intersection(other_geom).area
            ratio = inter_area / min(geom.area, other_geom.area)
            if ratio < config.BUILDING_OVERLAP_MIN_RATIO:
                continue

            if geom.boundary.crosses(other_geom.boundary):
                issues.append(Issue("crossing buildings", "way", b["id"], lat, lon,
                                     detail=f"outline crosses way {other['id']} ({ratio:.0%} area overlap)"))
            else:
                issues.append(Issue("overlapping buildings", "way", b["id"], lat, lon,
                                     detail=f"{ratio:.0%} area overlap with way {other['id']}"))
    return issues


def check_way_crossings(new_lines, context_lines, issue_name):
    issues = []
    all_lines = new_lines + context_lines
    geoms = [w["_geom"] for w in all_lines]
    valid_idx = [i for i, g in enumerate(geoms) if g is not None]
    if not valid_idx:
        return issues
    tree = STRtree([geoms[i] for i in valid_idx])
    checked = set()

    for w in new_lines:
        geom = w.get("_geom")
        if geom is None:
            continue
        shared_nodes = set(w["nodes"])
        for j in tree.query(geom):
            other = all_lines[valid_idx[j]]
            if other["id"] == w["id"]:
                continue
            pair = tuple(sorted((w["id"], other["id"])))
            if pair in checked:
                continue
            other_geom = other["_geom"]
            if shared_nodes & set(other.get("nodes", [])):
                continue
            if geom.crosses(other_geom):
                checked.add(pair)
                inter = geom.intersection(other_geom)
                lat, lon = geo_utils.centroid_of(inter if not inter.is_empty else geom)
                issues.append(Issue(issue_name, "way", w["id"], lat, lon,
                                     detail=f"crosses way {other['id']} without a shared junction node"))
    return issues


def check_overlapping_highways(new_highways, context_highways):
    issues = []
    for w in new_highways:
        geom = w.get("_geom")
        w_class = w.get("tags", {}).get("highway")
        if geom is None or geom.length == 0 or not w_class:
            continue
        buffered = geom.buffer(0.00003)
        for other in context_highways + new_highways:
            if other["id"] == w["id"]:
                continue
            if other.get("tags", {}).get("highway") != w_class:
                continue
            other_geom = other.get("_geom")
            if other_geom is None or other_geom.length == 0:
                continue
            overlap_geom = other_geom.intersection(buffered)
            overlap_len = overlap_geom.length
            ratio = overlap_len / other_geom.length
            if ratio > 0.6:
                lat, lon = geo_utils.centroid_of(overlap_geom if not overlap_geom.is_empty else geom)
                issues.append(Issue("overlapping highway", "way", w["id"], lat, lon,
                                     detail=f"runs alongside existing way {other['id']} (both '{w_class}') "
                                            f"for {ratio:.0%} of its length"))
                break
    return issues


def check_node_connects_highway_and_building(new_buildings, context_buildings, new_highways, context_highways):
    new_building_ids = {b["id"] for b in new_buildings}
    new_highway_ids = {h["id"] for h in new_highways}

    issues = []
    building_nodes = {}
    for b in new_buildings + context_buildings:
        for n in b["nodes"]:
            building_nodes.setdefault(n, []).append(b["id"])

    flagged = set()
    for h in new_highways + context_highways:
        if h.get("tags", {}).get("covered") == "yes":
            continue
        for n in h["nodes"]:
            if n in building_nodes and n not in flagged:
                building_id = building_nodes[n][0]
                if h["id"] not in new_highway_ids and building_id not in new_building_ids:
                    continue
                flagged.add(n)
                geom = h.get("_geom")
                if geom is None:
                    continue
                lat, lon = geo_utils.centroid_of(geom)
                issues.append(Issue("node connected highway and building", "node", n, lat, lon,
                                     detail=f"shared by highway way {h['id']} and building way {building_id}"))
    return issues


def check_endpoint_near_other_way(new_ways, context_ways):
    issues = []
    all_ways = new_ways + context_ways
    geoms = [w.get("_geom") for w in all_ways]
    valid = [(g, w) for g, w in zip(geoms, all_ways) if isinstance(g, LineString)]
    if not valid:
        return issues
    tree = STRtree([g for g, _ in valid])

    for w in new_ways:
        geom = w.get("_geom")
        if geom is None or not isinstance(geom, LineString) or len(w["nodes"]) < 2:
            continue
        endpoints = [(geom.coords[0], w["nodes"][0]), (geom.coords[-1], w["nodes"][-1])]
        for coord, node_id in endpoints:
            pt = Point(coord)
            for j in tree.query(pt.buffer(0.0001)):
                other_geom, other = valid[j]
                if other["id"] == w["id"] or node_id in other.get("nodes", []):
                    continue
                nearest_pt = other_geom.interpolate(other_geom.project(pt))
                dist_m = geo_utils.haversine_m(pt.y, pt.x, nearest_pt.y, nearest_pt.x)
                if config.ENDPOINT_NEAR_WAY_MIN_M <= dist_m <= config.ENDPOINT_NEAR_WAY_THRESHOLD_M:
                    issues.append(Issue("way end node near other way", "node", node_id, pt.y, pt.x,
                                         detail=f"{dist_m:.2f}m from way {other['id']} but not connected to it"))
                    break
    return issues


def check_sudden_highway_classification_change(new_highways, context_highways):
    all_highways = new_highways + context_highways
    node_to_ways = {}
    for w in all_highways:
        nodes = w.get("nodes")
        if not nodes:
            continue
        for nid in {nodes[0], nodes[-1]}:
            node_to_ways.setdefault(nid, []).append(w)

    issues = []
    for b in new_highways:
        geom = b.get("_geom")
        b_class = b.get("tags", {}).get("highway")
        nodes = b.get("nodes")
        if geom is None or not b_class or not nodes or len(nodes) < 2:
            continue
        if geo_utils.line_length_m(geom) > config.SUDDEN_CLASS_CHANGE_MAX_LENGTH_M:
            continue

        start_neighbors = [w for w in node_to_ways.get(nodes[0], []) if w["id"] != b["id"]]
        end_neighbors = [w for w in node_to_ways.get(nodes[-1], []) if w["id"] != b["id"]]
        if len(start_neighbors) != 1 or len(end_neighbors) != 1:
            continue

        a_class = start_neighbors[0].get("tags", {}).get("highway")
        c_class = end_neighbors[0].get("tags", {}).get("highway")
        if a_class and a_class == c_class and a_class != b_class:
            lat, lon = geo_utils.centroid_of(geom)
            issues.append(Issue(
                "sudden highway classification change", "way", b["id"], lat, lon,
                detail=f"short '{b_class}' segment (~{geo_utils.line_length_m(geom):.0f}m) sandwiched "
                       f"between '{a_class}' ways {start_neighbors[0]['id']} and {end_neighbors[0]['id']}",
            ))
    return issues


def check_broken_highway_continuity(new_highways, context_highways):
    issues = []
    new_highway_ids = {h["id"] for h in new_highways}
    all_highways = new_highways + context_highways

    def road_key(w):
        tags = w.get("tags", {})
        return tags.get("name") or tags.get("ref")

    groups = {}
    for w in all_highways:
        key = road_key(w)
        if key and w.get("_geom") is not None and w.get("nodes"):
            groups.setdefault(key, []).append(w)

    seen_pairs = set()
    for key, ways in groups.items():
        if len(ways) < 2:
            continue
        for i, w1 in enumerate(ways):
            geom1 = w1["_geom"]
            endpoints1 = [(geom1.coords[0], w1["nodes"][0]), (geom1.coords[-1], w1["nodes"][-1])]
            for w2 in ways[i + 1:]:
                if w1["id"] not in new_highway_ids and w2["id"] not in new_highway_ids:
                    continue
                if set(w1["nodes"]) & set(w2["nodes"]):
                    continue
                geom2 = w2["_geom"]
                endpoints2 = [(geom2.coords[0], w2["nodes"][0]), (geom2.coords[-1], w2["nodes"][-1])]
                for coord1, _ in endpoints1:
                    for coord2, _ in endpoints2:
                        d = geo_utils.haversine_m(coord1[1], coord1[0], coord2[1], coord2[0])
                        if d <= config.BROKEN_CONTINUITY_MAX_GAP_M:
                            pair = tuple(sorted((w1["id"], w2["id"])))
                            if pair in seen_pairs:
                                continue
                            seen_pairs.add(pair)
                            issues.append(Issue(
                                "broken highway continuity", "way", w1["id"], coord1[1], coord1[0],
                                detail=f"'{key}' looks continuous but way {w1['id']} and way {w2['id']} "
                                       f"are {d:.2f}m apart with no shared node",
                            ))
    return issues


def check_floating_highway(new_highways, context_highways, fetch_module=None):
    all_highways = new_highways + context_highways
    node_to_ways = {}
    for w in all_highways:
        for nid in (w.get("nodes") or []):
            node_to_ways.setdefault(nid, set()).add(w["id"])

    issues = []
    for w in new_highways:
        nodes = w.get("nodes")
        geom = w.get("_geom")
        if not nodes or len(nodes) < 2 or geom is None:
            continue
        start_connections = node_to_ways.get(nodes[0], set()) - {w["id"]}
        end_connections = node_to_ways.get(nodes[-1], set()) - {w["id"]}
        if start_connections or end_connections:
            continue

        if w.get("tags", {}).get("noexit") == "yes":
            continue

        if fetch_module is not None:
            confirmed_isolated = True
            for endpoint_node in (nodes[0], nodes[-1]):
                real_ways = fetch_module.fetch_ways_using_node(endpoint_node)
                if real_ways is None or (real_ways - {w["id"]}):
                    confirmed_isolated = False
                    break
            if not confirmed_isolated:
                continue

        lat, lon = geo_utils.centroid_of(geom)
        issues.append(Issue(
            "floating highway", "way", w["id"], lat, lon,
            detail=f"way {w['id']}: neither endpoint connects to any other highway -- isolated from the road network",
        ))
    return issues


def run_overpass_dependent_checks(cs_meta, new_ways, diff, fetch_module):
    if not new_ways:
        return [], False

    changeset_node_index = {
        n["id"]: n for n in diff["create"] + diff["modify"]
        if n["type"] == "node" and n.get("lat") is not None
    }
    all_needed_nodes = {n for w in new_ways for n in w["nodes"]}
    node_coords = fetch_module.fetch_node_coords(list(all_needed_nodes), changeset_node_index)
    for w in new_ways:
        w["_geom"] = geo_utils.build_way_geometry(w["nodes"], node_coords, w.get("tags"))

    try:
        context_ways, context_nodes = fetch_module.fetch_overpass_context(
            cs_meta["min_lat"], cs_meta["min_lon"], cs_meta["max_lat"], cs_meta["max_lon"],
            exclude_way_ids={w["id"] for w in new_ways},
            exclude_node_ids=all_needed_nodes,
        )
    except OverpassUnavailable:
        return [], True
    except (KeyError, TypeError):
        context_ways, context_nodes = [], {}

    for w in context_ways:
        w["_geom"] = geo_utils.build_way_geometry(w["nodes"], context_nodes, w.get("tags"))

    issues = []
    created_nodes = [e for e in diff["create"] if e["type"] == "node" and e.get("lat") is not None]
    issues += check_duplicate_against_existing(created_nodes, context_nodes, fetch_module)

    def is_building(w):
        return w.get("tags", {}).get("building") and isinstance(w.get("_geom"), Polygon)

    def is_highway(w):
        return "highway" in w.get("tags", {}) and isinstance(w.get("_geom"), LineString)

    def is_plain_way(w):
        return isinstance(w.get("_geom"), LineString) and not is_highway(w)

    new_buildings = [w for w in new_ways if is_building(w)]
    context_buildings = [w for w in context_ways if is_building(w)]
    issues += check_building_geometry(new_buildings, context_buildings, fetch_module)

    new_highways = [w for w in new_ways if is_highway(w)]
    context_highways = [w for w in context_ways if is_highway(w)]
    issues += check_way_crossings(new_highways, context_highways, "crossing highway")
    issues += check_way_crossings([w for w in new_ways if is_plain_way(w)],
                                   [w for w in context_ways if is_plain_way(w)],
                                   "crossing way")
    issues += check_overlapping_highways(new_highways, context_highways)
    issues += check_sudden_highway_classification_change(new_highways, context_highways)
    issues += check_broken_highway_continuity(new_highways, context_highways)
    issues += check_floating_highway(new_highways, context_highways, fetch_module)
    issues += check_node_connects_highway_and_building(
        new_buildings, context_buildings, new_highways, context_highways,
    )
    issues += check_endpoint_near_other_way(new_ways, context_ways)

    return issues, False


def run_all_checks(cs_meta, diff, fetch_module):
    issues = []
    issues += check_mass_edit_and_revert(cs_meta, diff)
    issues += check_comment_quality(cs_meta)

    changed_elements = diff["create"] + diff["modify"]
    non_relations = [e for e in changed_elements if e["type"] != "relation"]
    issues += check_untagged_and_missing_primary(non_relations)
    issues += check_wrong_tagging(changed_elements)

    created_nodes = [e for e in diff["create"] if e["type"] == "node" and e.get("lat") is not None]
    issues += check_duplicate_nodes(created_nodes)
    issues += check_dense_node_cluster(created_nodes)

    new_ways = [e for e in changed_elements if e["type"] == "way"]
    issues += check_duplicate_ways(new_ways)

    overpass_issues, incomplete = run_overpass_dependent_checks(cs_meta, new_ways, diff, fetch_module)
    issues += overpass_issues
    return issues, incomplete


def to_row(cs_meta, issue: Issue):
    country = geocode.country_for(issue.lat, issue.lon)
    return {
        "error_type": issue.issue_type,
        "username": cs_meta.get("user", "unknown"),
        "user_id": cs_meta.get("uid", ""),
        "osm_location_link": geo_utils.map_link(issue.lat, issue.lon) if issue.lat is not None else "",
        "changeset_id": cs_meta["id"],
        "changeset_link": geo_utils.changeset_link(cs_meta["id"]),
        "osm_object_type": issue.osm_type,
        "osm_object_id": issue.osm_id,
        "time_utc": cs_meta.get("closed_at") or cs_meta.get("created_at"),
        "country": country,
        "detail": issue.detail,
        "hashtag": config.HASHTAG,
    }
