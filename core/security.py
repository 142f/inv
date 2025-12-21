import os
from cryptography.fernet import Fernet
from .logger import Logger

class Security:
    def __init__(self, key_file=".secret.key"):
        self.key_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), key_file)
        self.key = self._load_or_create_key()
        self.cipher = Fernet(self.key)

    def _load_or_create_key(self):
        """加载或创建加密密钥"""
        if os.path.exists(self.key_file):
            with open(self.key_file, 'rb') as f:
                return f.read()
        else:
            key = Fernet.generate_key()
            with open(self.key_file, 'wb') as f:
                f.write(key)
            # 设置文件权限 (仅在 Unix 系统有效，Windows 忽略)
            try:
                os.chmod(self.key_file, 0o600)
            except:
                pass
            return key

    def encrypt(self, text):
        """加密字符串"""
        if not text: return ""
        return self.cipher.encrypt(str(text).encode()).decode()

    def decrypt(self, encrypted_text):
        """解密字符串"""
        if not encrypted_text: return ""
        try:
            return self.cipher.decrypt(encrypted_text.encode()).decode()
        except Exception as e:
            Logger.log("SYSTEM", "ERROR", f"解密失败: {e}")
            return None
