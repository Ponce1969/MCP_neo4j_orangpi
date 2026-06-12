"""Unit tests for domain parameter validation."""

from __future__ import annotations

import pytest

from mcp_oranpi.domain.errors import (
    VALID_PARAM_INVALID,
    VALID_PARAM_OUT_OF_RANGE,
    VALID_PARAM_REQUIRED,
    WS_PATH_ESCAPE,
)
from mcp_oranpi.domain.validation import (
    ValidationError,
    validate_container_name,
    validate_duration,
    validate_line_limit,
    validate_log_path,
    validate_port,
    validate_port_range,
    validate_service_name,
    validate_workspace_id,
    validate_workspace_path,
)


class TestValidationError:
    """Tests for ValidationError exception."""

    def test_error_with_field(self) -> None:
        """ValidationError carries code and field."""
        err = ValidationError(
            VALID_PARAM_INVALID,
            "Invalid input",
            field="port",
        )
        assert err.code == VALID_PARAM_INVALID
        assert err.field == "port"
        assert str(err) == "Invalid input"

    def test_error_without_field(self) -> None:
        """ValidationError field defaults to None."""
        err = ValidationError(VALID_PARAM_REQUIRED, "Missing param")
        assert err.field is None


class TestWorkspaceIdValidation:
    """Tests for workspace identifier validation."""

    def test_valid_simple_id(self) -> None:
        """Simple alphanumeric IDs pass validation."""
        assert validate_workspace_id("guardian") == "guardian"

    def test_valid_id_with_hyphen(self) -> None:
        """IDs with hyphens pass validation."""
        assert validate_workspace_id("my-project") == "my-project"

    def test_valid_id_with_underscore(self) -> None:
        """IDs with underscores pass validation."""
        assert validate_workspace_id("my_project") == "my_project"

    def test_valid_id_with_digits(self) -> None:
        """IDs with digits pass validation."""
        assert validate_workspace_id("project2") == "project2"

    def test_reject_empty(self) -> None:
        """Empty IDs are rejected with VALID_PARAM_REQUIRED."""
        with pytest.raises(ValidationError) as exc_info:
            validate_workspace_id("")
        assert exc_info.value.code == VALID_PARAM_REQUIRED
        assert exc_info.value.field == "workspace_id"

    def test_reject_traversal(self) -> None:
        """Path traversal patterns are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_workspace_id("../../etc")
        assert exc_info.value.code == VALID_PARAM_INVALID

    def test_reject_spaces(self) -> None:
        """IDs with spaces are rejected."""
        with pytest.raises(ValidationError):
            validate_workspace_id("my project")

    def test_reject_special_chars(self) -> None:
        """IDs with special characters are rejected."""
        with pytest.raises(ValidationError):
            validate_workspace_id("project!")

    def test_reject_slash(self) -> None:
        """IDs with slashes are rejected."""
        with pytest.raises(ValidationError):
            validate_workspace_id("path/to/project")


class TestContainerNameValidation:
    """Tests for Docker container name validation."""

    def test_valid_simple_name(self) -> None:
        """Simple container names pass validation."""
        assert validate_container_name("web-app") == "web-app"

    def test_valid_name_with_dots(self) -> None:
        """Container names with dots pass validation."""
        assert validate_container_name("web.app.v2") == "web.app.v2"

    def test_valid_name_with_underscores(self) -> None:
        """Container names with underscores pass validation."""
        assert validate_container_name("web_app") == "web_app"

    def test_reject_empty(self) -> None:
        """Empty container names are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_container_name("")
        assert exc_info.value.code == VALID_PARAM_REQUIRED

    def test_reject_start_with_dash(self) -> None:
        """Container names starting with dash are rejected."""
        with pytest.raises(ValidationError):
            validate_container_name("-app")

    def test_reject_start_with_dot(self) -> None:
        """Container names starting with dot are rejected."""
        with pytest.raises(ValidationError):
            validate_container_name(".app")

    def test_reject_spaces(self) -> None:
        """Container names with spaces are rejected."""
        with pytest.raises(ValidationError):
            validate_container_name("my app")


class TestServiceNameValidation:
    """Tests for systemd service name validation."""

    def test_valid_service_name(self) -> None:
        """Simple service names pass validation."""
        assert validate_service_name("nginx") == "nginx"

    def test_valid_service_with_dot(self) -> None:
        """Service names with dots pass validation."""
        assert validate_service_name("nginx.service") == "nginx.service"

    def test_valid_service_with_dash(self) -> None:
        """Service names with dashes pass validation."""
        assert validate_service_name("docker-compose") == "docker-compose"

    def test_reject_empty(self) -> None:
        """Empty service names are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_service_name("")
        assert exc_info.value.code == VALID_PARAM_REQUIRED


class TestPortValidation:
    """Tests for port number validation."""

    def test_valid_port_80(self) -> None:
        """Port 80 is valid."""
        assert validate_port(80) == 80

    def test_valid_port_1(self) -> None:
        """Minimum port 1 is valid."""
        assert validate_port(1) == 1

    def test_valid_port_65535(self) -> None:
        """Maximum port 65535 is valid."""
        assert validate_port(65535) == 65535

    def test_reject_port_0(self) -> None:
        """Port 0 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_port(0)
        assert exc_info.value.code == VALID_PARAM_OUT_OF_RANGE

    def test_reject_negative_port(self) -> None:
        """Negative ports are rejected."""
        with pytest.raises(ValidationError):
            validate_port(-1)

    def test_reject_port_over_65535(self) -> None:
        """Ports above 65535 are rejected."""
        with pytest.raises(ValidationError):
            validate_port(65536)


