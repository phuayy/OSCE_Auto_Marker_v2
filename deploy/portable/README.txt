OSCE AI Marker - portable install
=================================

Nothing here needs administrator rights, Docker, Git, Node or a separate
Python install. Everything lives in this folder.

1. Unzip this folder somewhere you own, with a short path and no special
   characters, for example C:\Users\<you>\OSCE-Marker
   (not Program Files, and not inside OneDrive if you can avoid it).
2. Double-click Install.cmd. It sets up Python and the packages inside
   this folder, then opens app\.env in Notepad:
     WHISPERX_HF_TOKEN  HuggingFace token, needed for speaker diarisation
     NVIDIA_API_KEY     scoring model key (can also be set later in
                        Settings > Provider API keys)
3. Double-click Start.cmd. The app opens at http://127.0.0.1:8787
   Sign in as admin / change-me, then change the password (Account page).
   Close the Start window to stop the app.

Good to know
- The first transcription downloads the speech models (about 3 GB) into
  your user profile, so that run needs internet. Later runs do not.
- GPU: transcription uses the NVIDIA card if its driver is installed.
  Without it Start.cmd falls back to the CPU, which is much slower.
  Installing a GPU driver is the one thing that does need an administrator.
- If you move this folder, run Install.cmd again before Start.cmd.
- Your data (uploads, results, database) is in app\storage. Back that up.
- Windows may say the .cmd files came from the internet. Choose
  "More info" > "Run anyway", or right-click the zip > Properties >
  Unblock before unzipping.
