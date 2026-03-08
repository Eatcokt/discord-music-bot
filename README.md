# discord-music-bot


skip to the environment setup if you already have python
# Installation Steps
Open PowerShell: Search for "PowerShell" in the Start Menu and select the standard Windows PowerShell (not the admin version, for a typical per-user install).
# Set the Execution Policy: 
Run the following command to allow the execution of scripts downloaded from the internet for your current user scope:
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
Type Y and press Enter when prompted to confirm the change.
        Invoke-RestMethod -Uri https://get.scoop.sh | Invoke-Expression

# then run: 
scoop install python

# Setting up the environment

create a virtual environment for python using: python -m venv venv
then use powershell and cd to folder and run: venv\scripts\activate 
run: pip install discord spotipy yt_dlp dotenv
run: bot.py (whatever version it is called)
