"""Minimal write-only protobuf wire-format encoder."""

import struct

from ..support.vocabulary import ENCODING

__all__ = [
    "encode_bytes_field",
    "encode_double_field",
    "encode_field_key",
    "encode_fixed64_field",
    "encode_string_field",
    "encode_varint",
    "encode_varint_field",
]

WIRE_TYPE_VARINT = 0
WIRE_TYPE_FIXED64 = 1
WIRE_TYPE_LENGTH_DELIMITED = 2


def encode_varint(value: int) -> bytes:
    if value < 0:
        value = value & 0xFFFFFFFFFFFFFFFF
    if value == 0:
        return b"\x00"
    result = []
    while value > 0:
        byte = value & 0x7F
        value >>= 7
        if value > 0:
            byte |= 0x80
        result.append(byte)
    return bytes(result)


def encode_field_key(field_number: int, wire_type: int) -> bytes:
    return encode_varint((field_number << 3) | wire_type)


def encode_varint_field(field_number: int, value: int) -> bytes:
    return encode_field_key(field_number, WIRE_TYPE_VARINT) + encode_varint(value)


def encode_fixed64_field(field_number: int, value: int) -> bytes:
    return encode_field_key(field_number, WIRE_TYPE_FIXED64) + struct.pack("<Q", value)


def encode_string_field(field_number: int, value: str) -> bytes:
    encoded = value.encode(ENCODING)
    return encode_field_key(field_number, WIRE_TYPE_LENGTH_DELIMITED) + encode_varint(len(encoded)) + encoded


def encode_bytes_field(field_number: int, value: bytes) -> bytes:
    return encode_field_key(field_number, WIRE_TYPE_LENGTH_DELIMITED) + encode_varint(len(value)) + value


def encode_double_field(field_number: int, value: float) -> bytes:
    return encode_field_key(field_number, WIRE_TYPE_FIXED64) + struct.pack("<d", value)
