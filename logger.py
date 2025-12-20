# logger.py
from datetime import datetime

class Logger:
    @staticmethod
    def log(symbol, action, message):
        t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{t} | {symbol:<9} | {action:<10} | {message}")
