# Ollama batch examples: Multiple Linear Regression I

These Windows batch files run the requested actions with:

```text
Connector: ollama
Input: input\Regression Modeling with Actuarial and Financial Applications_014 - 3 Multiple Linear Regression - I.tex
Output: tmp\output_<action>_<run_id>.txt
```

The scripts use the chapter source currently stored at that exact `.tex` path. Update
`INPUT_FILE` in `run_action.bat` if you rename or replace the chapter.

Run one artifact from this folder:

```bat
create_datatables.bat
create_flashcards.bat 20260908123000
create_qandas.bat
```

Run every action with the same run identifier:

```bat
run_all.bat 20260907115400
```

If no run identifier is supplied, the examples use `20260907115400` to mirror the
requested command format. Change it for later runs to avoid overwriting earlier output.
The scripts use `py -3.11` by default. Set `PYTHON_CMD` first if your virtual environment
uses a different launcher, for example `set "PYTHON_CMD=python"`. The selected Python
environment must have this project's dependencies installed, and Ollama must be available
at `OLLAMA_BASE_URL`.
