"""goodsmare：日淘上新提醒。盯着日本二手站的新上架，一出现就推到你手机上。"""

import sys

if sys.version_info < (3, 9):
    sys.exit("goodsmare 需要 Python 3.9 或更新的版本（现在是 %d.%d），去 https://www.python.org/downloads/ 装个新的。"
             % sys.version_info[:2])

__version__ = "0.2.0"
APP_NAME = "goodsmare"
