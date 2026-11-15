"""Compose 出站网络回归检查；不连接生产数据库或真实邮箱。"""
from pathlib import Path
import re
import unittest

COMPOSE = Path(__file__).resolve().parents[1] / "compose.prod.yaml"


class ComposeWorkerEgressTests(unittest.TestCase):
    def setUp(self):
        self.text = COMPOSE.read_text()

    def service(self, name):
        return re.search(
            rf"^  {name}:\n(.*?)(?=^  [a-z][a-z_-]*:|^volumes:)",
            self.text, re.M | re.S,
        ).group(1)

    def test_worker_has_egress_without_publishing_ports(self):
        worker = self.service("worker")
        self.assertIn("networks: [backend, public]", worker)
        self.assertNotRegex(worker, r"(?m)^    ports:")
        self.assertIn("cap_drop: [ALL]", worker)

    def test_database_stays_on_internal_network(self):
        postgres = self.service("postgres")
        self.assertIn("networks: [backend]", postgres)
        self.assertNotRegex(postgres, r"(?m)^    ports:")
        self.assertRegex(self.text, r"networks:\n  backend:\n    internal: true")


if __name__ == "__main__":
    unittest.main()
