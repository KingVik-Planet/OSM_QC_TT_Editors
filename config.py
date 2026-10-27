"""
Central configuration for the OSM #tt_event quality-check pipeline.

Everything here can be overridden with an environment variable, so the
same code runs unchanged locally (via a .env file you load yourself,
e.g. with `python-dotenv` or just `export`ing before running) and in
GitHub Actions (via repository secrets passed in as env vars in the
workflow file).
"""
import os

# --- What we're watching for ------------------------------------------------
# This deployment tracks a fixed list of OSM usernames directly (via the
# OSM API's own display_name= filter), not a #hashtag. HASHTAG is kept
# as a plain, fixed LABEL for the CSV's 13th column only -- it is no
# longer used anywhere as a search filter (see fetch.py's
# fetch_changesets_in_window, which queries TRACKED_USERNAMES instead).
HASHTAG = os.getenv("QC_HASHTAG", "TT_Editor")

# The editors this deployment watches. Every changeset any of these
# usernames closes gets checked, worldwide, the same as every other
# check in this pipeline -- just discovered per-user instead of by
# scanning every changeset on the planet for a hashtag.
TRACKED_USERNAMES = [
    'SK@_TT',
    'DomiAr',
    'Hajar-El',
    'Tomek_Koz',
    'KasiaaBaa',
    'vladaboitsik_tt',
    'Mapiate',
    'unenTT',
    'ahirek',
    'Priscilla_Zachée',
    'KingVik_TT',
    'Cotelabarca',
    'changio',
    'CasGroenigen',
    'spughetti_tomtom',
    'Natasha963',
    'Przemek_Bo',
    'Tabzi',
    'DS147',
    'sbaido',
    'TTCHLOE',
    'NodeNinja',
    'MapHappyMe',
    'sbp@tomtom',
    'RitaMine',
    'LeticiaB',
    'SolbaP',
    'Zorka Marinovic',
    'RiteshN',
    'Amol_Pawar_TT',
    'ShreyasBhogte26_TT',
    'MappingMore',
    'Ziad_abdalhamed',
    'MMersch_TT',
    'Peter LeGras',
    'hcyen66',
    'HappyK3C3',
    'Zuraimi Idris',
    'TTAmar',
    'Dinoafr',
    'DarioS_TT',
    'SupanchatMetta',
    'Sebasawk',
    'dazzlep',
    'Geo_Cop',
    'Ra_colate',
    'Swihail',
    'BR_pebble',
    'NumisK',
    'Crick_RN',
    'melody_Tej',
    'Ky_AT',
    'optimistic9',
    '007Transformer',
    'BerkU_',
    'Captain_America_802',
    'NewEngland_CountryGirl',
    'TT_Chili_King',
    'Training_Queen',
    'darylcav',
    'David_TT',
    'whitepanda',
    'Archi_Ma',
    'Flamen_Sru',
    'ajmat',
    'rajdhani',
    'Wildsharky',
    'Jebel_Hafeet',
    'Metauro',
    'Kaduna',
    'Gorger',
    'World_Winner',
    'dwarfs_rule',
    'Dwight-Schrute',
    'Nitz1981',
    'Fil_Vim',
    'Nar_ach',
    'Tinetto',
    'Sammamish',
    'Shel_Sri',
    'Saint_Vincent',
    'Shre_WD',
    'Pan_Nug',
    'Bod_Ger',
    'Byenin',
    'Geocrest',
    'Cliffmap',
    'Carto_Guy',
    'MountMapper',
    'PathRover',
    'Palary',
    'Char_Vad',
]

# --- OSM API -----------------------------------------------------------------
OSM_API_BASE = os.getenv("OSM_API_BASE", "https://api.openstreetmap.org/api/0.6")

# --- Overpass ------------------------------------------------------------
OVERPASS_ENDPOINTS = [
    os.getenv("OVERPASS_PRIMARY", "https://overpass-api.de/api/interpreter"),
    os.getenv("OVERPASS_FALLBACK", "https://overpass.kumi.systems/api/interpreter"),
]
OVERPASS_CONTEXT_BUFFER_M = float(os.getenv("QC_OVERPASS_BUFFER_M", 50))
OVERPASS_HTTP_TIMEOUT_S = int(os.getenv("QC_OVERPASS_HTTP_TIMEOUT_S", 45))
OVERPASS_QUERY_TIMEOUT_S = int(os.getenv("QC_OVERPASS_QUERY_TIMEOUT_S", 35))
OVERPASS_RETRIES = int(os.getenv("QC_OVERPASS_RETRIES", 1))

MAX_RETRY_PER_RUN = int(os.getenv("QC_MAX_RETRY_PER_RUN", 300))
MAX_SCAN_RETRY_PER_RUN = int(os.getenv("QC_MAX_SCAN_RETRY_PER_RUN", 20))

# --- osmcha (optional enrichment only -- never a hard dependency) -----------
OSMCHA_API_BASE = os.getenv("OSMCHA_API_BASE", "https://osmcha.org/api/v1")
OSMCHA_TOKEN = os.getenv("OSMCHA_TOKEN")

# --- Nominatim (reverse geocoding for "country") -----------------------------
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
NOMINATIM_USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "osm-tt-editors-quality-check/1.0 (set a contact email in this User-Agent)",
)
NOMINATIM_MIN_INTERVAL_S = 1.1

