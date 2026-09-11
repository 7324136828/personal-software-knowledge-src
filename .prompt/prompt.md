## Genesis 

based on the skills, can you create based on the original-project so that it will have everything in the root? Make sure you follow the guidance of productionization. Also make sure we can have a UI in reactjs such that the user can paste/select the pdf files to be converted and it will create a temp folder under system's temp folder and it will pipe through the script and allow the user to retrieve from the output file? Create a backend with the python code based on original-project. Make sure you have setup.bat/setup.sh file that will dispatch setup.py such that it will setup virtual environment and run.bat/run.sh such that it will dispatch the frontend and backend? 


## Defect 1 
Can you fix the code? 
Backend Offline? 
http://localhost:5174/api/config
Request method
GET
Status code
500 Internal Server Error
Remote address
[::1]:5174[RUN] Using Python: ***
.venv\Scripts\python.exe [RUN] Launching Backend on [http://127.0.0.1:8080](http://127.0.0.1:8080) ... [RUN] Launching Frontend on [http://localhost:5173](http://localhost:5173) ...
============================================================
Application is running!
-> Web UI:  http://localhost:5173
-> API Docs: http://127.0.0.1:8000/docs
Press Ctrl+C to stop both servers.
INFO:     Will watch for changes in these directories: [****]
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
INFO:     Started reloader process [38776] using StatReload
personal-software-knowledge-ui@1.0.0 dev
vite

INFO:     Started server process [33520]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
Port 5173 is in use, trying another one...
VITE v6.4.3  ready in 1443 ms
➜  Local:   http://localhost:5174/
➜  Network: use --host to expose
➜  press h + enter to show help
11:02:01 PM [vite] http proxy error: /api/config
Error: connect ECONNREFUSED 127.0.0.1:8000
at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1706:16)
11:02:01 PM [vite] http proxy error: /api/config
Error: connect ECONNREFUSED 127.0.0.1:8000
at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1706:16) (x2)


## Feature 1 
user should be able to access historical conversion of the files. can you create a screen which can be directed via the landing page that store historical conversion of the files? 
In fact, you should allow the file in progress also show in historical conversion of the file, and user can select either continue or discard. For completed file, user should be able to download as a zip file.

## Feature 2 
The user can select multiple artifacts/actions under Select Learning Artifact / Action selection. In addition, the user should be able to upload multiple files.

## Defect 2
If we encounter error like the following, we should allow the server to truncate the size (by a random amount) of the original chunk and retry to the backend with the error appended in the prompt?  server: Application error during generation: Invalid output for chunk_0002: $.data[0].source_id: invalid format; $.data[1].source_id: invalid format; $.data[2].source_id: invalid format

## Feature 3 
in the conversation history, rather than having 
multiple artifact for the same file:
test2_000 - Complete Text.tex
failed
Podcast Scripts
ollama · llama3.2
Sep 10, 2026, 11:38 PM
Invalid output for chunk_0001: $.cast: speaker ids must be unique
Continue
Discard
test2_000 - Complete Text.tex
completed
Q & A Pairs
ollama · llama3.2
Sep 10, 2026, 11:38 PM
Download ZIP
Discard
test2_000 - Complete Text.tex
failed
Infographics
ollama · llama3.2
Sep 10, 2026, 11:37 PM
Invalid output for chunk_0001_01: $.sections[4].items: chart entries must be 'label: number'; $.sections[5].value: must reference assets/<slug>.svg
Continue
Discard
test2_000 - Complete Text.tex
completed
Data Tables
ollama · llama3.2
Sep 10, 2026, 11:37 PM
Download ZIP
Discard
test2_000 - Complete Text.tex
failed
Flashcards
ollama · llama3.2
Sep 10, 2026, 11:37 PM
Invalid output for chunk_0001: $.cards[0].tags[1]: invalid format; $.cards[0].tags[2]: invalid format; $.cards[2].tags[0]: invalid format
Continue
It should be just one file:
test2_000 - Complete Text.tex
and an accordian view, when we expand the accordian view, we will have the items:
test2_000 - Complete Text.tex
|
+--- Podcast Scripts (success) (download/delete)
|    [small log about Podcast Scripts]
+--- Q & A Pairs (failed) (try again/delete)
|    [small log about Q & A]
+--- Infographics (failed) (try again/delete)
    [small log about Infographics]
test1_000 - Complete Text.tex (queued) (cancel)
|
+--- Podcast Scripts (in progress) (cancel)
|
+--- Q & A Pairs (in progress) (cancel)
|
+--- Infographics (in progress) (cancel)
Since user is able to upload multiple files, the user should be able to have a file list history that they can drag the file to higher priority or lower priority, can we remodel the history screen as file list screen?
In addition, I don't see that we are doing the retries for the files that are failed, can we log the number of retries in the log? We should be able to them in the log. When the user retry, in fact, they should be able to select the number of characters for chunk separation for the artifact.
# of characters of chunk separation
[2048] characters
[okay] [no]

Total: 6 prompts to finish 

## Documentation 1
based on the images folder, can you update the readme.md file?
