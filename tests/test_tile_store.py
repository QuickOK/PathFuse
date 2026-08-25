# tests/test_tile_store.py
import tile_store as T


def test_encode_canonical_vector():
    # The reference vector every geohash implementation agrees on.
    assert T.encode(57.64911, 10.40744, precision=11) == "u4pruydqqvj"


def test_encode_precision_7_is_a_prefix_of_11():
    assert T.encode(57.64911, 10.40744, 7) == "u4pruyd"


def test_encode_corners():
    assert T.encode(-90.0, -180.0, 7) == "0000000"
    assert T.encode(89.9999, 179.9999, 7) == "zzzzzzz"


def test_encode_southern_hemisphere_positive_longitude():
    assert T.encode(-33.8688, 151.2093, 7) == "r3gx2f7"


def test_nearby_points_share_a_tile():
    # ~6 m apart: well inside one geohash-7 cell.
    assert T.encode(41.100000, -73.500000) == T.encode(41.100050, -73.500050)


def test_points_hundreds_of_metres_apart_do_not():
    assert T.encode(41.100000, -73.500000) != T.encode(41.104000, -73.500000)


def test_bbox_contains_its_own_centre():
    tile = T.encode(41.1, -73.5)
    south, west, north, east = T.bbox(tile)
    lat, lon = T.center(tile)
    assert south < lat < north
    assert west < lon < east
    assert T.encode(lat, lon) == tile


def test_bbox_is_about_150_m_tall():
    south, _, north, _ = T.bbox(T.encode(41.1, -73.5))
    assert 130.0 < (north - south) * 111320.0 < 170.0
