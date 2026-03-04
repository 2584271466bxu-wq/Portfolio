Set shell = CreateObject("WScript.Shell")
projectPath = "C:\Job\Job_Scraping"
pythonwPath = projectPath & "\.venv\Scripts\pythonw.exe"
scriptPath = projectPath & "\job_monitor.py"
configPath = projectPath & "\config.json"

shell.Run Chr(34) & pythonwPath & Chr(34) & " " & Chr(34) & scriptPath & Chr(34) & " --once --config " & Chr(34) & configPath & Chr(34), 0, False
