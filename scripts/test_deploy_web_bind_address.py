"""部署编排的 web 绑定地址行为测试。

覆盖 CANVAS_WEB_HOST_IP 的三条落地链路：
- docker-compose.deploy.yml 的端口映射插值；
- install-server*.sh 的 validate_web_host_ip 校验；
- install-host-updater.sh 的 ensure_health_url 健康检查推导。

用 bash 直接调用脚本里的真实函数，而不是复制一份实现，避免测试与脚本漂移。
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).parent
DEPLOY_COMPOSE = SCRIPTS.parent / "docker-compose.deploy.yml"
ENV_EXAMPLE = SCRIPTS.parent / ".env.example"


def bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def extract_function(path: Path, name: str) -> str:
    """从 shell 脚本里截取单个函数定义。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def compose_port_template() -> str:
    """取部署编排里 web 的端口映射模板（已剔除 YAML 列表前缀与引号）。"""
    match = re.search(
        r'"\$\{CANVAS_WEB_HOST_IP:-[^}]*\}:\$\{CANVAS_HTTP_PORT:-[^}]*\}:\d+"',
        DEPLOY_COMPOSE.read_text(encoding="utf-8"),
    )
    assert match, "docker-compose.deploy.yml 缺少 CANVAS_WEB_HOST_IP 端口映射"
    return match.group(0).strip('"')


def interpolate(template: str, env: dict) -> str:
    """模拟 docker compose 的 ${VAR:-default} 插值语义。"""
    return re.sub(
        r"\$\{(\w+):-([^}]*)\}",
        lambda m: env.get(m.group(1)) or m.group(2),
        template,
    )


class ComposePortMappingTests(unittest.TestCase):
    def setUp(self):
        self.template = compose_port_template()

    def test_defaults_to_all_interfaces_like_before(self):
        # 未设置变量时必须与改动前的 "3000:3000" 等价，否则现有部署会失效。
        self.assertEqual(interpolate(self.template, {}), "0.0.0.0:3000:3000")

    def test_empty_value_keeps_existing_deployments_working(self):
        # 旧 .env 里没有该键、写成空值，两种情况都必须落回默认。
        self.assertEqual(interpolate(self.template, {"CANVAS_WEB_HOST_IP": ""}), "0.0.0.0:3000:3000")

    def test_explicit_wildcard_is_all_interfaces(self):
        result = interpolate(self.template, {"CANVAS_WEB_HOST_IP": "0.0.0.0"})
        self.assertEqual(result, "0.0.0.0:3000:3000")

    def test_specific_address_restricts_the_binding(self):
        env = {"CANVAS_WEB_HOST_IP": "100.74.221.72", "CANVAS_HTTP_PORT": "3100"}
        self.assertEqual(interpolate(self.template, env), "100.74.221.72:3100:3000")

    def test_custom_port_applies_without_bind_address(self):
        self.assertEqual(interpolate(self.template, {"CANVAS_HTTP_PORT": "8080"}), "0.0.0.0:8080:3000")

    def test_ipv6_keeps_brackets(self):
        self.assertEqual(interpolate(self.template, {"CANVAS_WEB_HOST_IP": "[::1]"}), "[::1]:3000:3000")


class ValidateWebHostIpTests(unittest.TestCase):
    """两个部署脚本共用同一份校验规则。"""

    def setUp(self):
        self.scripts = [SCRIPTS / "install-server.sh", SCRIPTS / "install-server-image.sh"]

    def check(self, value: str) -> list[str]:
        result = bash(f"{extract_function(SCRIPTS / 'install-server.sh', 'validate_web_host_ip')}\n"
                      f"if validate_web_host_ip '{value}'; then echo accept; else echo reject; fi")
        return result.stdout.strip().splitlines()

    def test_accepts_ipv4_literals(self):
        for value in ["100.74.221.72", "0.0.0.0", "127.0.0.1", "192.168.1.10", ""]:
            with self.subTest(value=value):
                self.assertEqual(self.check(value), ["accept"])

    def test_accepts_bracketed_ipv6(self):
        for value in ["[::1]", "[fe80::1]", "[2001:db8::1]"]:
            with self.subTest(value=value):
                self.assertEqual(self.check(value), ["accept"])

    def test_rejects_bare_ipv6_to_avoid_port_ambiguity(self):
        # 裸 IPv6 会被 Compose 与端口号混在一起解析，要求显式方括号。
        for value in ["::1", "fe80::1"]:
            with self.subTest(value=value):
                self.assertEqual(self.check(value), ["reject"])

    def test_rejects_values_carrying_port_or_mask(self):
        for value in ["100.74.221.72:3100", "1.2.3.4:80:80", "[::1]:3100", "100.74.221.72/24"]:
            with self.subTest(value=value):
                self.assertEqual(self.check(value), ["reject"])

    def test_rejects_non_address_input(self):
        for value in ["example.com", "http://1.2.3.4", "1.2.3", "1.2.3.4;id"]:
            with self.subTest(value=value):
                self.assertEqual(self.check(value), ["reject"])

    def test_both_install_scripts_share_the_same_rule(self):
        for path in self.scripts:
            with self.subTest(script=path.name):
                self.assertIn("validate_web_host_ip", path.read_text(encoding="utf-8"))


