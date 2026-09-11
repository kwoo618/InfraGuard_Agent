"""Grafana 프로비저닝·대시보드·target-server 버킷 설정의 일관성 검사 (Phase 5, #74).

실행 중인 Grafana 없이 저장소 파일만 읽는다. PyYAML이 backend 의존성에 없어서
YAML·compose 파일은 텍스트로 확인한다.
"""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GRAFANA_DIR = ROOT / "infra" / "grafana"
DASHBOARD = GRAFANA_DIR / "dashboard.json"
DATASOURCE_PROVISIONING = GRAFANA_DIR / "provisioning" / "datasources" / "prometheus.yml"
DASHBOARD_PROVISIONING = GRAFANA_DIR / "provisioning" / "dashboards" / "infraguard.yml"
COMPOSE = ROOT / "docker-compose.yml"
TARGET_SERVER = ROOT / "infra" / "target-server" / "main.py"


def _panels() -> list[dict]:
    data = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return [panel for panel in data["panels"] if panel.get("type") != "row"]


def _exprs() -> list[str]:
    return [target["expr"] for panel in _panels() for target in panel.get("targets", [])]


def test_dashboard_datasource_uid_matches_provisioning():
    """패널이 참조하는 datasource uid = 프로비저닝 datasource uid (docs/02 ISSUE-4)."""
    text = DATASOURCE_PROVISIONING.read_text(encoding="utf-8")
    match = re.search(r"^\s*uid:\s*(\S+)\s*$", text, re.MULTILINE)
    assert match is not None
    uid = match.group(1)

    for panel in _panels():
        assert panel["datasource"]["uid"] == uid, panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] == uid, panel["title"]


def test_dashboard_queries_use_existing_labels_only():
    """에러율은 status="5xx" 라벨(ISSUE-7), cAdvisor 쿼리는 없음(ISSUE-3)."""
    exprs = _exprs()
    assert exprs
    for expr in exprs:
        assert "status_code" not in expr
        assert "container_" not in expr
    assert any('status="5xx"' in expr for expr in exprs)


def test_replica_panel_counts_up_targets():
    assert 'count(up{job="target-server"} == 1)' in _exprs()


def test_home_dashboard_path_points_to_mounted_dashboard():
    """결과 패널 링크(localhost:3000)가 여는 홈 대시보드 = 마운트된 dashboard.json."""
    compose = COMPOSE.read_text(encoding="utf-8")
    mount = re.search(r"-\s*\./infra/grafana:(\S+?):ro", compose)
    home = re.search(r"GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=(\S+)", compose)
    assert mount is not None and home is not None
    assert home.group(1) == f"{mount.group(1)}/{DASHBOARD.name}"

    provider = DASHBOARD_PROVISIONING.read_text(encoding="utf-8")
    assert re.search(rf"^\s*path:\s*{re.escape(mount.group(1))}\s*$", provider, re.MULTILINE)


def test_grafana_image_is_pinned():
    compose = COMPOSE.read_text(encoding="utf-8")
    image = re.search(r"image:\s*grafana/grafana:(\S+)", compose)
    assert image is not None
    assert image.group(1) != "latest"


def test_latency_buckets_cover_measured_range():
    """P95 SLO 기본값 1초가 버킷 경계이고, 실측 1대 /heavy P95(약 3.3초)를 담는다 (ISSUE-8)."""
    tree = ast.parse(TARGET_SERVER.read_text(encoding="utf-8"))
    buckets = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "LATENCY_BUCKETS" for target in node.targets
        ):
            buckets = ast.literal_eval(node.value)
    assert buckets is not None

    assert list(buckets) == sorted(set(buckets))
    assert 1.0 in buckets
    assert max(buckets) >= 5.0
