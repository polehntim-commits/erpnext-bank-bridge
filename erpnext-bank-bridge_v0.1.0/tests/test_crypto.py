# SPDX-License-Identifier: MIT
"""Fernet encryption for the stored Plaid access_tokens:
  * round-trips (encrypt → decrypt) correctly
  * ciphertext is not the plaintext
  * a key is generated + persisted on first use and reused thereafter

    cd erpnext-bank-bridge_v0.1.0
    python3 -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest

os.environ.setdefault('DATABASE_URL', 'postgresql://x:x@localhost/x')

from app import create_app, db, crypto  # noqa: E402


class CryptoBase(unittest.TestCase):
    def setUp(self):
        self._dbfd, self._dbpath = tempfile.mkstemp(suffix='.sqlite')
        self._datadir = tempfile.mkdtemp()
        self.app = create_app({
            'TESTING': True,
            'SQLALCHEMY_DATABASE_URI': f'sqlite:///{self._dbpath}',
            'DATA_DIR': self._datadir,
            'FERNET_KEY': '',           # force autogenerate + persist
            'SCHEDULER_ENABLED': False,
        })
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        crypto.reset_cache()
        os.close(self._dbfd)
        os.remove(self._dbpath)


class TestCrypto(CryptoBase):
    def test_round_trip(self):
        token = 'access-sandbox-3b8f7a0e-1234-5678-9abc-def012345678'
        ct = crypto.encrypt(token)
        self.assertNotEqual(ct, token)
        self.assertNotIn(token, ct)
        self.assertEqual(crypto.decrypt(ct), token)

    def test_key_persisted_and_reused(self):
        # First use generates the key file.
        ct = crypto.encrypt('secret-token')
        key_path = os.path.join(self._datadir, 'fernet.key')
        self.assertTrue(os.path.exists(key_path))
        # A fresh cache (simulating a new process) still decrypts using the
        # persisted key.
        crypto.reset_cache()
        self.assertEqual(crypto.decrypt(ct), 'secret-token')

    def test_configured_key_wins(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode('utf-8')
        self.app.config['FERNET_KEY'] = key
        crypto.reset_cache()
        ct = crypto.encrypt('abc')
        # No key file needed when FERNET_KEY is configured.
        self.assertEqual(crypto.decrypt(ct), 'abc')
        # The configured key is exactly what decrypts it.
        self.assertEqual(Fernet(key.encode()).decrypt(ct.encode()).decode(), 'abc')


if __name__ == '__main__':
    unittest.main()
