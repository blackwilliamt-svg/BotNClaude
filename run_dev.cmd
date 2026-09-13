@echo off
set MASTER_KEY=dev-test-master-key-not-for-prod
"%~dp0.venv\Scripts\python.exe" "%~dp0server.py"
