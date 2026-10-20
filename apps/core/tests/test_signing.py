from django.test import SimpleTestCase

from apps.core.exceptions import InvalidQRToken
from apps.core.signing import QRKind, make_token, read_token


class QRTokenTests(SimpleTestCase):
    def test_round_trip(self):
        token = make_token(QRKind.MACHINE, 42)
        self.assertEqual(read_token(token, QRKind.MACHINE), "42")

    def test_tampered_token_is_rejected(self):
        token = make_token(QRKind.MACHINE, 42)
        bad = token[:-1] + ("A" if token[-1] != "A" else "B")
        with self.assertRaises(InvalidQRToken):
            read_token(bad, QRKind.MACHINE)

    def test_wrong_kind_is_rejected(self):
        token = make_token(QRKind.MACHINE, 42)
        with self.assertRaises(InvalidQRToken):
            read_token(token, QRKind.TRAVELER)

    def test_garbage_is_rejected(self):
        with self.assertRaises(InvalidQRToken):
            read_token("not-a-real-token", QRKind.MACHINE)

    def test_unknown_kind_cannot_be_issued(self):
        with self.assertRaises(ValueError):
            make_token("spaceship", 1)