class TestPortRangeValidation:
    """Tests for port range validation."""

    def test_valid_range(self) -> None:
        """Valid port ranges pass validation."""
        assert validate_port_range(1, 1024) == (1, 1024)

    def test_valid_max_range(self) -> None:
        """Maximum allowed range of 10000 ports passes validation."""
        assert validate_port_range(1, 10000) == (1, 10000)

    def test_valid_small_range(self) -> None:
        """Small port ranges pass validation."""
        assert validate_port_range(80, 85) == (80, 85)

    def test_reject_inverted_range(self) -> None:
        """Inverted ranges (start > end) are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_port_range(1024, 80)
        assert exc_info.value.code == VALID_PARAM_INVALID

    def test_reject_too_large_range(self) -> None:
        """Ranges exceeding 10000 ports are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_port_range(1, 10002)
        assert exc_info.value.code == VALID_PARAM_OUT_OF_RANGE

    def test_reject_invalid_start_port(self) -> None:
        """Invalid start port in range is rejected."""
        with pytest.raises(ValidationError):
            validate_port_range(0, 1024)

    def test_reject_invalid_end_port(self) -> None:
        """Invalid end port in range is rejected."""
        with pytest.raises(ValidationError):
            validate_port_range(1, 70000)


class TestDurationValidation:
    """Tests for Duration literal validation."""

    def test_valid_durations(self) -> None:
        """All recognized duration literals pass validation."""
        for d in ("5m", "15m", "30m", "1h", "6h", "24h"):
            assert validate_duration(d) == d

    def test_reject_invalid_duration(self) -> None:
        """Non-literal durations are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_duration("2h")
        assert exc_info.value.code == VALID_PARAM_INVALID

    def test_reject_empty_duration(self) -> None:
        """Empty string duration is rejected."""
        with pytest.raises(ValidationError):
            validate_duration("")


class TestLineLimitValidation:
    """Tests for line limit validation."""

    def test_valid_limit_1(self) -> None:
        """Minimum limit 1 is valid."""
        assert validate_line_limit(1) == 1

    def test_valid_limit_100(self) -> None:
        """Default limit 100 is valid."""
        assert validate_line_limit(100) == 100

    def test_valid_limit_500(self) -> None:
        """Maximum limit 500 is valid."""
        assert validate_line_limit(500) == 500

    def test_reject_limit_0(self) -> None:
        """Limit 0 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_line_limit(0)
        assert exc_info.value.code == VALID_PARAM_OUT_OF_RANGE

    def test_reject_negative_limit(self) -> None:
        """Negative limits are rejected."""
        with pytest.raises(ValidationError):
            validate_line_limit(-10)

    def test_reject_limit_over_500(self) -> None:
        """Limits above 500 are rejected."""
        with pytest.raises(ValidationError):
            validate_line_limit(501)


class TestWorkspacePathValidation:
    """Tests for workspace path containment validation."""

    def test_valid_path_under_root(self) -> None:
        """A path under root_dir passes validation."""
        result = validate_workspace_path(
            "/home/cerra/codigo/guardian",
            "/home/cerra/codigo",
        )
        assert result == "/home/cerra/codigo/guardian"

    def test_reject_path_traversal(self) -> None:
        """Paths with '..' are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_workspace_path(
                "/home/cerra/codigo/../../etc/passwd",
                "/home/cerra/codigo",
            )
        assert exc_info.value.code == WS_PATH_ESCAPE

    def test_reject_absolute_escape(self) -> None:
        """Absolute paths outside root_dir are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_workspace_path(
                "/etc/passwd",
                "/home/cerra/codigo",
            )
        assert exc_info.value.code == WS_PATH_ESCAPE

    def test_reject_different_home(self) -> None:
        """Paths under a different home directory are rejected."""
        with pytest.raises(ValidationError):
            validate_workspace_path(
                "/home/other/codigo/project",
                "/home/cerra/codigo",
            )


class TestLogPathValidation:
    """Tests for log path allowlist validation."""

    def test_valid_log_path(self) -> None:
        """Log paths under an allowed directory pass validation."""
        assert validate_log_path(
            "/var/log/syslog",
            ["/var/log", "/home", "/opt"],
        ) == "/var/log/syslog"

    def test_valid_home_log_path(self) -> None:
        """Log paths under /home pass when /home is allowed."""
        assert validate_log_path(
            "/home/user/app.log",
            ["/var/log", "/home", "/opt"],
        ) == "/home/user/app.log"

    def test_reject_non_allowed_path(self) -> None:
        """Log paths outside allowed directories are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_log_path(
                "/etc/shadow",
                ["/var/log", "/home", "/opt"],
            )
        assert exc_info.value.code == VALID_PARAM_INVALID

    def test_reject_traversal_in_log_path(self) -> None:
        """Log paths with '..' are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            validate_log_path(
                "/var/log/../../../etc/shadow",
                ["/var/log", "/home", "/opt"],
            )
        assert exc_info.value.code == WS_PATH_ESCAPE