# --- Thresholds: mass upload / delete ----------------------------------------
MASS_CREATE_THRESHOLD = int(os.getenv("QC_MASS_CREATE_THRESHOLD", 10000))
MASS_MODIFY_THRESHOLD = int(os.getenv("QC_MASS_MODIFY_THRESHOLD", 10000))
MASS_DELETE_THRESHOLD = int(os.getenv("QC_MASS_DELETE_THRESHOLD", 10000))
REVERT_SIGNATURES = ("reverter_plugin", "revert")

# --- Geometry check tolerances ------------------------------------------------
DUPLICATE_NODE_TOLERANCE_M = float(os.getenv("QC_DUP_NODE_TOLERANCE_M", 0.05))
BUILDING_OVERLAP_MIN_RATIO = float(os.getenv("QC_BUILDING_OVERLAP_MIN_RATIO", 0.02))
ENDPOINT_NEAR_WAY_MIN_M = float(os.getenv("QC_ENDPOINT_NEAR_WAY_MIN_M", 0.02))
ENDPOINT_NEAR_WAY_THRESHOLD_M = float(os.getenv("QC_ENDPOINT_NEAR_WAY_M", 0.5))
DENSE_CLUSTER_RADIUS_M = float(os.getenv("QC_DENSE_CLUSTER_RADIUS_M", 1.0))
DENSE_CLUSTER_MIN_NODES = int(os.getenv("QC_DENSE_CLUSTER_MIN_NODES", 15))
SUDDEN_CLASS_CHANGE_MAX_LENGTH_M = float(os.getenv("QC_SUDDEN_CLASS_CHANGE_MAX_M", 50))
BROKEN_CONTINUITY_MAX_GAP_M = float(os.getenv("QC_BROKEN_CONTINUITY_MAX_GAP_M", 5.0))

# --- Storage -------------------------------------------------------------
DATA_DIR = os.getenv("QC_DATA_DIR", "data")
CSV_BASENAME = os.getenv("QC_CSV_BASENAME", "quality_check")
CSV_MAX_BYTES = 40 * 1024 * 1024
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# --- Slack -------------------------------------------------------------
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
SLACK_ENABLED = os.getenv("QC_SLACK_ENABLED", "") in ("1", "true", "True")

# --- Tag-key whitelists used by the "wrong tagging" check --------------------
ENUMERATED_KEY_VALUES = {
    "area": {"yes", "no"},
    "building": {
        "yes", "house", "residential", "apartments", "detached", "terrace",
        "semidetached_house", "garage", "garages", "commercial", "industrial",
        "retail", "warehouse", "school", "church", "hospital", "hotel",
        "office", "roof", "hut", "shed", "cabin", "farm", "farm_auxiliary",
        "barn", "greenhouse", "service", "civic", "public", "stadium",
        "train_station", "transportation", "kiosk", "construction", "ruins",
        "collapsed", "no",
    },
    "highway": {
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "unclassified", "residential", "service", "track", "path",
        "footway", "cycleway", "bridleway", "steps", "pedestrian",
        "living_street", "road", "motorway_link", "trunk_link",
        "primary_link", "secondary_link", "tertiary_link", "construction",
        "proposed", "bus_stop", "crossing", "traffic_signals", "give_way",
        "stop", "mini_roundabout", "turning_circle", "milestone", "elevator",
    },
    "landuse": {
        "residential", "commercial", "industrial", "retail", "farmland",
        "farmyard", "forest", "meadow", "grass", "orchard", "vineyard",
        "quarry", "cemetery", "construction", "military", "railway",
        "recreation_ground", "allotments", "landfill", "brownfield",
        "greenfield",
    },
    "natural": {
        "wood", "water", "wetland", "tree", "tree_row", "scrub",
        "grassland", "heath", "bare_rock", "sand", "beach", "cliff",
        "coastline", "peak", "valley", "ridge", "glacier", "volcano", "bay",
    },
    "waterway": {
        "river", "stream", "canal", "drain", "ditch", "dam", "weir",
        "waterfall", "riverbank", "boatyard",
    },
    "railway": {
        "rail", "subway", "light_rail", "tram", "narrow_gauge", "monorail",
        "funicular", "station", "halt", "platform", "construction",
        "abandoned", "disused",
    },
    "amenity": {
        "restaurant", "cafe", "school", "hospital", "bank", "pharmacy",
        "fuel", "parking", "place_of_worship", "toilets", "bar", "pub",
        "fast_food", "police", "fire_station", "post_office", "library",
        "clinic", "marketplace", "waste_basket", "bench", "drinking_water",
        "atm", "kindergarten", "university", "college", "townhall",
        "community_centre", "social_facility", "veterinary",
    },
}

PRIMARY_TAG_KEYS = {
    "advertising", "aerialway", "aeroway", "amenity", "barrier",
    "boundary", "building", "club", "craft", "departures_board",
    "education", "emergency", "geological", "healthcare", "highway",
    "historic", "landcover", "landuse", "leisure", "man_made",
    "military", "natural", "office", "piste:type", "place", "power",
    "public_transport", "railway", "route", "shop", "telecom",
    "tourism", "waterway",
    "addr:interpolation", "allotments", "area:highway", "attraction",
    "building:part", "bridge:support", "cemetery", "entrance", "ford",
    "golf", "indoor", "junction", "noexit", "playground",
    "roller_coaster", "traffic_calming", "traffic_sign",
}

LIFECYCLE_PREFIXES = {
    "proposed", "planned", "construction", "disused", "abandoned",
    "ruins", "demolished", "removed", "razed", "destroyed", "was",
    "former", "closed",
}
