"""Unit tests for domain models."""

from __future__ import annotations

from mcp_oranpi.domain.models import (
    BlockIO,
    ContainerDetail,
    ContainerSummary,
    DiskInfo,
    HealthStatus,
    MountInfo,
    NetworkBinding,
    NetworkIO,
    OccupiedPort,
    PortBinding,
    PortOccupant,
    PortSuggestion,
    SensorReading,
    ServiceInfo,
    TailscalePeer,
    TailscaleStatus,
    TruncationMeta,
)


class TestDockerModels:
    """Tests for Docker domain models."""

    def test_port_binding(self) -> None:
        """PortBinding stores host and container port info."""
        pb = PortBinding(
            host_port=8080,
            container_port=80,
            protocol="tcp",
            host_ip="0.0.0.0",
        )
        assert pb.host_port == 8080
        assert pb.container_port == 80
        assert pb.protocol == "tcp"
        assert pb.host_ip == "0.0.0.0"

    def test_container_summary_minimal(self) -> None:
        """ContainerSummary with required fields only."""
        cs = ContainerSummary(
            id="abc123def456",
            name="web-app",
            image="nginx:latest",
            status="running",
            created="2024-01-01T00:00:00Z",
        )
        assert cs.ports == []
        assert cs.id == "abc123def456"

    def test_container_summary_with_ports(self) -> None:
        """ContainerSummary with port bindings."""
        ports = [
            PortBinding(host_port=80, container_port=80, protocol="tcp", host_ip="0.0.0.0"),
            PortBinding(host_port=443, container_port=443, protocol="tcp", host_ip="0.0.0.0"),
        ]
        cs = ContainerSummary(
            id="abc123def456",
            name="web-app",
            image="nginx:latest",
            status="running",
            created="2024-01-01T00:00:00Z",
            ports=ports,
        )
        assert len(cs.ports) == 2
        assert cs.ports[0].host_port == 80
        assert cs.ports[1].host_port == 443

    def test_container_detail_minimal(self) -> None:
        """ContainerDetail with required fields only."""
        cd = ContainerDetail(
            id="abc123def456789012345678",
            name="web-app",
            image="nginx:latest",
            status="running",
            created="2024-01-01T00:00:00Z",
        )
        assert cd.ports == []
        assert cd.networks == []
        assert cd.labels == {}
        assert cd.env == []
        assert cd.mounts == []
        assert cd.health is None
        assert cd.restart_policy == ""

    def test_container_detail_full(self) -> None:
        """ContainerDetail with all fields."""
        health = HealthStatus(status="healthy", failing_streak=0, last_output="OK")
        mount = MountInfo(source="/host/data", destination="/app/data", mode="rw", type="bind")
        cd = ContainerDetail(
            id="abc123def456789012345678",
            name="web-app",
            image="nginx:latest",
            status="running",
            created="2024-01-01T00:00:00Z",
            networks=["bridge", "frontend"],
            labels={"com.docker.compose.service": "web"},
            env=["PATH", "NODE_ENV"],
            mounts=[mount],
            health=health,
            restart_policy="unless-stopped",
        )
        assert len(cd.networks) == 2
        assert cd.labels["com.docker.compose.service"] == "web"
        assert len(cd.env) == 2
        assert "PATH" in cd.env
        # Security: env list should contain NAMES only, never values
        assert cd.health is not None
        assert cd.health.status == "healthy"
        assert cd.restart_policy == "unless-stopped"

    def test_container_detail_env_is_names_only(self) -> None:
        """ContainerDetail.env stores variable NAMES only, never values."""
        cd = ContainerDetail(
            id="abc123",
            name="app",
            image="app:latest",
            status="running",
            created="2024-01-01T00:00:00Z",
            env=["PATH", "HOME", "DATABASE_URL"],
        )
        # All entries should be names, not KEY=VALUE pairs
        for entry in cd.env:
            assert "=" not in entry

    def test_port_occupant_minimal(self) -> None:
        """PortOccupant with required fields only."""
        po = PortOccupant(
            host_port=5432,
            container_port=5432,
            protocol="tcp",
            container_name="db-app-1",
            container_id="abc123def456",
            host_ip="0.0.0.0",
        )
        assert po.host_port == 5432
        assert po.process is None

    def test_port_occupant_with_process(self) -> None:
        """PortOccupant with process info."""
        po = PortOccupant(
            host_port=80,
            container_port=80,
            protocol="tcp",
            container_name="web-app-1",
            container_id="def789abc012",
            host_ip="0.0.0.0",
            process="nginx",
        )
        assert po.process == "nginx"
        assert po.container_name == "web-app-1"

    def test_health_status(self) -> None:
        """HealthStatus captures container health."""
        hs = HealthStatus(
            status="healthy",
            failing_streak=0,
            last_output="OK",
        )
        assert hs.status == "healthy"
        assert hs.failing_streak == 0

    def test_mount_info(self) -> None:
        """MountInfo stores bind mount or volume info."""
        mi = MountInfo(
            source="/host/data",
            destination="/app/data",
            mode="rw",
            type="bind",
        )
        assert mi.type == "bind"
        assert mi.mode == "rw"

    def test_network_io(self) -> None:
        """NetworkIO stores bytes in/out."""
        nio = NetworkIO(bytes_in=1024, bytes_out=2048)
        assert nio.bytes_in == 1024
        assert nio.bytes_out == 2048

    def test_block_io(self) -> None:
        """BlockIO stores read/write bytes."""
        bio = BlockIO(bytes_read=512, bytes_written=1024)
        assert bio.bytes_read == 512
        assert bio.bytes_written == 1024


