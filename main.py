import requests

sendkey = 'SCT472354T6RHnd6IaYaQAVeiJblBSdpAx'
url = f"https://sctapi.ftqq.com/{sendkey}.send"

data = {
    "title": "任务完成通知",
    "desp": "你的脚本已经跑完了，一切正常。"
}


response = requests.post(url, data=data)
print(response.json())
