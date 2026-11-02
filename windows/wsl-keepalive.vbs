' Keeps the Ubuntu WSL distro (and Docker + the bot inside it) running, without a window.
CreateObject("WScript.Shell").Run "wsl.exe -d Ubuntu --exec sleep infinity", 0, False
