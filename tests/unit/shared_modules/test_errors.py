"""ApiError of shared/net.py."""
import unittest

from tests.unit._load import load_shared

net = load_shared("net")


class ApiErrorTest(unittest.TestCase):
    def test_message_shows_only_the_start_of_the_answer(self):
        error = net.ApiError(500, "x" * 900, "GET", "url")
        self.assertEqual(str(error), "GET url -> HTTP 500: " + "x" * 400)

    def test_keeps_the_status_and_the_whole_answer(self):
        error = net.ApiError(500, "x" * 900, "GET", "url")
        self.assertEqual((error.status, error.body), (500, "x" * 900))

    def test_the_caller_can_show_more(self):
        self.assertEqual(str(net.ApiError(500, "x" * 900, "GET", "url", shown=500)).count("x"), 500)


if __name__ == "__main__":
    unittest.main()
