import os
from pathlib import Path
from cryptography.fernet import Fernet
from .logger import Logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Security:
    def __init__(self, key_file: str = ".secret.key"):
        self.key_file = PROJECT_ROOT / key_file
        self.key = self._load_or_create_key()
        self.cipher = Fernet(self.key)

    def _load_or_create_key(self):
        if self.key_file.exists():
            with open(self.key_file, "rb") as f:
                return f.read()

        key = Fernet.generate_key()
        with open(self.key_file, "wb") as f:
            f.write(key)

        try:
            os.chmod(self.key_file, 0o600)
        except Exception:
            pass

        return key

    def encrypt(self, text):
        if not text:
            return ""
        return self.cipher.encrypt(str(text).encode()).decode()

    def decrypt(self, encrypted_text):
        if not encrypted_text:
            return ""
        try:
            return self.cipher.decrypt(encrypted_text.encode()).decode()
        except Exception as exc:
            Logger.log("SYSTEM", "ERROR", f"解密失败: {exc}")
            return None