class TestNetworkModels:
    """Tests for Network domain models."""

    def test_occupied_port(self) -> None:
        """OccupiedPort with minimal required fields."""
        op = OccupiedPort(port=80, protocol="tcp")
        assert op.process is None
        assert op.container is None

    def test_occupied_port_with_process(self) -> None:
        """OccupiedPort with process info."""
        op = OccupiedPort(port=80, protocol="tcp", process="nginx")
        assert op.process == "nginx"

    def test_port_suggestion_available(self) -> None:
        """PortSuggestion for an available port."""
        ps = PortSuggestion(port=8080, available=True)
        assert ps.occupant is None

    def test_port_suggestion_occupied(self) -> None:
        """PortSuggestion for an occupied port."""
        ps = PortSuggestion(port=80, available=False, occupant="nginx:80")
        assert ps.available is False

    def test_network_binding(self) -> None:
        """NetworkBinding with all fields."""
        nb = NetworkBinding(
            local_address="0.0.0.0",
            port=443,
            protocol="tcp",
            process="nginx",
            pid=1234,
            container="web-app",
        )
        assert nb.local_address == "0.0.0.0"
        assert nb.pid == 1234

    def test_network_binding_minimal(self) -> None:
        """NetworkBinding with required fields only."""
        nb = NetworkBinding(
            local_address="0.0.0.0",
            port=80,
            protocol="tcp",
        )
        assert nb.process is None
        assert nb.pid is None
        assert nb.container is None

    def test_tailscale_peer(self) -> None:
        """TailscalePeer with all fields."""
        tp = TailscalePeer(
            name="laptop",
            tailscale_ip="100.100.100.2",
            online=True,
            last_seen="2024-01-01T00:00:00Z",
        )
        assert tp.online is True
        assert tp.last_seen is not None

    def test_tailscale_peer_offline(self) -> None:
        """TailscalePeer offline without last_seen."""
        tp = TailscalePeer(
            name="desktop",
            tailscale_ip="100.100.100.3",
            online=False,
        )
        assert tp.last_seen is None

    def test_tailscale_status_minimal(self) -> None:
        """TailscaleStatus with required fields only."""
        ts = TailscaleStatus(online=True)
        assert ts.tailscale_ip is None
        assert ts.public_ip is None
        assert ts.hostname == ""
        assert ts.dns_name is None
        assert ts.peers == []

    def test_tailscale_status_full(self) -> None:
        """TailscaleStatus with all fields."""
        peer = TailscalePeer(
            name="laptop",
            tailscale_ip="100.100.100.2",
            online=True,
        )
        ts = TailscaleStatus(
            online=True,
            tailscale_ip="100.100.100.1",
            public_ip="203.0.113.1",
            hostname="oranpi",
            dns_name="oranpi.tailnet.example.com",
            peers=[peer],
        )
        assert ts.online is True
        assert ts.tailscale_ip == "100.100.100.1"
        assert ts.public_ip == "203.0.113.1"
        assert ts.hostname == "oranpi"
        assert ts.dns_name == "oranpi.tailnet.example.com"
        assert len(ts.peers) == 1


class TestSystemModels:
    """Tests for System domain models."""

    def test_disk_info(self) -> None:
        """DiskInfo stores filesystem usage."""
        di = DiskInfo(
            mount_point="/",
            device="/dev/sda1",
            total_gb=100.0,
            used_gb=45.5,
            available_gb=54.5,
            percent=45.5,
            filesystem_type="ext4",
        )
        assert di.percent == 45.5
        assert di.filesystem_type == "ext4"

    def test_sensor_reading(self) -> None:
        """SensorReading with optional critical threshold."""
        sr = SensorReading(name="cpu_thermal", temp_c=65.0)
        assert sr.critical_c is None

    def test_sensor_reading_with_critical(self) -> None:
        """SensorReading with critical threshold."""
        sr = SensorReading(name="cpu_thermal", temp_c=85.0, critical_c=95.0)
        assert sr.critical_c == 95.0

    def test_service_info(self) -> None:
        """ServiceInfo stores systemd service state."""
        si = ServiceInfo(
            name="nginx",
            active="active",
            sub_state="running",
            enabled=True,
            description="A high performance web server",
        )
        assert si.enabled is True


class TestTruncationMeta:
    """Tests for TruncationMeta model."""

    def test_default_no_truncation(self) -> None:
        """TruncationMeta defaults to no truncation."""
        meta = TruncationMeta()
        assert meta.truncated is False
        assert meta.total_count is None
        assert meta.returned_count is None
        assert meta.bytes_limit == 0

    def test_truncation_with_counts(self) -> None:
        """TruncationMeta with list counts."""
        meta = TruncationMeta(
            truncated=True,
            total_count=750,
            returned_count=500,
            bytes_limit=0,
        )
        assert meta.truncated is True
        assert meta.total_count == 750
        assert meta.returned_count == 500

    def test_truncation_with_bytes(self) -> None:
        """TruncationMeta with byte limit."""
        meta = TruncationMeta(
            truncated=True,
            bytes_limit=51200,
        )
        assert meta.bytes_limit == 51200