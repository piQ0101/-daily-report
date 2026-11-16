import requests
import os

token = os.environ['SENDKEY']
url = f"https://sctapi.ftqq.com/{token}.send"

data = {
    "title": "任务完成通知",
    "desp": "你的脚本已经跑完了，一切正常。"
}


response = requests.post(url, data=data)
print(response.json())
