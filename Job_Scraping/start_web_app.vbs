Set shell = CreateObject("WScript.Shell")
projectPath = "C:\Job\Job_Scraping"
pythonwPath = projectPath & "\.venv\Scripts\pythonw.exe"
webAppPath = projectPath & "\web_app.py"

shell.Run Chr(34) & pythonwPath & Chr(34) & " " & Chr(34) & webAppPath & Chr(34), 0, False
WScript.Sleep 1500
shell.Run "http://127.0.0.1:5000"
