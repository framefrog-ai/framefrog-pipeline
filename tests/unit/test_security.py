# SPDX-FileCopyrightText: 2026 FrameFrog
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Security boundary tests: authentication, URI validation, SSRF, path safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AuthenticationError, ValidationError
from app.core.security import (
    assert_safe_http_url,
    constant_time_equals,
    parse_input_uri,
    resolve_local_input,
    safe_object_key,
    safe_relative_path,
    verify_api_key,
)


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #
def test_api_key_required_when_configured():
    with pytest.raises(AuthenticationError):
        verify_api_key(None, "expected")
    with pytest.raises(AuthenticationError):
        verify_api_key("wrong", "expected")


def test_api_key_accepted():
    verify_api_key("expected", "expected")


def test_api_key_disabled_when_not_configured():
    verify_api_key(None, None)
    verify_api_key("anything", "")


def test_constant_time_compare():
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("abc", "abcd")


# --------------------------------------------------------------------------- #
# URI validation
# --------------------------------------------------------------------------- #
def test_s3_uri_parsed(tmp_path):
    parsed = parse_input_uri("s3://bucket/path/to/file.mp4", allowed_roots=[tmp_path])
    assert parsed.kind == "s3"
    assert parsed.bucket == "bucket"
    assert parsed.key == "path/to/file.mp4"


def test_s3_uri_without_key_rejected(tmp_path):
    with pytest.raises(ValidationError):
        parse_input_uri("s3://bucket", allowed_roots=[tmp_path])


def test_s3_uri_with_traversal_rejected(tmp_path):
    with pytest.raises(ValidationError):
        parse_input_uri("s3://bucket/../etc/passwd", allowed_roots=[tmp_path])


def test_unsupported_scheme_rejected(tmp_path):
    with pytest.raises(ValidationError):
        parse_input_uri("ftp://example.com/x.mp4", allowed_roots=[tmp_path])


def test_http_inputs_disabled_by_default(tmp_path):
    with pytest.raises(ValidationError):
        parse_input_uri("http://example.com/x.mp4", allowed_roots=[tmp_path])


def test_local_path_outside_root_rejected(tmp_path):
    outside = Path("/etc/passwd")
    with pytest.raises(ValidationError):
        parse_input_uri(str(outside), allowed_roots=[tmp_path])


def test_local_path_inside_root_accepted(tmp_path):
    target = tmp_path / "clip.mp4"
    target.write_bytes(b"x")
    parsed = parse_input_uri(str(target), allowed_roots=[tmp_path])
    assert parsed.kind == "local"
    assert parsed.path == target.resolve()


def test_traversal_in_relative_path_rejected(tmp_path):
    with pytest.raises(ValidationError):
        safe_relative_path("../etc/passwd")
    with pytest.raises(ValidationError):
        safe_relative_path("a/../../b")


def test_null_byte_rejected(tmp_path):
    with pytest.raises(ValidationError):
        parse_input_uri("bad\x00name.mp4", allowed_roots=[tmp_path])


def test_resolve_local_input_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = root / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(ValidationError):
        resolve_local_input(str(link), [root])


# --------------------------------------------------------------------------- #
# SSRF
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/admin",
        "http://[::1]/x",
    ],
)
def test_blocked_destinations(url):
    with pytest.raises(ValidationError):
        assert_safe_http_url(url)


def test_unsupported_url_scheme():
    with pytest.raises(ValidationError):
        assert_safe_http_url("ftp://example.com/x")


# --------------------------------------------------------------------------- #
# object keys
# --------------------------------------------------------------------------- #
def test_safe_object_key_strips_traversal():
    key = safe_object_key("outputs", "job/../../etc", "master.mp4")
    assert key == "outputs/job/etc/master.mp4"
    assert ".." not in key


def test_safe_object_key_requires_content():
    with pytest.raises(ValidationError):
        safe_object_key("", "..")
