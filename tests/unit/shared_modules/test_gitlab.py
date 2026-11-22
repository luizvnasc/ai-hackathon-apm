"""shared/gitlab.py, with send() faked: no request is made."""
import unittest
from unittest import mock

from tests.unit._load import load_shared

gitlab = load_shared("gitlab")
net = load_shared("net")
TOKEN = "glpat-secret"


def client(*answers):
    """A client whose send() returns `answers` in order."""
    made = gitlab.GitLabClient("https://gitlab.example.com/", TOKEN, "demo/app")
    made.send = mock.Mock(side_effect=answers)
    return made


def sent_urls(made):
    return [call.args[1] for call in made.send.call_args_list]


class GraphqlTest(unittest.TestCase):
    def test_returns_the_data(self):
        made = client(({"data": {"ok": 1}}, {}))
        self.assertEqual(made.graphql("query", {"p": "a/b"}), {"ok": 1})
        payload = {"query": "query", "variables": {"p": "a/b"}}
        made.send.assert_called_once_with("POST", "https://gitlab.example.com/api/graphql", payload)

    def test_variables_default_to_empty(self):
        made = client(({"data": {}}, {}))
        made.graphql("query")
        self.assertEqual(made.send.call_args.args[2]["variables"], {})

    def test_an_error_with_http_200_is_raised(self):
        made = client(({"errors": [{"message": "boom"}]}, {}))
        with self.assertRaises(net.ApiError) as caught:
            made.graphql("query")
        self.assertEqual(str(caught.exception), 'POST /api/graphql -> HTTP 200: [{"message": "boom"}]')


class RestTest(unittest.TestCase):
    def test_token_is_only_in_the_header(self):
        made = client(({}, {}))
        made.rest("/user")
        self.assertEqual(made.headers, {"PRIVATE-TOKEN": TOKEN})
        self.assertNotIn(TOKEN, repr(made.send.call_args))

    def test_read(self):
        made = client(({"id": 1}, {}))
        self.assertEqual(made.rest("/projects", {"search": "a b&c"}), {"id": 1})
        made.send.assert_called_once_with("GET", "https://gitlab.example.com/api/v4/projects?search=a+b%26c", None)

    def test_write(self):
        made = client(({"id": 1}, {}))
        made.rest("/labels", method="POST", body={"name": "x"})
        made.send.assert_called_once_with("POST", "https://gitlab.example.com/api/v4/labels", {"name": "x"})

    def test_follows_every_page(self):
        made = client(([1], {"X-Next-Page": "2"}), ([2], {"X-Next-Page": "3"}), ([3], {"X-Next-Page": ""}))
        self.assertEqual(made.rest_pages("/x"), [1, 2, 3])
        base = "https://gitlab.example.com/api/v4/x?per_page=100&page="
        self.assertEqual(sent_urls(made), [base + "1", base + "2", base + "3"])

    def test_does_not_change_the_params_it_receives(self):
        params = {"sort": "asc"}
        client(([], {})).rest_pages("/x", params)
        self.assertEqual(params, {"sort": "asc"})

    def test_empty_answer_is_an_error_by_default(self):
        with self.assertRaises(TypeError):
            client((None, {})).rest_pages("/x")

    def test_empty_answer_can_be_an_empty_page(self):
        made = client((None, {}))
        made.empty_page_is_ok = True
        self.assertEqual(made.rest_pages("/x"), [])


class ProjectsTest(unittest.TestCase):
    def test_lists_the_membership_most_recent_first(self):
        made = client(([], {}))
        made.member_projects()
        query = "membership=true&archived=false&order_by=last_activity_at&sort=desc&per_page=100&page=1"
        self.assertEqual(sent_urls(made), ["https://gitlab.example.com/api/v4/projects?" + query])

    def test_hides_projects_marked_for_deletion(self):
        def project(path, **fields):
            return {"path_with_namespace": path, "name": path, "web_url": "url", **fields}

        rows = [project("g/kept"), project("g/on", marked_for_deletion_on="2026-01-01"),
                project("g/at", marked_for_deletion_at="2026-01-01")]
        self.assertEqual(client((rows, {})).my_projects(), [{"path": "g/kept", "name": "g/kept", "url": "url"}])


if __name__ == "__main__":
    unittest.main()
