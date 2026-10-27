"""
Talks to the OSM API, Overpass, and (optionally) osmcha to find
changesets by a tracked list of OSM usernames in a given UTC time
window, and pull down the data needed to run checks on them.

This is the username-based sibling of the hashtag-based pipeline: OSM's
API supports filtering changesets directly by editor (display_name=),
so instead of scanning every changeset on the planet and filtering by
comment text client-side, this queries each tracked editor directly --
precise, far lower volume per query, and avoids the global-scan
performance problems (massive pagination, rate-limiting, stuck narrow
slices) that come with a worldwide hashtag scan.
"""
import logging
import time
from datetime import datetime

import requests
import xml.etree.ElementTree as ET

import config
import geo_utils

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "osm-tt-editors-quality-check/1.0"}


class OverpassUnavailable(Exception):
    """
    Raised when Overpass context could not be fetched -- either every
    mirror failed every retry, or the circuit breaker is currently open.
    Distinct from "Overpass answered and there's genuinely nothing
    nearby" (which returns an empty result normally, not an exception),
    so callers can tell the difference between "no context exists" and
    "we couldn't find out" -- the latter needs to be retried later, the
    former is a final, correct answer.
    """
    pass


# --- Overpass circuit breaker -----------------------------------------------
_OVERPASS_CIRCUIT_THRESHOLD = 1
_overpass_consecutive_failures = 0


def overpass_circuit_is_open():
    return _overpass_consecutive_failures >= _OVERPASS_CIRCUIT_THRESHOLD


def _get(url, params=None, headers=None, timeout=60, max_attempts=3):
    """
    Light retry wrapper for OSM API calls. A couple of quick retries
    absorb the vast majority of brief hiccups (a 503, a slow first
    byte) without meaningfully slowing the normal, successful case.
    """
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, params=params, headers=h, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_attempts:
                wait = 2 * attempt
                log.info("OSM API request failed (attempt %d/%d): %s -- retrying in %ds",
                         attempt, max_attempts, e, wait)
                time.sleep(wait)
    raise last_exc


