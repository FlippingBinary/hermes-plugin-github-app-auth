"""Tests for schemas.py — tool schema structure and contracts."""

from github_app_auth.schemas import LOGIN_SCHEMA, LOGOUT_SCHEMA


class TestLoginSchema:
    def test_name(self) -> None:
        assert LOGIN_SCHEMA["name"] == "github_app_login"

    def test_has_description(self) -> None:
        assert isinstance(LOGIN_SCHEMA["description"], str)
        assert len(LOGIN_SCHEMA["description"]) > 0

    def test_parameters_type(self) -> None:
        assert LOGIN_SCHEMA["parameters"]["type"] == "object"

    def test_repo_property(self) -> None:
        props = LOGIN_SCHEMA["parameters"]["properties"]
        assert "repo" in props
        assert props["repo"]["type"] == "string"

    def test_repo_required(self) -> None:
        assert "repo" in LOGIN_SCHEMA["parameters"]["required"]


class TestLogoutSchema:
    def test_name(self) -> None:
        assert LOGOUT_SCHEMA["name"] == "github_app_logout"

    def test_has_description(self) -> None:
        assert isinstance(LOGOUT_SCHEMA["description"], str)
        assert len(LOGOUT_SCHEMA["description"]) > 0

    def test_parameters_type(self) -> None:
        assert LOGOUT_SCHEMA["parameters"]["type"] == "object"

    def test_no_required_params(self) -> None:
        assert LOGOUT_SCHEMA["parameters"].get("required", []) == []
