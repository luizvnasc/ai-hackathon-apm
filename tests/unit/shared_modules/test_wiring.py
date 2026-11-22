"""Every skill uses the shared clients, and redefines only the variants listed here."""
import unittest
from unittest import mock

from tests.unit._load import load

SKILLS = {"assign-issues": "assign.py", "blocked-issues": "blocked.py", "commitment-delivered": "commitment.py",
          "populate-project": "populate.py", "scope-stability": "stability.py", "stale-issues": "stale.py",
          "team-velocity": "velocity.py"}

# The methods of a shared client that a skill replaces, because it still behaves differently.
# Remove the entry together with the variant.
VARIANTS = {
    ("assign-issues", "GitLab"): {"my_projects"},
    ("assign-issues", "Plane"): {"my_projects"},
    ("blocked-issues", "GitLab"): {"my_projects", "graphql"},
    ("blocked-issues", "Plane"): {"my_projects"},
    ("scope-stability", "Plane"): {"request"},
}
EMPTY_PAGE_IS_OK = {"assign-issues", "blocked-issues", "populate-project"}
ALWAYS_OWN = {"__init__", "send"}


def replaced_methods(client):
    """The shared methods that the skill's class defines again."""
    shared = client.__mro__[1]
    own = {name for name, member in vars(client).items() if callable(member)}
    common = {name for name, member in vars(shared).items() if callable(member)}
    return (own & common) - ALWAYS_OWN


class ClientWiringTest(unittest.TestCase):
    def test_uses_the_shared_clients_except_for_the_known_variants(self):
        for skill, script in SKILLS.items():
            module = load(skill, script)
            for name in ("GitLab", "Plane"):
                with self.subTest(skill=skill, client=name):
                    client = getattr(module, name)
                    self.assertEqual(client.__mro__[1].__name__, f"{name}Client")
                    self.assertEqual(replaced_methods(client), VARIANTS.get((skill, name), set()))

    def test_empty_page_rule_of_each_skill(self):
        for skill, script in SKILLS.items():
            with self.subTest(skill=skill):
                client = load(skill, script).GitLab
                self.assertEqual(client.empty_page_is_ok, skill in EMPTY_PAGE_IS_OK)

    def test_send_goes_through_the_http_of_the_skill(self):
        for skill, script in SKILLS.items():
            module = load(skill, script)
            answer = ({"ok": 1}, {"X-Next-Page": ""})
            # populate's http() also returns the status
            returned = (200, *answer) if skill == "populate-project" else answer
            for name in ("GitLab", "Plane"):
                with self.subTest(skill=skill, client=name), mock.patch.object(module, "http", return_value=returned) as http:
                    client = getattr(module, name).__new__(getattr(module, name))
                    client.headers = {"key": "secret"}
                    self.assertEqual(client.send("POST", "url", {"a": 1}), answer)
                    self.assertEqual(http.call_args.args, ("POST", "url", {"key": "secret"}, {"a": 1}))


if __name__ == "__main__":
    unittest.main()