def _parse_osm_dt(s):
    return datetime.strptime(s.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")


def _fetch_user_changesets(username, start_dt, end_dt):
    """
    Returns (found_dict, unresolved_list) for one tracked editor's
    closed changesets in [start_dt, end_dt) UTC, using OSM's own
    display_name= filter -- no client-side text matching needed at all,
    since the API itself guarantees every result actually belongs to
    this user.

    Still paginates defensively (same cursor-narrowing approach as the
    hashtag pipeline), in case a single prolific editor closes more than
    100 changesets in one hour -- rare, but not impossible.

    `unresolved` is a list of (username, start_iso, end_iso) tuples --
    scoped per-user now, not just per-time-range, since a persistent
    failure here is tied to querying THIS specific editor, not the
    whole planet.
    """
    found = {}
    unresolved = []
    cursor_end = end_dt

    while True:
        params = {
            "display_name": username,
            "time": f"{start_dt.isoformat()}Z,{cursor_end.isoformat()}Z",
            "closed": "true",
        }
        try:
            resp = _get(f"{config.OSM_API_BASE}/changesets.json", params=params)
        except requests.RequestException as e:
            log.warning(
                "Persistent failure scanning changesets for %s from %s to %s -- "
                "queuing this slice for later retry instead of blocking "
                "the whole window: %s",
                username, start_dt.isoformat(), cursor_end.isoformat(), e,
            )
            unresolved.append((username, start_dt.isoformat(), cursor_end.isoformat()))
            break

        data = resp.json().get("changesets", [])
        if not data:
            break

        for cs in data:
            found[cs["id"]] = cs

        if len(data) < 100:
            break  # reached the last page

        oldest_seen = min((c.get("closed_at") or c.get("created_at")) for c in data)
        new_cursor_end = _parse_osm_dt(oldest_seen)
        if new_cursor_end <= start_dt or new_cursor_end >= cursor_end:
            break
        cursor_end = new_cursor_end

    return found, unresolved


def fetch_changesets_in_window(start_dt, end_dt):
    """
    Returns (found, unresolved) for every closed changeset in
    [start_dt, end_dt) UTC made by any username in config.TRACKED_USERNAMES.

    `unresolved` is a list of (username, start_iso, end_iso) tuples for
    any tracked editor whose query persistently failed this run --
    queue these for a later retry (per storage's pending-changeset-scan
    queue) rather than treating the window as fully scanned for that
    user. Keeps the same name as the hashtag pipeline's equivalent
    function so main.py and the retry-queue plumbing need no changes
    beyond handling the extra `username` field in each queue entry.
    """
    if not config.TRACKED_USERNAMES:
        raise ValueError(
            "config.TRACKED_USERNAMES is empty -- refusing to scan, since "
            "there would be nothing to search for."
        )

    found = {}
    unresolved = []
    log.info("Scanning changesets for %d tracked editor(s): %s",
              len(config.TRACKED_USERNAMES), ", ".join(config.TRACKED_USERNAMES))

    for username in config.TRACKED_USERNAMES:
        user_found, user_unresolved = _fetch_user_changesets(username, start_dt, end_dt)
        found.update(user_found)
        unresolved.extend(user_unresolved)

    return list(found.values()), unresolved


def fetch_changeset_meta(changeset_id):
    """
    Fetches a single changeset's own metadata (user, bbox, tags, etc.)
    directly by ID. Used when retrying a changeset from the pending
    Overpass-recheck queue.
    Returns None if the changeset can't be fetched (deleted, hidden,
    network error).
    """
    try:
        resp = _get(f"{config.OSM_API_BASE}/changeset/{changeset_id}.json")
        return resp.json().get("changeset")
    except requests.RequestException as e:
        log.warning("Could not fetch metadata for pending changeset %s: %s", changeset_id, e)
        return None


def fetch_changeset_diff(changeset_id):
    """
    Downloads and parses the osmChange for a changeset. Returns:
    {"create": [...], "modify": [...], "delete": [...]}
    """
    resp = _get(f"{config.OSM_API_BASE}/changeset/{changeset_id}/download")
    root = ET.fromstring(resp.content)
    out = {"create": [], "modify": [], "delete": []}
    for action in root:
        if action.tag not in out:
            continue
        for el in action:
            if el.tag not in ("node", "way", "relation"):
                continue
            item = {"type": el.tag, "id": int(el.get("id"))}
            if el.tag == "node":
                lat_str, lon_str = el.get("lat"), el.get("lon")
                item["lat"] = float(lat_str) if lat_str is not None else None
                item["lon"] = float(lon_str) if lon_str is not None else None
            if el.tag == "way":
                item["nodes"] = [int(nd.get("ref")) for nd in el.findall("nd")]
            item["tags"] = {t.get("k"): t.get("v") for t in el.findall("tag")}
            out[action.tag].append(item)
    return out


def fetch_node_coords(node_ids, changeset_nodes):
    coords = {}
    missing = []
    for nid in node_ids:
        n = changeset_nodes.get(nid)
        if n is not None and n.get("lat") is not None and n.get("lon") is not None:
            coords[nid] = (n["lon"], n["lat"])
        else:
            missing.append(nid)

    for i in range(0, len(missing), 700):
        batch = missing[i:i + 700]
        try:
            resp = _get(f"{config.OSM_API_BASE}/nodes.json", params={"nodes": ",".join(map(str, batch))})
            for el in resp.json().get("elements", []):
                lon, lat = el.get("lon"), el.get("lat")
                if lon is not None and lat is not None:
                    coords[el["id"]] = (lon, lat)
        except requests.RequestException as e:
            log.warning("Could not resolve %d node coords: %s", len(batch), e)
    return coords


def fetch_overpass_context(min_lat, min_lon, max_lat, max_lon, exclude_way_ids, exclude_node_ids):
    global _overpass_consecutive_failures

    if _overpass_consecutive_failures >= _OVERPASS_CIRCUIT_THRESHOLD:
        log.info(
            "Overpass circuit breaker open (%d consecutive full failures this run) "
            "-- skipping context fetch instantly instead of retrying",
            _overpass_consecutive_failures,
        )
        raise OverpassUnavailable("circuit breaker open")

    buf_deg = config.OVERPASS_CONTEXT_BUFFER_M / 111000
    s, w, n, e = min_lat - buf_deg, min_lon - buf_deg, max_lat + buf_deg, max_lon + buf_deg
    query = f"""
    [out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT_S}];
    (
      way["building"]({s},{w},{n},{e});
      way["highway"]({s},{w},{n},{e});
    );
    out body;
    >;
    out skel qt;
    """

    data = None
    last_err = None
    for endpoint in config.OVERPASS_ENDPOINTS:
        for attempt in range(1, config.OVERPASS_RETRIES + 1):
            try:
                resp = requests.post(
                    endpoint, data={"data": query}, headers=HEADERS,
                    timeout=config.OVERPASS_HTTP_TIMEOUT_S,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as e:
                last_err = e
                log.info("Overpass %s attempt %d/%d failed: %s",
                         endpoint, attempt, config.OVERPASS_RETRIES, e)
        if data is not None:
            break

    if data is None:
        _overpass_consecutive_failures += 1
        log.warning(
            "All Overpass endpoints failed after retries (%d consecutive full "
            "failures this run): %s", _overpass_consecutive_failures, last_err,
        )
        raise OverpassUnavailable(str(last_err))

    _overpass_consecutive_failures = 0
    nodes, ways = {}, []
    for el in data.get("elements", []):
        if el["type"] == "node" and el["id"] not in exclude_node_ids:
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way" and el["id"] not in exclude_way_ids:
            ways.append({"id": el["id"], "nodes": el.get("nodes", []), "tags": el.get("tags", {})})
    return ways, nodes


def fetch_live_node(node_id):
    try:
        resp = _get(f"{config.OSM_API_BASE}/node/{node_id}.json")
        el = resp.json().get("elements", [{}])[0]
        if el.get("type") != "node" or el.get("visible") is False:
            return None
        lon, lat = el.get("lon"), el.get("lat")
        if lon is None or lat is None:
            return None
        return (lon, lat)
    except Exception as e:
        log.info("Live re-check of node %s failed (treated as unconfirmed): %s", node_id, e)
        return None


def fetch_ways_using_node(node_id):
    query = f"[out:json][timeout:{config.OVERPASS_QUERY_TIMEOUT_S}];way(bn:{node_id});out ids;"
    try:
        resp = requests.post(
            config.OVERPASS_ENDPOINTS[0], data={"data": query}, headers=HEADERS,
            timeout=config.OVERPASS_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        return {el["id"] for el in data.get("elements", []) if el["type"] == "way"}
    except Exception as e:
        log.info("Could not verify node %s's real connections (treated as unconfirmed): %s", node_id, e)
        return None


def fetch_live_way_geometry(way_id):
    try:
        resp = _get(f"{config.OSM_API_BASE}/way/{way_id}/full.json")
        elements = resp.json().get("elements", [])
        way = next((e for e in elements if e["type"] == "way" and e["id"] == way_id), None)
        if way is None:
            return None
        node_coords = {e["id"]: (e["lon"], e["lat"]) for e in elements if e["type"] == "node"}
        return geo_utils.build_way_geometry(way.get("nodes", []), node_coords, way.get("tags"))
    except Exception as e:
        log.info("Live re-check of way %s failed (non-fatal, keeping original result): %s", way_id, e)
        return None


def fetch_osmcha_flags(changeset_id):
    if not config.OSMCHA_TOKEN:
        return {}
    try:
        resp = _get(
            f"{config.OSMCHA_API_BASE}/changesets/{changeset_id}/",
            headers={"Authorization": f"Token {config.OSMCHA_TOKEN}"},
        )
        return resp.json()
    except requests.RequestException as e:
        log.info("osmcha lookup failed for %s (non-fatal): %s", changeset_id, e)
        return {}
