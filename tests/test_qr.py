from wattracker.web.qr import _matrix, pairing_qr_svg


def test_pairing_qr_is_a_version_one_svg_with_quiet_zone():
    svg = pairing_qr_svg("ABCD-EFGH-JKMN")
    assert svg is not None
    assert 'viewBox="0 0 29 29"' in svg
    assert 'shape-rendering="crispEdges"' in svg
    assert svg.count("M") > 20


def test_pairing_qr_timing_patterns_start_dark():
    matrix = _matrix(b"ABCD-EFGH-JKMN")
    assert matrix[6][8:13] == [1, 0, 1, 0, 1]
    assert [matrix[row][6] for row in range(8, 13)] == [1, 0, 1, 0, 1]


def test_pairing_qr_rejects_payloads_that_do_not_fit():
    assert pairing_qr_svg("x" * 18) is None
    assert pairing_qr_svg("é") is None
