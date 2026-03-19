@echo off
cd /d "S:\claude coding\Onboarding Workflow Automation"
python execution\process_applicants.py
python execution\process_challenge.py
python execution\process_clan_cleanup.py
