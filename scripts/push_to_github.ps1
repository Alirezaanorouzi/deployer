# Push local main to GitHub (after you create an empty repo on github.com).
# Usage: .\scripts\push_to_github.ps1 -RepoName liara-deployer

param(
  [string]$Owner = "Alirezaanorouzi",
  [string]$RepoName = "liara-deployer"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$remote = "https://github.com/$Owner/$RepoName.git"
git remote remove origin 2>$null
git remote add origin $remote

$token = & .\.venv\Scripts\python -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('GITHUB_TOKEN','').strip())"
if (-not $token) { throw "GITHUB_TOKEN missing in .env" }

$pushUrl = "https://x-access-token:${token}@github.com/${Owner}/${RepoName}.git"
git push -u $pushUrl main
git remote set-url origin $remote
Write-Host "Pushed to $remote"
