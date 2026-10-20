from django.test import TestCase
from django.urls import reverse

from .helpers import TEST_PASSWORD, make_user


class AuthenticationTests(TestCase):
    def setUp(self):
        self.user = make_user("worker")

    def test_login_page_renders(self):
        self.assertEqual(self.client.get(reverse("accounts:login")).status_code, 200)

    def test_valid_login_redirects_to_home(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "worker", "password": TEST_PASSWORD}
        )
        self.assertRedirects(response, reverse("accounts:home"))

    def test_invalid_login_is_rejected(self):
        response = self.client.post(
            reverse("accounts:login"), {"username": "worker", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_home_requires_login(self):
        response = self.client.get(reverse("accounts:home"))
        self.assertRedirects(
            response, f"{reverse('accounts:login')}?next=/", fetch_redirect_response=False
        )

    def test_logout_requires_post_and_ends_session(self):
        self.client.login(username="worker", password=TEST_PASSWORD)
        self.assertEqual(self.client.get(reverse("accounts:logout")).status_code, 405)
        self.client.post(reverse("accounts:logout"))
        self.assertNotIn("_auth_user_id", self.client.session)
