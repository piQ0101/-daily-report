import os
import requests

TOKEN = os.environ["PUSHPLUS_TOKEN"]
URL = "https://www.pushplus.plus/send"

# ====== 这里放你的抓取逻辑 ======
# 你现在不用改，先让它发一条测试消息
content = "这是一条测试消息，说明 GitHub Actions 跑通了。"
# ==============================

data = {
    "token": TOKEN,
    "title": "每日汇总",
    "content": content,
    "template": "txt"
}

resp = requests.post(URL, json=data)
print(resp.text)
