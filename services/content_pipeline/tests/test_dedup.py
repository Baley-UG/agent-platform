"""Pure-logic tests for the dedup helpers."""

from app.services.dedup import hamming_distance


def test_identical_returns_zero():
    a = bytes([0xAB, 0xCD, 0xEF])
    assert hamming_distance(a, a) == 0


def test_one_bit_difference_returns_one():
    a = bytes([0b00000000])
    b = bytes([0b00000001])
    assert hamming_distance(a, b) == 1


def test_byte_difference_counts_all_bits():
    # 0xAB = 0b10101011 ; 0x54 = 0b01010100  → 8 bits differ
    a = bytes([0xAB])
    b = bytes([0x54])
    assert hamming_distance(a, b) == 8


def test_unequal_lengths_return_minus_one():
    assert hamming_distance(bytes([1, 2]), bytes([1, 2, 3])) == -1


def test_none_returns_minus_one():
    assert hamming_distance(None, bytes([1])) == -1
    assert hamming_distance(bytes([1]), None) == -1
