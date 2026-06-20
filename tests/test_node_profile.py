"""Tests for v30 multi-tenant node profile."""

from __future__ import annotations

import os
import unittest

from system.node_profile import (
    apply_node_profile_to_environ,
    get_node_profile,
    reset_node_profile_for_tests,
)


class NodeProfileTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_node_profile_for_tests()
        for key in (
            "NODE_ENV",
            "IG_NODE_PROFILE",
            "IG_API_PORT",
            "IG_AGENT_SHADOW_DESK",
            "IG_APEX_PROTECT_PRODUCTION_PORTS",
        ):
            os.environ.pop(key, None)

    def test_production_defaults(self) -> None:
        os.environ["NODE_ENV"] = "production"
        reset_node_profile_for_tests()
        profile = apply_node_profile_to_environ()
        self.assertEqual(profile.kind, "production")
        self.assertEqual(profile.api_port, 8080)
        self.assertEqual(profile.cockpit_port, 8787)

    def test_shadow_sandbox(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        reset_node_profile_for_tests()
        profile = get_node_profile(reload=True)
        apply_node_profile_to_environ()
        self.assertEqual(profile.kind, "shadow")
        self.assertEqual(profile.api_port, 9090)
        self.assertEqual(profile.cockpit_port, 9191)
        self.assertTrue(profile.runtime_state_file.name.endswith("runtime_state_shadow.json"))
        self.assertEqual(os.environ.get("IG_AGENT_SHADOW_DESK"), "1")

    def test_resolve_api_port_shadow(self) -> None:
        os.environ["NODE_ENV"] = "shadow"
        reset_node_profile_for_tests()
        from system.boot.preflight_helpers import resolve_api_port

        self.assertEqual(resolve_api_port(), 9090)

    def test_resolve_api_port_production(self) -> None:
        os.environ["NODE_ENV"] = "production"
        reset_node_profile_for_tests()
        from system.boot.preflight_helpers import resolve_api_port

        self.assertEqual(resolve_api_port(), 8080)


if __name__ == "__main__":
    unittest.main()