class UpdaterHealthUrlTests(unittest.TestCase):
    """绑定地址后，回环不再可达，健康检查必须跟着改。"""

    def setUp(self):
        self.function = extract_function(SCRIPTS / "install-host-updater.sh", "ensure_health_url")
        self.tmp = tempfile.TemporaryDirectory()
        self.install_dir = Path(self.tmp.name)
        self.updater_env = self.install_dir / "updater.env"
        self.env = {"INSTALL_DIR": str(self.install_dir), "UPDATER_ENV": str(self.updater_env)}

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, host_ip: str, port: str) -> str:
        (self.install_dir / ".env").write_text(
            f"CANVAS_HTTP_PORT={port}\nCANVAS_WEB_HOST_IP={host_ip}\n", encoding="utf-8"
        )
        self.updater_env.write_text("", encoding="utf-8")
        bash(f"{self.function}\nensure_health_url", env=self.env)
        for line in self.updater_env.read_text(encoding="utf-8").splitlines():
            if line.startswith("CANVAS_UPDATER_HEALTH_URL="):
                return line.split("=", 1)[1]
        return ""

    def test_all_interfaces_uses_loopback(self):
        # 监听全部接口时回环必然可达，避免把健康检查流量打到外部网卡。
        self.assertEqual(self.resolve("", "3000"), "http://127.0.0.1:3000/api/health/ready")
        self.assertEqual(self.resolve("0.0.0.0", "3000"), "http://127.0.0.1:3000/api/health/ready")

    def test_bound_address_replaces_loopback(self):
        # 这是 updater 二进制硬编码回环导致在线更新失败的根因。
        self.assertEqual(
            self.resolve("100.74.221.72", "3100"),
            "http://100.74.221.72:3100/api/health/ready",
        )

    def test_ipv6_keeps_brackets(self):
        self.assertEqual(self.resolve("[::1]", "3000"), "http://[::1]:3000/api/health/ready")

    def test_manual_override_is_preserved(self):
        (self.install_dir / ".env").write_text(
            "CANVAS_HTTP_PORT=3100\nCANVAS_WEB_HOST_IP=100.74.221.72\n", encoding="utf-8"
        )
        self.updater_env.write_text(
            "CANVAS_UPDATER_TOKEN=abc\nCANVAS_UPDATER_HEALTH_URL=http://custom:9999/api/health/ready\n",
            encoding="utf-8",
        )
        bash(f"{self.function}\nensure_health_url", env=self.env)
        self.assertIn(
            "CANVAS_UPDATER_HEALTH_URL=http://custom:9999/api/health/ready",
            self.updater_env.read_text(encoding="utf-8"),
        )

    def test_repeated_install_does_not_duplicate_entry(self):
        (self.install_dir / ".env").write_text(
            "CANVAS_HTTP_PORT=3100\nCANVAS_WEB_HOST_IP=100.74.221.72\n", encoding="utf-8"
        )
        self.updater_env.write_text("", encoding="utf-8")
        for _ in range(3):
            bash(f"{self.function}\nensure_health_url", env=self.env)
        lines = [
            line for line in self.updater_env.read_text(encoding="utf-8").splitlines()
            if line.startswith("CANVAS_UPDATER_HEALTH_URL=")
        ]
        self.assertEqual(len(lines), 1)


class EnvExampleTests(unittest.TestCase):
    def test_variable_is_documented(self):
        # 该变量此前完全不可发现，部署者无法自行定位。
        self.assertIn("CANVAS_WEB_HOST_IP", ENV_EXAMPLE.read_text(encoding="utf-8"))

    def test_example_declares_no_real_address(self):
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            if line.startswith("CANVAS_WEB_HOST_IP"):
                self.assertNotRegex(line, r"=\s*[^#\s]\S*")


class ShellSyntaxTests(unittest.TestCase):
    def test_modified_scripts_parse(self):
        for path in [SCRIPTS / "install-server.sh", SCRIPTS / "install-server-image.sh", SCRIPTS / "install-host-updater.sh"]:
            with self.subTest(script=path.name):
                self.assertEqual(bash(f"bash -n {path}").returncode, 0)


if __name__ == "__main__":
    unittest.main()
