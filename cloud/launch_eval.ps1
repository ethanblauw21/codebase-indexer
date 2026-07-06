<#
  launch_eval.ps1 — one-command launcher for the ADR-019 cloud reranker eval.

  What it does (local, Windows PowerShell):
    1. tars the run bundle (repo code + fixtures + prebuilt indexes)
    2. uploads it to the GCS bucket
    3. creates a SPOT T4 VM that runs cloud/startup.sh and self-deletes

  Prereqs: the one-time setup in cloud/README.md is done (gcloud installed + authed,
  APIs enabled, GPU quota granted, bucket + service account created).

  Run from the repo root:   .\cloud\launch_eval.ps1
#>
$ErrorActionPreference = "Stop"

# --- config (matches cloud/README.md) -------------------------------------------
$PROJECT = "prefabinventoryapp"
$ZONE    = "us-central1-a"
$BUCKET  = "prefabinventoryapp-code-eval"
$SA      = "eval-runner@$PROJECT.iam.gserviceaccount.com"
$STAMP   = Get-Date -Format "yyyyMMdd-HHmmss"
$VM      = "adr019-eval-$STAMP"

# --- 1. bundle the exact files the eval needs -----------------------------------
Write-Host "==> taring run bundle..." -ForegroundColor Cyan
tar -czf bundle.tar.gz `
    tools src pyproject.toml indexer.toml `
    benchmarks/real_repo/fixtures `
    benchmarks/real_repo/repos.toml `
    benchmarks/real_repo/index
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

# --- 2. upload it ---------------------------------------------------------------
Write-Host "==> uploading bundle to gs://$BUCKET/inputs/ ..." -ForegroundColor Cyan
gcloud storage cp bundle.tar.gz "gs://$BUCKET/inputs/bundle.tar.gz" --project=$PROJECT
if ($LASTEXITCODE -ne 0) { throw "upload failed" }

# --- 3. create the spot T4 VM ---------------------------------------------------
# It runs cloud/startup.sh, which self-deletes on completion. --max-run-duration +
# --instance-termination-action=DELETE is the hard backstop: even if the script dies,
# the VM is DELETED (never left billing) within 3h, and a preempted spot VM is deleted
# too (not left stopped).
Write-Host "==> creating spot T4 VM $VM ..." -ForegroundColor Cyan
$args = @(
  "compute","instances","create",$VM,
  "--project=$PROJECT",
  "--zone=$ZONE",
  "--machine-type=n1-standard-4",
  "--accelerator=type=nvidia-tesla-t4,count=1",
  "--maintenance-policy=TERMINATE",
  "--provisioning-model=SPOT",
  "--instance-termination-action=DELETE",
  "--max-run-duration=10800s",
  "--image-family=common-cu129-ubuntu-2404-nvidia-580",
  "--image-project=deeplearning-platform-release",
  "--boot-disk-size=100GB",
  "--service-account=$SA",
  "--scopes=cloud-platform",
  "--metadata=eval-bucket=$BUCKET",
  "--metadata-from-file=startup-script=cloud/startup.sh"
)
gcloud @args
if ($LASTEXITCODE -ne 0) { throw "VM create failed (GPU quota not granted yet? see README step 3)" }

Write-Host ""
Write-Host "Launched $VM. It self-deletes when done (~30-45 min)." -ForegroundColor Green
Write-Host "Watch progress:" -ForegroundColor Yellow
Write-Host "  gcloud compute instances get-serial-port-output $VM --zone=$ZONE --project=$PROJECT | Select-String 'startup|device|/C:|verdict|reranker'"
Write-Host "Confirm it's gone (should be empty when finished):" -ForegroundColor Yellow
Write-Host "  gcloud compute instances list --project=$PROJECT"
Write-Host "Fetch results once it's gone:" -ForegroundColor Yellow
Write-Host "  gcloud storage cp gs://$BUCKET/outputs/result.jsonl .\benchmarks\real_repo\real_repo_authoritative.jsonl --project=$PROJECT"
Write-Host "  gcloud storage cp gs://$BUCKET/outputs/eval-run.log . --project=$PROJECT   # contains the printed verdict()"
