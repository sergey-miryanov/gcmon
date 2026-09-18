"""Tests for protobuf wire-format encoder."""

import struct

from gcmon.exporters.protobuf_encoder import (
    encode_bytes_field,
    encode_double_field,
    encode_field_key,
    encode_fixed64_field,
    encode_string_field,
    encode_varint,
    encode_varint_field,
)


class TestEncodeVarint:
    def test_zero(self) -> None:
        assert encode_varint(0) == b"\x00"

    def test_small_value(self) -> None:
        assert encode_varint(1) == b"\x01"
        assert encode_varint(127) == b"\x7f"

    def test_two_byte_value(self) -> None:
        assert encode_varint(128) == b"\x80\x01"
        assert encode_varint(300) == b"\xac\x02"

    def test_large_value(self) -> None:
        assert encode_varint(16384) == b"\x80\x80\x01"

    def test_max_uint64(self) -> None:
        result = encode_varint(2**64 - 1)

        assert len(result) == 10
        assert result == b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01"

    def test_negative_value_wraps(self) -> None:
        result = encode_varint(-1)

        assert len(result) == 10
        assert result == b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01"

    def test_negative_value_int32(self) -> None:
        """Two's complement over 64 bits: 0xD6 is the low seven bits of -42
        with the continuation bit set."""
        assert encode_varint(-42) == b"\xd6\xff\xff\xff\xff\xff\xff\xff\xff\x01"


class TestEncodeFieldKey:
    def test_field_1_varint(self) -> None:
        assert encode_field_key(1, 0) == b"\x08"

    def test_field_1_fixed64(self) -> None:
        assert encode_field_key(1, 1) == b"\x09"

    def test_field_1_length_delimited(self) -> None:
        assert encode_field_key(1, 2) == b"\x0a"

    def test_field_2_varint(self) -> None:
        assert encode_field_key(2, 0) == b"\x10"

    def test_large_field_number(self) -> None:
        result = encode_field_key(60, 2)

        assert result == b"\xe2\x03"


class TestEncodeVarintField:
    def test_field_1_value_150(self) -> None:
        assert encode_varint_field(1, 150) == b"\x08\x96\x01"

    def test_field_8_timestamp(self) -> None:
        assert encode_varint_field(8, 1_000_000) == b"\x40\xc0\x84\x3d"


class TestEncodeFixed64Field:
    def test_field_1_value(self) -> None:
        result = encode_fixed64_field(1, 0x123456789ABCDEF0)

        assert result[0:1] == b"\x09"
        assert result[1:] == struct.pack("<Q", 0x123456789ABCDEF0)

    def test_field_5_value(self) -> None:
        result = encode_fixed64_field(5, 42)

        assert result[0:1] == b"\x29"
        assert result[1:] == struct.pack("<Q", 42)


class TestEncodeStringField:
    def test_empty_string(self) -> None:
        assert encode_string_field(1, "") == b"\x0a\x00"

    def test_short_string(self) -> None:
        result = encode_string_field(1, "test")

        assert result == b"\x0a\x04test"

    def test_unicode_string(self) -> None:
        result = encode_string_field(2, "café")

        assert result[0:2] == b"\x12\x05"
        assert result[2:] == "café".encode()


class TestEncodeBytesField:
    def test_empty_bytes(self) -> None:
        assert encode_bytes_field(1, b"") == b"\x0a\x00"

    def test_short_bytes(self) -> None:
        result = encode_bytes_field(1, b"\x01\x02\x03")

        assert result == b"\x0a\x03\x01\x02\x03"

    def test_nested_message(self) -> None:
        inner = encode_varint_field(1, 42)

        result = encode_bytes_field(2, inner)

        assert result[0:1] == b"\x12"
        assert result[1:2] == bytes([len(inner)])
        assert result[2:] == inner


class TestEncodeDoubleField:
    def test_zero(self) -> None:
        result = encode_double_field(1, 0.0)

        assert result[0:1] == b"\x09"
        assert result[1:] == struct.pack("<d", 0.0)

    def test_positive_value(self) -> None:
        result = encode_double_field(2, 3.14159)

        assert result[0:1] == b"\x11"
        assert result[1:] == struct.pack("<d", 3.14159)

    def test_negative_value(self) -> None:
        result = encode_double_field(3, -2.5)

        assert result[0:1] == b"\x19"
        assert result[1:] == struct.pack("<d", -2.5)
